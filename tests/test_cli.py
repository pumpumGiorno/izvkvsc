"""Сценарии запуска с поддельным SoundCloud: dry-run, продолжение после прерывания, плейлисты."""

import csv
import json

import pytest

from vk2sc.cli import main
from vk2sc.normalize import simplify
from vk2sc.soundcloud import RetryExhausted


def sc_track(id, title, user, seconds=None):
    return {
        "id": id,
        "kind": "track",
        "title": title,
        "user": {"username": user},
        "permalink_url": f"https://soundcloud.com/{simplify(user).replace(' ', '')}/{id}",
        "full_duration": seconds * 1000 if seconds else None,
        "policy": "ALLOW",
        "playback_count": 100,
    }


CATALOG = [
    sc_track(1, "Believer", "Imagine Dragons", 204),
    sc_track(2, "Группа крови", "Кино", 285),
    sc_track(3, "Bad Romance (Skrillex Remix)", "Lady Gaga", 294),
    sc_track(4, "Get Lucky", "Daft Punk", 369),
    sc_track(5, "Get Lucky (Radio Edit)", "Daft Punk", 248),
    sc_track(6, "Starboy (feat. Daft Punk)", "The Weeknd", 230),
]


class FakeClient:
    """Ищет по вхождению ядра названия в запрос; считает все вызовы."""

    def __init__(self, catalog=CATALOG, fail_after=None, error=KeyboardInterrupt):
        self.catalog = catalog
        self.fail_after = fail_after
        self.error = error
        self.searches = []
        self.created = []
        self.updated = []
        self.oauth_token = None
        self.remote_playlists = []

    def search_tracks(self, query, limit=20):
        if self.fail_after is not None and len(self.searches) >= self.fail_after:
            raise self.error()
        self.searches.append(query)
        q = simplify(query)
        return [t for t in self.catalog if simplify(t["title"].split("(")[0]) in q]

    def resolve(self, url):
        for t in self.catalog:
            if t["permalink_url"] == url:
                return t
        return None

    def set_oauth_token(self, token):
        self.oauth_token = token

    def me(self):
        return {"id": 42, "username": "tester"}

    def user_playlists(self, user_id):
        return self.remote_playlists

    def create_playlist(self, title, track_ids, sharing="private"):
        self.created.append((title, list(track_ids), sharing))
        pid = 1000 + len(self.created)
        return {"id": pid, "permalink_url": f"https://soundcloud.com/tester/sets/{pid}", "track_count": len(track_ids)}

    def set_playlist_tracks(self, playlist_id, track_ids):
        self.updated.append((playlist_id, list(track_ids)))
        return {"id": playlist_id, "track_count": len(track_ids)}


class Inputs:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"неожиданный вопрос: {prompt}")
        return self.answers.pop(0)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOUNDCLOUD_OAUTH_TOKEN", raising=False)
    return tmp_path


def write_tracks(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_report(path):
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.reader(f, delimiter=";"))


BASIC = [
    "Imagine Dragons — Believer (Official Audio) | 3:24",
    "Кино — Группа крови | 4:45",
    "Lady Gaga — Bad Romance (Skrillex Remix)",
    "Неизвестный — Несуществующая песня",
]


def run(args, client, interactive=False, inputs=None, out=None):
    lines = []
    code = main(args, client=client, interactive=interactive,
                input_fn=inputs or Inputs(), out=out or (lambda *a: lines.append(" ".join(map(str, a)))))
    return code, "\n".join(lines)


def test_dry_run_report(workdir):
    write_tracks(workdir / "tracks.txt", BASIC)
    client = FakeClient()
    code, out = run(["--dry-run"], client)
    assert code == 0
    rows = read_report(workdir / "report.csv")
    assert rows[0] == ["Трек из VK", "Найдено в SoundCloud", "Ссылка", "Уверенность", "Статус"]
    statuses = [r[4] for r in rows[1:]]
    assert statuses == ["будет добавлен", "будет добавлен", "будет добавлен", "не найден"]
    assert rows[1][1].startswith("Imagine Dragons — Believer")
    assert int(rows[1][3]) >= 95
    assert client.created == [] and client.updated == []
    assert "Будет добавлено:" in out and "Не найдено:" in out


def test_resume_after_interrupt_does_not_search_again(workdir):
    write_tracks(workdir / "tracks.txt", BASIC)
    first = FakeClient(fail_after=2)  # Ctrl+C во время третьего поиска
    code, out = run(["--dry-run"], first)
    assert code == 130 and "Прогресс сохранён" in out
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert len(state["tracks"]) == 2

    second = FakeClient()
    code, _ = run(["--dry-run"], second)
    assert code == 0
    assert all("Believer" not in q and "Группа" not in q for q in second.searches)
    assert any("Bad Romance" in q for q in second.searches)

    third = FakeClient()
    run(["--dry-run"], third)
    assert third.searches == []  # всё уже обработано


