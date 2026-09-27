"""Сценарии запуска с поддельным SoundCloud: dry-run, продолжение после прерывания, плейлисты."""

import csv
import json

import pytest

from vk2sc.cli import main
from vk2sc.matching import MATCHING_VERSION
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
    from vk2sc import envfile

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOUNDCLOUD_OAUTH_TOKEN", raising=False)
    # .env ищется только во временной папке: настоящий .env разработчика не должен влиять на тесты.
    monkeypatch.setattr(envfile, "env_candidates", lambda: [tmp_path / ".env"])
    return tmp_path


def write_tracks(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


STATUS = 8  # колонка status в report.csv


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
    assert rows[0] == ["line", "original_artist", "original_title", "search_title", "matched_artist",
                       "matched_title", "soundcloud_url", "score", "status", "search_variant", "note",
                       "title_score", "artist_score", "duration_delta", "version_conflict", "margin"]
    statuses = [r[STATUS] for r in rows[1:]]
    assert statuses == ["автоматически выбран"] * 3 + ["не найден"]
    assert rows[1][1:4] == ["Imagine Dragons", "Believer (Official Audio)", "Believer"]
    assert rows[1][4:6] == ["Imagine Dragons", "Believer"]
    assert int(rows[1][7]) >= 95
    assert rows[1][9] == "исходный"
    assert rows[4][4:8] == ["", "", "", ""]
    assert rows[1][11:15] == ["100", "100", "0", "нет"]  # признаки выбранного кандидата
    assert rows[4][11:] == ["", "", "", "", ""]
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
    inputs = Inputs("y")  # название не спрашивается, только подтверждение
    code, out = run([], client, interactive=True, inputs=inputs)
    assert code == 0
    assert inputs.prompts == ["Продолжить? [y/N]: "]
    assert client.created == [("Из VK", [1, 2, 3], "private")]
    assert "создать приватный плейлист «Из VK» — 3 треков" in out
    rows = read_report(workdir / "report.csv")
    assert [r[STATUS] for r in rows[1:]] == ["добавлен", "добавлен", "добавлен"]

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
    row = read_report(workdir / "report.csv")[1]
    assert row[STATUS] == "автоматически выбран" and "не добавлен" in row[10]


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
    code, out = run([], client, interactive=True, inputs=Inputs())
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
    code, _ = run([], client, interactive=True, inputs=Inputs("y"))
    assert code == 0 and client.created[0][1] == [1, 2]
    statuses = [r[STATUS] for r in read_report(workdir / "report.csv")[1:]]
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
    code, out = run(["--dry-run", "--interactive"], client, interactive=True, inputs=inputs)
    assert code == 0
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    entries = list(state["tracks"].values())
    assert entries[0]["status"] == "manual"
    assert entries[1]["status"] == "manual" and entries[1]["match"]["id"] == 1
    assert "Imagine Dragons Believer" in entries[1]["queries"]
    assert entries[2]["status"] == "auto"
    statuses = [r[STATUS] for r in read_report(workdir / "report.csv")[1:]]
    assert statuses == ["выбран вручную", "выбран вручную", "автоматически выбран"]


def test_skip_and_link(workdir):
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky", "Неизвестный — Песня"])
    client = FakeClient()
    inputs = Inputs("", "https://soundcloud.com/imaginedragons/1")
    run(["--dry-run", "--interactive"], client, interactive=True, inputs=inputs)
    entries = list(json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"].values())
    assert entries[0]["status"] == "skipped"
    assert entries[1]["status"] == "manual" and entries[1]["match"]["id"] == 1


def test_legacy_pending_entry_is_resolved_automatically_from_cache(workdir):
    """state.json старой версии: трек ждал ручного выбора (--no-input). Теперь он решается сам, без поиска."""
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky | 6:09"])
    legacy = {"version": 1, "playlists": [], "pending_create": None, "tracks": {
        "daft punk — get lucky #1": {
            "source": "Daft Punk — Get Lucky", "status": "pending", "match": None,
            "queries": ["Daft Punk Get Lucky"],
            "candidates": [dict(CATALOG[4], url=CATALOG[4]["permalink_url"], username="Daft Punk", duration=248),
                           dict(CATALOG[3], url=CATALOG[3]["permalink_url"], username="Daft Punk", duration=369)],
        }}}
    for c in legacy["tracks"]["daft punk — get lucky #1"]["candidates"]:
        for k in ("kind", "user", "permalink_url", "full_duration"):
            c.pop(k)
    (workdir / "state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    client = FakeClient()
    code, _ = run(["--dry-run"], client, interactive=True, inputs=Inputs())
    assert code == 0 and client.searches == []
    entry = list(json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"].values())[0]
    assert entry["status"] == "auto" and entry["match"]["id"] == 4 and entry["algo"] >= 2


def test_review_reasks_skipped(workdir):
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky"])
    run(["--dry-run", "--interactive"], FakeClient(), interactive=True, inputs=Inputs(""))
    client = FakeClient()
    run(["--dry-run", "--interactive", "--review"], client, interactive=True, inputs=Inputs("1"))
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
    run(["--dry-run", "--interactive"], FakeClient(), interactive=True, inputs=Inputs())
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    client = FakeClient()
    inputs = Inputs("Мой перенос", "y")
    run(["--interactive"], client, interactive=True, inputs=inputs)
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
    run(["--dry-run", "--interactive"], client, interactive=True, inputs=Inputs("/1979", "1"))
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
    assert row[1].startswith("'=cmd")
    assert row[4].startswith("'@SUM")


BROKEN_LIST = [
    "@ФакШиза — 320 | 1:13",
    "CLONNEX — ~128 | 1:54",
    "emothug — 256 kbps | 0:52",
    "Imagine Dragons — Believer | 3:24",
    "Arctic Monkeys — 505",
]


def test_broken_titles_are_not_searched_and_reported(workdir):
    write_tracks(workdir / "tracks.txt", BROKEN_LIST)
    client = FakeClient()
    code, out = run(["--dry-run"], client)
    assert code == 0
    # Ни поиска по «320», ни поиска по одному исполнителю.
    assert not any("ФакШиза" in q or "CLONNEX" in q or "emothug" in q for q in client.searches)
    assert any("Believer" in q for q in client.searches)
    assert any("505" in q for q in client.searches)  # настоящая песня ищется как обычно
    assert "битым названием" in out and "строки 1, 2, 3" in out
    assert "Битые названия:        3" in out
    rows = read_report(workdir / "report.csv")[1:]
    assert [r[STATUS] for r in rows[:3]] == ["битое название"] * 3
    assert rows[0][4:8] == ["", "", "", ""]  # ничего не «нашлось»
    assert rows[3][STATUS] == "автоматически выбран"


def stale_state(workdir, key, source):
    """Старый state.json, где для «320» по ошибке «нашёлся» чужой трек исполнителя."""
    stale = {
        "version": 1,
        "playlists": [],
        "pending_create": None,
        "tracks": {
            key: {
                "source": source, "status": "auto", "queries": [source.replace(" —", "")],
                "match": {"id": 3, "title": "Bad Romance (Skrillex Remix)", "username": "Lady Gaga",
                          "url": "u", "score": 90},
                "candidates": [],
            }
        },
    }
    (workdir / "state.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")


def test_stale_match_for_broken_title_is_overridden_and_not_added(workdir, monkeypatch):
    """Старый state.json мог содержать «совпадение» для «320» — оно не должно попасть в плейлист."""
    # «~128» рядом — признак бага экспорта, поэтому и голое «320» считается битрейтом.
    write_tracks(workdir / "tracks.txt", BROKEN_LIST[:2] + BROKEN_LIST[3:4])
    stale_state(workdir, "@факшиза — 320 #1", "@ФакШиза — 320")
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    client = FakeClient()
    code, _ = run(["--title", "Из VK"], client, interactive=True, inputs=Inputs("y"))
    assert code == 0
    assert client.created == [("Из VK", [1], "private")]
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["tracks"]["@факшиза — 320 #1"]["status"] == "broken_title"
    assert state["tracks"]["@факшиза — 320 #1"]["match"] is None

    again = FakeClient()
    run(["--dry-run"], again, interactive=True, inputs=Inputs())
    assert again.searches == []  # битое название не ищется и без --review не переспрашивается

    # В --review можно найти такой трек вручную; Enter оставляет «битое название» без поиска.
    review = FakeClient()
    run(["--dry-run", "--interactive", "--review"], review, interactive=True, inputs=Inputs("", ""))
    assert review.searches == []
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["tracks"]["@факшиза — 320 #1"]["status"] == "broken_title"


def test_stale_match_for_lone_number_title_is_rechecked(workdir, monkeypatch):
    """Одиночное «Artist — 320» ищется как песня, но старое «совпадение» по исполнителю не выживает."""
    write_tracks(workdir / "tracks.txt", BROKEN_LIST[:1] + BROKEN_LIST[3:4])
    stale_state(workdir, "@факшиза — 320 #1", "@ФакШиза — 320")
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    kokain = sc_track(40, "Кокаин", "@ФакШиза", 73)

    class ByArtist(FakeClient):
        def search_tracks(self, query, limit=20):
            if "ФакШиза" in query:
                self.searches.append(query)
                return [kokain]
            return super().search_tracks(query, limit)

    client = ByArtist()
    code, _ = run(["--title", "Из VK", "--yes"], client, interactive=False)
    assert code == 0
    assert client.created == [("Из VK", [1], "private")]  # «Кокаин» вместо «320» не добавлен
    entry = json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"]["@факшиза — 320 #1"]
    assert entry["status"] in ("not_found", "low_confidence") and entry["match"] is None


def test_lone_number_title_matches_only_the_same_number(workdir):
    write_tracks(workdir / "tracks.txt", ["Some Band — 128"])
    catalog = [sc_track(6, "Another Song", "Some Band", 200), sc_track(7, "128", "Some Band", 201)]

    class ByQuery(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return self.catalog

    client = ByQuery(catalog=catalog)
    run(["--dry-run"], client)
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["tracks"]["some band — 128 #1"]["status"] == "auto"
    assert state["tracks"]["some band — 128 #1"]["match"]["id"] == 7

    (workdir / "state.json").unlink()
    only_other = ByQuery(catalog=catalog[:1])
    run(["--dry-run"], only_other)
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["tracks"]["some band — 128 #1"]["match"] is None
    assert all(q.strip() != "128" for q in only_other.searches)  # по одному числу не ищем


def test_manual_choice_for_number_title_is_kept_even_in_bitrate_context(workdir):
    write_tracks(workdir / "tracks.txt", ["Some Band — 128", "Other — ~320"])
    manual = {"version": 1, "playlists": [], "pending_create": None, "tracks": {
        "some band — 128 #1": {"source": "Some Band — 128", "status": "manual", "queries": ["Some Band 128"],
                               "match": {"id": 7, "title": "128", "username": "Some Band", "url": "u", "score": 100},
                               "candidates": []}}}
    (workdir / "state.json").write_text(json.dumps(manual, ensure_ascii=False), encoding="utf-8")
    later = FakeClient()
    run(["--dry-run"], later)
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["tracks"]["some band — 128 #1"]["status"] == "manual" and later.searches == []


def test_title_made_only_of_noise_is_not_searched_by_artist_alone():
    from vk2sc.cli import build_queries
    from vk2sc.tracks import Track

    queries = build_queries(Track("Imagine Dragons", "(Official Audio)"))
    assert all(q.strip() != "Imagine Dragons" for q in queries)


def test_start_banner_counts_broken_titles_separately(workdir):
    write_tracks(workdir / "tracks.txt", BROKEN_LIST)
    _, out = run(["--dry-run"], FakeClient())
    assert "Искать впервые: 2." in out and "Ждут выбора" not in out


# ---- Полностью автоматический режим ----


class NoInput:
    """input(), который нельзя вызывать."""

    def __init__(self):
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        raise AssertionError(f"input() вызван: {prompt}")


def test_search_variants_strip_vk_junk():
    from vk2sc.cli import search_variants
    from vk2sc.tracks import Track

    def queries(title):
        return dict((label, q) for label, q in search_variants(Track("Artist", title)))

    q = queries("Song (VK.COM)")
    assert q["исходный"] == "Artist Song (VK.COM)" and q["очищенный"] == "Artist Song"
    assert queries("Song [Reupload]")["очищенный"] == "Artist Song"
    assert queries("Song (VK.COM) [Reupload]")["очищенный"] == "Artist Song"
    assert "Remix" in queries("Song (Remix)")["исходный"]
    assert list(queries("Song (Remix)").values())[0] == "Artist Song (Remix)"
    assert all("Remix" in v for v in queries("Song (Remix)").values())  # версию из запроса не выбрасываем
    assert all("Live" in v for v in queries("Song (Live)").values())


def test_search_variants_order_limit_and_no_artist_only_query():
    from vk2sc.cli import MAX_QUERIES, search_variants
    from vk2sc.tracks import Track

    variants = search_variants(Track("Кино & Юрий Шевчук feat. Гость", "Группа крови (VK.COM)"))
    labels = [label for label, _ in variants]
    assert len(variants) <= MAX_QUERIES
    assert labels[:3] == ["исходный", "очищенный", "транслит"]
    assert "только название" in labels
    assert dict(variants)["основной исполнитель"] == "кино Группа крови"
    assert all(q.strip().lower() not in ("кино", "кино & юрий шевчук") for _, q in variants)
    # Простой трек без мусора: исходный и очищенный запрос совпадают — один запрос, не два.
    simple = search_variants(Track("Imagine Dragons", "Believer"))
    assert [label for label, _ in simple] == ["исходный", "название + исполнитель", "только название"]
    # Название-число не ищется само по себе.
    assert all(q != "505" for _, q in search_variants(Track("Arctic Monkeys", "505")))


def test_cascade_retries_with_clean_query_and_logs(workdir):
    write_tracks(workdir / "tracks.txt", ["JDFLAG — Track Name (VK.COM) | 3:00"])
    good = sc_track(11, "Track Name", "JDFLAG", 181)
    junk = sc_track(12, "JDFLAG - Track Name [Phonk Edition]", "someone", 176)

    class Picky(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return [junk] if "VK.COM" in query else [good, junk]

    client = Picky()
    code, out = run(["--dry-run"], client, interactive=True, inputs=NoInput())
    assert code == 0
    assert client.searches == ["JDFLAG Track Name (VK.COM)", "JDFLAG Track Name"]
    assert "  поиск: JDFLAG Track Name (VK.COM)" in out
    assert "результат недостаточно хороший" in out
    assert "повторный поиск (очищенный): JDFLAG Track Name" in out
    assert "лучший вариант: JDFLAG — Track Name" in out and "→ автоматически выбран" in out
    assert "  duration: +1 сек" in out and "  version conflict: нет" in out
    row = read_report(workdir / "report.csv")[1]
    assert row[1:4] == ["JDFLAG", "Track Name (VK.COM)", "Track Name"]
    assert row[4:7] == ["JDFLAG", "Track Name", good["permalink_url"]]
    assert row[STATUS] == "автоматически выбран" and row[9] == "очищенный"


def test_cascade_is_limited_and_unknown_track_is_skipped(workdir):
    write_tracks(workdir / "tracks.txt", ["Неизвестный & Другой — Несуществующая песня (VK.COM)"])
    client = FakeClient()
    code, out = run(["--dry-run"], client, interactive=True, inputs=NoInput())
    assert code == 0
    from vk2sc.cli import MAX_QUERIES
    assert 3 <= len(client.searches) <= MAX_QUERIES
    assert len(set(client.searches)) == len(client.searches)
    assert read_report(workdir / "report.csv")[1][STATUS] == "не найден"


def test_first_result_is_not_taken_blindly(workdir):
    write_tracks(workdir / "tracks.txt", ["Imagine Dragons — Believer | 3:24"])
    results = [
        sc_track(21, "Believer (Slowed + Reverb)", "Imagine Dragons", 260),
        sc_track(22, "Believer - Перевод на русском", "danon_", 211),
        sc_track(23, "Believer", "Imagine Dragons", 204),
    ]

    class Ordered(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return results

    client = Ordered()
    run(["--dry-run"], client, interactive=True, inputs=NoInput())
    entry = list(json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"].values())[0]
    assert entry["status"] == "auto" and entry["match"]["id"] == 23
    assert client.searches == ["Imagine Dragons Believer"]  # хорошее совпадение — больше не ищем


def test_doubtful_match_is_skipped_as_low_confidence(workdir):
    write_tracks(workdir / "tracks.txt", ["Imagine Dragons — Believer | 3:24"])

    class OnlySlowed(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return [sc_track(31, "Believer (slowed + reverb)", "Imagine Dragons", 210)]

    run(["--dry-run"], OnlySlowed(), interactive=True, inputs=NoInput())
    row = read_report(workdir / "report.csv")[1]
    assert row[STATUS] == "пропущен: низкая уверенность"
    assert row[5] == "Believer (slowed + reverb)"  # для проверки видно, что именно отвергнуто
    entry = list(json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"].values())[0]
    assert entry["status"] == "low_confidence" and entry["match"] is None


def test_default_run_several_versions_is_automatic(workdir):
    """Раньше «Get Lucky» без длительности спрашивал выбор — теперь решается сам."""
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky"])
    code, out = run(["--dry-run"], FakeClient(), interactive=True, inputs=NoInput())
    assert code == 0 and "Нужен ваш выбор" not in out
    entry = list(json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"].values())[0]
    assert entry["status"] == "auto" and entry["match"]["id"] == 4


def test_dry_run_never_asks(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC + ["Daft Punk — Get Lucky"])
    inputs = NoInput()
    client = FakeClient()
    code, out = run(["--dry-run"], client, interactive=True, inputs=inputs)
    assert code == 0 and inputs.prompts == []
    assert client.created == [] and client.updated == []
    assert "плейлист не создавался" in out


def test_yes_never_calls_input(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC + ["Daft Punk — Get Lucky"])
    inputs = NoInput()
    client = FakeClient()
    code, out = run(["--yes"], client, interactive=True, inputs=inputs)
    assert code == 0 and inputs.prompts == []
    assert client.created == [("Из VK", [1, 2, 3, 4], "private")]
    rows = read_report(workdir / "report.csv")[1:]
    assert [r[STATUS] for r in rows] == ["добавлен", "добавлен", "добавлен", "не найден", "добавлен"]

    # Плейлист удалили на сайте: с --yes он пересоздаётся тоже без вопросов.
    write_tracks(workdir / "tracks.txt", BASIC[:2])
    from vk2sc.soundcloud import PlaylistNotFound

    class Gone(FakeClient):
        def set_playlist_tracks(self, playlist_id, track_ids):
            raise PlaylistNotFound("нет")

    gone = Gone()
    code, out = run(["--yes"], gone, interactive=True, inputs=inputs)
    assert code == 0 and inputs.prompts == [] and gone.created == [("Из VK", [1, 2], "private")]


def test_yes_without_terminal(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    client = FakeClient()
    code, _ = run(["--yes", "--no-input"], client, interactive=True, inputs=NoInput())
    assert code == 0 and client.created == [("Из VK", [1], "private")]


def test_interactive_requires_terminal(workdir):
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    code, out = run(["--interactive"], FakeClient(), interactive=False)
    assert code == 1 and "--interactive" in out
    code, out = run(["--review"], FakeClient(), interactive=True, inputs=NoInput())
    assert code == 1 and "--review" in out


def test_state_resume_does_not_search_processed_tracks(workdir):
    write_tracks(workdir / "tracks.txt", BASIC)
    run(["--dry-run"], FakeClient(), interactive=True, inputs=NoInput())
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert {e["algo"] for e in state["tracks"].values()} == {MATCHING_VERSION}
    # Все решения (найден, не найден) остаются; второй запуск не делает ни одного запроса.
    again = FakeClient()
    code, out = run(["--dry-run"], again, interactive=True, inputs=NoInput())
    assert code == 0 and again.searches == []
    assert "Решено раньше: 4" in out


def test_old_algorithm_entries(workdir):
    """Найденное старой версией не трогаем без --rematch; ненайденное — перепроверяем новыми запросами."""
    write_tracks(workdir / "tracks.txt", ["Imagine Dragons — Believer (VK.COM)", "Кино — Группа крови"])
    old = {"version": 1, "playlists": [], "pending_create": None, "tracks": {
        "imagine dragons — believer (vk.com) #1": {
            "source": "Imagine Dragons — Believer (VK.COM)", "status": "not_found", "match": None,
            "queries": ["Imagine Dragons Believer (VK.COM)"], "candidates": []},
        "кино — группа крови #1": {
            "source": "Кино — Группа крови", "status": "auto", "queries": ["Кино Группа крови"],
            "match": {"id": 99, "title": "Группа крови (cover)", "username": "Кино fan", "url": "u", "score": 86},
            "candidates": [{"id": 99, "title": "Группа крови (cover)", "username": "Кино fan", "url": "u",
                            "score": 86}]},
    }}
    (workdir / "state.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    class Exact(FakeClient):
        def search_tracks(self, query, limit=20):
            if "VK.COM" in query:  # по «грязному» запросу SoundCloud ничего не находит
                self.searches.append(query)
                return []
            return super().search_tracks(query, limit)

    client = Exact()
    run(["--dry-run"], client, interactive=True, inputs=NoInput())
    assert client.searches == ["Imagine Dragons Believer"]  # «грязный» запрос уже был, повторять не стали
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"]
    assert state["imagine dragons — believer (vk.com) #1"]["status"] == "auto"
    assert state["кино — группа крови #1"]["match"]["id"] == 99  # без --rematch не тронут

    rematch = FakeClient()
    run(["--dry-run", "--rematch"], rematch, interactive=True, inputs=NoInput())
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"]
    assert state["кино — группа крови #1"]["match"]["id"] == 2 and state["кино — группа крови #1"]["algo"] == MATCHING_VERSION
    # Кэш (кавер) не подошёл: добраны только запросы, которых ещё не было.
    assert rematch.searches == ["kino gruppa krovi", "Группа крови Кино"]
    assert all("Believer" not in q for q in rematch.searches)  # новое решение не пересчитывается


def test_rematch_never_touches_manual_choice(workdir):
    write_tracks(workdir / "tracks.txt", ["Кино — Группа крови"])
    manual = {"version": 1, "playlists": [], "pending_create": None, "tracks": {
        "кино — группа крови #1": {"source": "Кино — Группа крови", "status": "manual", "queries": [],
                                   "match": {"id": 77, "title": "Группа крови", "username": "x", "url": "u"},
                                   "candidates": []}}}
    (workdir / "state.json").write_text(json.dumps(manual, ensure_ascii=False), encoding="utf-8")
    client = FakeClient()
    run(["--dry-run", "--rematch"], client, interactive=True, inputs=NoInput())
    assert client.searches == []
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"]
    assert state["кино — группа крови #1"]["match"]["id"] == 77


def test_verbose_lists_candidates(workdir):
    write_tracks(workdir / "tracks.txt", ["Daft Punk — Get Lucky"])
    _, quiet = run(["--dry-run"], FakeClient(), interactive=True, inputs=NoInput())
    (workdir / "state.json").unlink()
    _, loud = run(["--dry-run", "-v"], FakeClient(), interactive=True, inputs=NoInput())
    assert "кандидаты:" not in quiet and "Get Lucky (Radio Edit)" not in quiet
    assert "кандидаты:" in loud and "Get Lucky (Radio Edit)" in loud


def test_yo_variant_is_not_deduplicated_away():
    from vk2sc.cli import search_variants
    from vk2sc.tracks import Track

    variants = dict(search_variants(Track("Ёлка", "Прованс")))
    assert variants["исходный"] == "Ёлка Прованс" and variants["без ё"] == "Елка Прованс"


def test_rematch_dry_run_recalculates_v2_results_without_new_export(workdir):
    """Сценарий: python -m vk2sc --rematch --dry-run -v поверх state.json прошлой версии."""
    write_tracks(workdir / "tracks.txt", [
        "Bladee, Ecco2k — Gotham CityGirls Just Want to Have Fun | 2:14",
        "Кино — Группа крови",
        "Imagine Dragons — Believer",
    ])
    girls = {"id": 50, "title": "Girls just want to have fun", "username": "Bladee, Ecco2k",
             "url": "https://soundcloud.com/bladee/girls", "duration": 134, "score": 80}
    remake = {"id": 51, "title": "Bladee & Ecco2k - Girls Just Want to Have Fun (instrumental remake)",
              "username": "spectre", "url": "https://soundcloud.com/spectre/remake", "duration": 134, "score": 50}
    reupload = {"id": 52, "title": "bladee & ecco2k - girls just want to have fun", "username": "tk0",
                "url": "https://soundcloud.com/tk0/girls", "duration": 150, "score": 59}
    old = {"version": 1, "playlists": [], "pending_create": None, "tracks": {
        "bladee, ecco2k — gotham citygirls just want to have fun #1": {
            "source": "Bladee, Ecco2k — Gotham CityGirls Just Want to Have Fun", "status": "low_confidence",
            "match": None, "reason": "название заметно отличается (80)", "algo": 2,
            "queries": ["Bladee, Ecco2k Gotham CityGirls Just Want to Have Fun"],
            "candidates": [girls, remake, reupload]},
        "кино — группа крови #1": {
            "source": "Кино — Группа крови", "status": "manual", "algo": 2, "queries": [],
            "match": {"id": 77, "title": "Группа крови", "username": "x", "url": "u"}, "candidates": []},
        "imagine dragons — believer #1": {
            "source": "Imagine Dragons — Believer", "status": "auto", "algo": 2, "queries": ["Imagine Dragons Believer"],
            "match": dict(CATALOG[0], url=CATALOG[0]["permalink_url"], username="Imagine Dragons", score=100),
            "candidates": [{"id": 1, "title": "Believer", "username": "Imagine Dragons",
                            "url": CATALOG[0]["permalink_url"], "duration": 204, "score": 100}]},
    }}
    (workdir / "state.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    client = FakeClient()
    code, out = run(["--rematch", "--dry-run", "-v"], client, interactive=True, inputs=NoInput())
    assert code == 0 and client.searches == []  # хватило сохранённых кандидатов
    assert "→ автоматически выбран: точный исполнитель + длительность" in out
    assert "  artist: 100 (тот же набор исполнителей)" in out and "  duration: +0 сек" in out
    assert "  version conflict: нет" in out and "  margin: +" in out
    assert "кандидаты:" in out and "другая версия" in out

    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))["tracks"]
    bladee = state["bladee, ecco2k — gotham citygirls just want to have fun #1"]
    assert bladee["status"] == "auto" and bladee["match"]["id"] == 50 and bladee["algo"] == MATCHING_VERSION
    assert bladee["metrics"]["artist"] == 100 and bladee["metrics"]["duration_delta"] == 0
    assert state["кино — группа крови #1"]["match"]["id"] == 77  # ручной выбор не тронут
    assert state["imagine dragons — believer #1"]["algo"] == MATCHING_VERSION  # пересчитан по --rematch

    row = read_report(workdir / "report.csv")[1]
    assert row[STATUS] == "автоматически выбран" and row[5] == "Girls just want to have fun"
    assert row[10].startswith("точный исполнитель") and row[13] == "0" and row[14] == "нет"


def test_low_confidence_log_shows_features(workdir):
    write_tracks(workdir / "tracks.txt", ["Artist — Midnight City Lights | 3:20"])

    class Slowed(FakeClient):
        def search_tracks(self, query, limit=20):
            self.searches.append(query)
            return [sc_track(60, "City Lights (Slowed + Reverb)", "Artist", 200)]

    code, out = run(["--dry-run"], Slowed(), interactive=True, inputs=NoInput())
    assert "лучший вариант: Artist — City Lights (Slowed + Reverb)" in out
    assert "  version conflict: да" in out
    assert "→ пропущен: низкая уверенность (другая версия)" in out
    row = read_report(workdir / "report.csv")[1]
    assert row[STATUS] == "пропущен: низкая уверенность" and row[14] == "да"


# ---- Создание плейлистов через браузер (DataDome) ----


def test_datadome_block_falls_back_to_browser_script(workdir, monkeypatch):
    """Реальный случай: /me проходит, а POST /playlists получает 403 от DataDome."""
    from vk2sc.soundcloud import BlockedError

    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:3])

    class Blocked(FakeClient):
        def create_playlist(self, title, track_ids, sharing="private"):
            raise BlockedError("SoundCloud пропускает изменения в аккаунте только из браузера. "
                               "Токен при этом в порядке.")

    client = Blocked()
    inputs = Inputs("y")
    code, out = run(["--title", "Из VK"], client, interactive=True, inputs=inputs)
    assert code == 3 and inputs.prompts == ["Продолжить? [y/N]: "]
    assert "Токен при этом в порядке" in out and "устарел" not in out
    assert "soundcloud_playlists.js" in out and "вошли в аккаунт tester" in out and "allow pasting" in out
    script = (workdir / "soundcloud_playlists.js").read_text(encoding="utf-8-sig")
    assert '"title":"Из VK","tracks":[1,2,3]' in script and "secret-token-value" not in script
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["playlists"] == [] and state["pending_create"] is None


def test_browser_flag_needs_no_token_and_changes_nothing(workdir):
    write_tracks(workdir / "tracks.txt", BASIC[:2])
    run(["--dry-run"], FakeClient())
    client = FakeClient()
    code, out = run(["--browser", "--title", "Мой VK"], client, interactive=True, inputs=NoInput())
    assert code == 0 and client.created == [] and client.updated == [] and client.oauth_token is None
    script = (workdir / "soundcloud_playlists.js").read_text(encoding="utf-8-sig")
    assert '"title":"Мой VK","tracks":[1,2]' in script and "«Мой VK» — 2 треков" in out


def test_browser_script_splits_into_parts_of_500(workdir):
    catalog = [sc_track(i, f"Song number {i}", "Band") for i in range(1, 503)]
    write_tracks(workdir / "tracks.txt", [f"Band — Song number {i}" for i in range(1, 503)])

    class ExactClient(FakeClient):
        def search_tracks(self, query, limit=20):
            return [t for t in self.catalog if f"Band {t['title']}" == query]

    code, _ = run(["--browser"], ExactClient(catalog), interactive=True, inputs=NoInput())
    script = (workdir / "soundcloud_playlists.js").read_text(encoding="utf-8-sig")
    assert code == 0 and '"title":"Из VK","tracks":[1,2,3,' in script and '"title":"Из VK (2)","tracks":[501,502]' in script


def test_playlists_created_in_browser_are_adopted(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:3])
    client = FakeClient()
    client.remote_playlists = [
        {"id": 900, "title": "Из VK", "description": "мой старый плейлист", "created_at": "2026-09-01T00:00:00Z"},
        {"id": 901, "title": "Из VK", "description": "Перенесено из VK (vk2sc)", "created_at": "2026-09-27T12:00:00Z",
         "permalink_url": "https://soundcloud.com/tester/sets/iz-vk", "sharing": "private",
         "tracks": [{"id": 1}, {"id": 2}, {"id": 3}], "track_count": 3},
    ]
    code, out = run(["--title", "Из VK"], client, interactive=True, inputs=NoInput())
    assert code == 0 and client.created == [] and client.updated == []
    assert "созданный в браузере" in out and "уже в актуальном состоянии" in out
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["playlists"][0]["id"] == 901  # чужой «Из VK» без метки не тронут
    assert [r[STATUS] for r in read_report(workdir / "report.csv")[1:]] == ["добавлен"] * 3


def test_same_title_without_mark_is_not_adopted(workdir, monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_OAUTH_TOKEN", "secret-token-value")
    write_tracks(workdir / "tracks.txt", BASIC[:1])
    client = FakeClient()
    client.remote_playlists = [{"id": 900, "title": "Из VK", "description": "", "tracks": [{"id": 1}], "track_count": 1}]
    code, _ = run(["--title", "Из VK", "--yes"], client, interactive=True, inputs=NoInput())
    assert code == 0 and client.created == [("Из VK", [1], "private")]