def test_rate_limit_stops_and_keeps_state(workdir):
    write_tracks(workdir / "tracks.txt", BASIC)
    client = FakeClient(fail_after=1, error=lambda: RetryExhausted("HTTP 429"))
    code, out = run(["--dry-run"], client)
    assert code == 2 and "Подождите" in out
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert list(state["tracks"]) == ["imagine dragons — believer (official audio) #1"]
    assert (workdir / "report.csv").exists()


def test_real_run_asks_confirmation_and_is_idempotent(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:3])
    client = FakeClient()
    inputs = Inputs("", "y")  # название по умолчанию, подтверждение
    code, out = run([], client, interactive=True, inputs=inputs)
    assert code == 0
    assert client.created == [("Из VK", [1, 2, 3], "private")]
    assert "создать приватный плейлист «Из VK» — 3 треков" in out
    rows = read_report(workdir / "report.csv")
    assert [r[4] for r in rows[1:]] == ["добавлен", "добавлен", "добавлен"]

    # Повторный запуск: ничего не ищет, ничего не создаёт, дублей нет.
    again = FakeClient()
    code, out = run([], again, interactive=True, inputs=Inputs())
    assert code == 0 and again.searches == [] and again.created == [] and again.updated == []
    assert "уже в актуальном состоянии" in out

    # Дописали трек в конец — обновляется тот же плейлист целиком, порядок сохраняется.
    write_tracks(workdir / "tracks.txt", BASIC[:3] + ["The Weeknd feat. Daft Punk — Starboy"])
    more = FakeClient()
    code, _ = run([], more, interactive=True, inputs=Inputs("y"))
    assert code == 0 and more.created == []
    assert more.updated == [(1001, [1, 2, 3, 6])]


def test_declined_confirmation_creates_nothing(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    client = FakeClient()
    code, out = run(["--title", "Мой VK"], client, interactive=True, inputs=Inputs("n"))
    assert code == 0 and client.created == []
    assert "ничего не изменилось" in out
    assert read_report(workdir / "report.csv")[1][4] == "найден, не добавлен"


def test_no_input_never_creates_playlist(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    client = FakeClient()
    code, out = run([], client, interactive=False)
    assert code == 1 and client.created == []
    assert "Без подтверждения ничего не создаю" in out


def test_missing_token(workdir):
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    client = FakeClient()
    code, out = run([], client, interactive=True, inputs=Inputs("", ))
    assert code == 1 and "SOUNDCLOUD_OAUTH_TOKEN" in out and client.created == []


def test_split_into_parts_of_500(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    catalog = [sc_track(i, f"Song number {i}", "Band") for i in range(1, 503)]
    write_tracks(workdir / "tracks.txt", [f"Band — Song number {i}" for i in range(1, 503)])

    class ExactClient(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return [t for t in self.catalog if f"Band {t['title']}" == query]

    client = ExactClient(catalog)
    code, _ = run(["--title", "Из VK"], client, interactive=True, inputs=Inputs("y"))
    assert code == 0
    assert [(t, len(ids)) for t, ids, _ in client.created] == [("Из VK", 500), ("Из VK (2)", 2)]
    assert client.created[0][1][:3] == [1, 2, 3] and client.created[1][1] == [501, 502]


def test_duplicates_added_once(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", ["Imagine Dragons — Believer", "Кино — Группа крови", "Imagine Dragons — Believer"])
    client = FakeClient()
    code, _ = run([], client, interactive=True, inputs=Inputs("", "y"))
    assert code == 0 and client.created[0][1] == [1, 2]
    statuses = [r[4] for r in read_report(workdir / "report.csv")[1:]]
    assert statuses[2].startswith("дубликат")


def test_interactive_choice_skip_and_custom_query(workdir):
    write_tracks(
        workdir / "tracks.txt",
        [
            "Daft Punk — Get Lucky",  # два варианта разной длины → вопрос
            "Неизвестный — Несуществующая песня",  # ничего → свой запрос
            "Lady Gaga — Bad Romance (Skrillex Remix)",
        ],
    )
    client = FakeClient()
    inputs = Inputs("2", "Imagine Dragons Believer", "1")
    code, out = run(["--dry-run"], client, interactive=True, inputs=inputs)
    assert code == 0
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    entries = list(state["tracks"].values())
    assert entries[0]["status"] == "manual"
    assert entries[1]["status"] == "manual" and entries[1]["match"]["id"] == 1
    assert "Imagine Dragons Believer" in entries[1]["queries"]
    assert entries[2]["status"] == "auto"
    statuses = [r[4] for r in read_report(workdir / "report.csv")[1:]]
    assert statuses == ["выбран вручную", "выбран вручную", "будет добавлен"]


def test_skip_and_link(workdir):
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky", "Неизвестный — Песня"])
    client = FakeClient()
    inputs = Inputs("", "https://soundcloud.com/imaginedragons/1")
    run(["--dry-run"], client, interactive=True, inputs=inputs)
    entries = list(json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"].values())
    assert entries[0]["status"] == "skipped"
    assert entries[1]["status"] == "manual" and entries[1]["match"]["id"] == 1


def test_no_input_defers_then_asks_without_new_search(workdir):
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky"])
    first = FakeClient()
    run(["--dry-run"], first, interactive=False)
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert list(state["tracks"].values())[0]["status"] == "pending"
    assert read_report(workdir / "report.csv")[1][4] == "ждёт ручного выбора"

    second = FakeClient()
    run(["--dry-run"], second, interactive=True, inputs=Inputs("1"))
    assert second.searches == []
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert list(state["tracks"].values())[0]["status"] == "manual"


def test_review_reasks_skipped(workdir):
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky"])
    run(["--dry-run"], FakeClient(), interactive=True, inputs=Inputs(""))
    client = FakeClient()
    run(["--dry-run", "--review"], client, interactive=True, inputs=Inputs("1"))
    assert client.searches == []
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert list(state["tracks"].values())[0]["status"] == "manual"


def test_recover_playlist_created_before_crash(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:2])
    run(["--dry-run"], FakeClient())
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    # Имитируем падение сразу после POST /playlists: id не успел записаться.
    state["pending_create"] = {"title": "Из VK", "started_at": 1_700_000_000}
    state["playlist_title"] = "Из VK"
    (workdir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    client = FakeClient()
    client.remote_playlists = [
        {"id": 555, "title": "Из VK", "created_at": "2020-01-01T00:00:00Z"},  # старый, чужой — не трогаем
        {"id": 777, "title": "Из VK", "created_at": "2023-11-14T22:14:00Z", "permalink_url": "u"},
    ]
    code, out = run([], client, interactive=True, inputs=Inputs("y"))
    assert code == 0
    assert client.created == []
    assert client.updated == [(777, [1, 2])]
    assert "созданный в прошлый раз" in out


def test_fallback_query_used_when_auto_match_is_only_a_reupload(workdir):
    write_tracks(workdir / "tracks.txt", ["Мумий Тролль — Владивосток 2000"])
    reupload = sc_track(1, "Мумий Тролль - Владивосток 2000", "nano4ka", 162)
    official = sc_track(2, "Владивосток 2000", "Mumiy Troll", 161)

    class TwoQueries(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return [official] if query.isascii() else [reupload]

    client = TwoQueries()
    run(["--dry-run"], client)
    assert len(client.searches) == 2
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert list(state["tracks"].values())[0]["match"]["id"] == 2


def test_dry_run_does_not_fix_title_so_real_run_asks(workdir, monkeypatch):
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    run(["--dry-run"], FakeClient())
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    client = FakeClient()
    inputs = Inputs("Мой перенос", "y")
    run([], client, interactive=True, inputs=inputs)
    assert inputs.prompts[0].startswith("Название нового плейлиста")
    assert client.created[0][0] == "Мой перенос"


def test_numeric_query_and_out_of_range_number_search(workdir):
    write_tracks(workdir / "tracks.txt", ["Smashing Pumpkins — 1979"])
    client = FakeClient(catalog=[sc_track(9, "1979", "The Smashing Pumpkins", 266)])

    class Picky(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return self.catalog if query in ("1979", "/1979") else []

    client = Picky(catalog=client.catalog)
    run(["--dry-run"], client, interactive=True, inputs=Inputs("/1979", "1"))
    assert client.searches[-1] == "1979"
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert list(state["tracks"].values())[0]["match"]["id"] == 9


def test_deleted_playlist_is_recreated_after_confirmation(workdir, monkeypatch):
    from vk2sc.soundcloud import PlaylistNotFound

    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    run(["--title", "Из VK"], FakeClient(), interactive=True, inputs=Inputs("y"))
    write_tracks(workdir / "tracks.txt", BASIC[:2])

    class Gone(FakeClient):
        def set_playlist_tracks(self, playlist_id, track_ids):
            raise PlaylistNotFound("нет")

    client = Gone()
    code, out = run([], client, interactive=True, inputs=Inputs("y", "y"))
    assert code == 0 and "удалили на сайте" in out
    assert client.created == [("Из VK", [1, 2], "private")]
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["playlists"][0]["id"] == 1001 and state["playlists"][0]["track_ids"] == [1, 2]


def test_bad_request_delay_and_threshold(workdir, monkeypatch):
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    monkeypatch.setenv("REQUEST_DELAY", "полторы")
    code, out = run(["--dry-run"], None)
    assert code == 1 and "REQUEST_DELAY" in out
    code, out = run(["--dry-run", "--threshold", "150"], FakeClient())
    assert code == 1 and "--threshold" in out


def test_report_cells_cannot_become_excel_formulas(workdir):
    write_tracks(workdir / "tracks.txt", ["=cmd|' /C calc'!A0 — Song"])
    evil = sc_track(5, "Song", "@SUM(1+1)")

    class Exact(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return [evil]

    run(["--dry-run"], Exact())
    row = read_report(workdir / "report.csv")[1]
    assert row[0].startswith("'=cmd")
    assert row[1].startswith("'@SUM")
