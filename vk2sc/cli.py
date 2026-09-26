"""Точка входа: python -m vk2sc [--dry-run] [--tracks tracks.txt] ..."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import report
from .matching import AUTO_THRESHOLD, Candidate, Decision, Scored, decide, rank, score_candidate
from .normalize import (
    clean_artist_for_query,
    clean_title_for_query,
    fold,
    has_cyrillic,
    parse_artist,
    simplify,
    translit,
)
from .soundcloud import (
    AuthError,
    PlaylistNotFound,
    Redactor,
    RetryExhausted,
    SoundCloudClient,
    SoundCloudError,
    playlist_track_count,
)
from .state import AUTO, DECIDED, MANUAL, MATCHED, NOT_FOUND, PENDING, SKIPPED, State, StateError
from .tracks import Track, TracksFileError, format_duration, read_tracks, track_keys

log = logging.getLogger("vk2sc")

DEFAULT_TITLE = "Из VK"
PLAYLIST_LIMIT = 500  # лимит SoundCloud на число треков в плейлисте
SHOW_CANDIDATES = 5
KEEP_CANDIDATES = 10
YES = {"y", "yes", "д", "да"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vk2sc",
        description="Ищет треки из списка VK в SoundCloud и собирает из них новый плейлист.",
    )
    p.add_argument("--tracks", type=Path, default=Path("tracks.txt"), help="файл со списком (по умолчанию tracks.txt)")
    p.add_argument("--dry-run", action="store_true", help="только поиск и отчёт: плейлист не создаётся, токен не нужен")
    p.add_argument("--title", help=f"название плейлиста (иначе спросит при запуске, по умолчанию «{DEFAULT_TITLE}»)")
    p.add_argument("--public", action="store_true", help="сделать плейлист публичным (по умолчанию приватный)")
    p.add_argument("--no-input", action="store_true",
                   help="ничего не спрашивать: сомнительные треки откладываются до следующего запуска")
    p.add_argument("--review", action="store_true", help="заново предложить выбор для пропущенных и ненайденных")
    p.add_argument("--threshold", type=int, default=AUTO_THRESHOLD,
                   help=f"порог уверенности для автодобавления, 0–100 (по умолчанию {AUTO_THRESHOLD})")
    p.add_argument("--delay", type=float, default=None,
                   help="пауза между запросами, сек (по умолчанию 1.5, минимум 1)")
    p.add_argument("--state", type=Path, default=Path("state.json"), help="файл прогресса (по умолчанию state.json)")
    p.add_argument("--report", type=Path, default=Path("report.csv"), help="файл отчёта (по умолчанию report.csv)")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    return p


def setup_logging(verbose: bool, log_path: Path = Path("vk2sc.log")) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("  ! %(message)s"))
    console.addFilter(Redactor())
    root.addHandler(console)
    try:
        fh = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        return
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh.addFilter(Redactor())
    root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_queries(track: Track) -> list[str]:
    """Основной запрос и, если есть смысл, один запасной."""
    artist = clean_artist_for_query(track.artist)
    title = clean_title_for_query(track.title)
    main = f"{artist} {title}".strip()
    alt = None
    if "ё" in main.lower():
        alt = main.replace("ё", "е").replace("Ё", "Е")
    elif has_cyrillic(fold(main)):
        alt = translit(simplify(main))
    else:
        names = parse_artist(artist).names
        if len(names) > 1:
            alt = f"{names[0]} {title}"
    return [main] + ([alt] if alt and alt != main else [])


class Runner:
    def __init__(self, args: argparse.Namespace, tracks: list[Track], state: State,
                 client: SoundCloudClient, interactive: bool,
                 input_fn: Callable[[str], str] = input, out: Callable[..., None] = print) -> None:
        self.args = args
        self.tracks = tracks
        self.keys = track_keys(tracks)
        self.state = state
        self.client = client
        self.interactive = interactive
        self.input = input_fn
        self.out = out
        self.search_count = 0

    # ---------- общий сценарий ----------

    def run(self) -> int:
        code = 0
        log.info("=== Запуск: треков %d, dry-run=%s, интерактивно=%s ===",
                 len(self.tracks), self.args.dry_run, self.interactive)
        try:
            self.choose_title()
            self.match_all()
            if not self.args.dry_run:
                code = self.sync_playlists()
        except (KeyboardInterrupt, EOFError):
            self.out("\nПрервано. Прогресс сохранён в " + str(self.state.path) + " — запустите снова, чтобы продолжить.")
            code = 130
        except RetryExhausted as e:
            self.out(f"\nSoundCloud не отвечает или ограничил запросы: {e}")
            self.out(f"Прогресс сохранён в {self.state.path}. Подождите 15–30 минут и запустите снова.")
            code = 2
        except AuthError as e:
            self.out(f"\nОшибка доступа: {e}")
            code = 1
        except SoundCloudError as e:
            self.out(f"\nОшибка SoundCloud: {e}")
            self.out(f"Прогресс сохранён в {self.state.path}.")
            code = 1
        finally:
            self.finish()
        return code

    def finish(self) -> None:
        _, duplicates = self.desired_ids()
        added = {tid for pl in self.state.playlists for tid in pl.get("track_ids", [])}
        summary = report.build(self.tracks, self.keys, self.state, self.args.dry_run, duplicates, added)
        try:
            report.write_csv(self.args.report, summary)
        except OSError as e:
            self.out(f"Не удалось записать отчёт {self.args.report}: {e}")
        report.print_summary(summary, self.args.dry_run, self.args.report, out=self.out)
        if self.args.dry_run:
            self.out("Режим --dry-run: плейлист не создавался и не менялся.")

    def choose_title(self) -> None:
        st = self.state
        if st.playlists:
            if self.args.title and self.args.title != st.playlist_title:
                self.out(f"Плейлист уже создан под названием «{st.playlist_title}», название не меняю.")
            return
        if self.args.title:
            st.playlist_title = self.args.title
            st.save()
        elif not st.playlist_title and self.interactive and not self.args.dry_run:
            # В --dry-run не спрашиваем и ничего не запоминаем: иначе при настоящем
            # запуске вопрос о названии уже не прозвучит.
            answer = self.input(f"Название нового плейлиста [{DEFAULT_TITLE}]: ").strip()
            st.playlist_title = answer or DEFAULT_TITLE
            st.save()

    # ---------- поиск и сопоставление ----------

    def needs_work(self, entry: Optional[dict]) -> bool:
        if entry is None or entry["status"] == PENDING:
            return True
        return self.args.review and self.interactive and entry["status"] in (SKIPPED, NOT_FOUND)

    def match_all(self) -> None:
        todo = [i for i, k in enumerate(self.keys) if self.needs_work(self.state.get(k))]
        entries = [self.state.get(k) for k in self.keys]
        fresh = sum(1 for e in entries if e is None)
        waiting = len(todo) - fresh
        self.out(
            f"Треков в списке: {len(self.tracks)}. Решено раньше: {len(self.tracks) - len(todo)}. "
            f"Искать впервые: {fresh}." + (f" Ждут выбора (из кэша, без нового поиска): {waiting}." if waiting else "")
        )
        if todo and not self.interactive:
            self.out("Работаю без вопросов: сомнительные совпадения отложу до запуска в интерактивном режиме.")
        for i in todo:
            self.match_one(i)

    def match_one(self, i: int) -> None:
        track, key = self.tracks[i], self.keys[i]
        entry = self.state.get(key)
        self.out(f"\n[{i + 1}/{len(self.tracks)}] {track.display}")

        if entry and entry.get("candidates") is not None:
            # Уже искали: берём кэш, повторно в SoundCloud не ходим.
            candidates = [Candidate.from_dict(c) for c in entry["candidates"]]
            queries = list(entry.get("queries", []))
        else:
            candidates, queries = self.search(track)
        ranked = rank(track, candidates)
        decision = decide(track, ranked, self.args.threshold)

        if decision.kind == "auto":
            best = decision.best
            self.out(f"  ✓ {report.describe(best.candidate)} — {best.score}%")
            self.save(key, track, AUTO, best, ranked, queries)
        elif not self.interactive:
            if decision.kind == "ask":
                self.out(f"  ? отложено: {decision.reason}, лучший вариант {decision.best.score}%")
                self.save(key, track, PENDING, None, ranked, queries)
            else:
                self.out("  ✗ не найдено")
                self.save(key, track, NOT_FOUND, None, ranked, queries)
        else:
            status, chosen, ranked = self.ask(track, decision, queries)
            self.save(key, track, status, chosen, ranked, queries)

    def search(self, track: Track) -> tuple[list[Candidate], list[str]]:
        collected: list[Candidate] = []
        used: list[str] = []
        for q in build_queries(track):
            used.append(q)
            log.debug("Поиск: %s", q)
            collected += [Candidate.from_api(t) for t in self.client.search_tracks(q)]
            self.search_count += 1
            decision = decide(track, rank(track, collected), self.args.threshold)
            # Перезаливку от случайного пользователя считаем достаточной, только если
            # запасной запрос (транслит, без «ё») не нашёл загрузку с аккаунта исполнителя.
            if decision.kind == "auto" and decision.best.uploader_match:
                break
        return collected, used

    def save(self, key: str, track: Track, status: str, chosen: Optional[Scored],
             ranked: list[Scored], queries: list[str]) -> None:
        match = None
        if chosen is not None:
            match = chosen.candidate.to_dict()
            match["score"] = chosen.score
        candidates = []
        for s in ranked[:KEEP_CANDIDATES]:
            d = s.candidate.to_dict()
            d["score"] = s.score
            candidates.append(d)
        self.state.put(key, {
            "source": track.display,
            "line": track.line_no,
            "status": status,
            "match": match,
            "candidates": candidates,
            "queries": queries,
        })

    # ---------- диалог ----------

    def show(self, shown: list[Scored]) -> None:
        for n, s in enumerate(shown, 1):
            c = s.candidate
            dur = format_duration(c.duration) or "?:??"
            flags = f"  [{c.flags}]" if c.flags else ""
            self.out(f"   {n}. {c.display}  {dur}  {s.score}%  {c.url}{flags}")

    def ask(self, track: Track, decision: Decision, queries: list[str]) -> tuple[str, Optional[Scored], list[Scored]]:
        ranked = decision.ranked
        shown = ranked[:SHOW_CANDIDATES] if decision.kind == "ask" else []
        skip_status = SKIPPED if shown else NOT_FOUND
        if shown:
            self.out(f"  Нужен ваш выбор: {decision.reason}.")
        else:
            self.out("  Ничего похожего не нашлось.")
        while True:
            if shown:
                self.show(shown)
            answer = self.input("  Номер — выбрать, Enter — пропустить, текст или ссылка — искать иначе: ").strip()
            if not answer:
                self.out("  → пропущен" if skip_status == SKIPPED else "  → не найден")
                return skip_status, None, ranked
            if answer.isdigit() and 1 <= int(answer) <= len(shown):
                chosen = shown[int(answer) - 1]
                self.out(f"  → выбран: {chosen.candidate.display}")
                return MANUAL, chosen, ranked
            if answer.startswith("/"):
                answer = answer[1:].strip()  # «/1979» — искать число, а не выбрать вариант
                if not answer:
                    continue
            elif answer.startswith(("http://", "https://")) or "soundcloud.com/" in answer:
                data = self.client.resolve(answer if answer.startswith("http") else "https://" + answer)
                if not data or data.get("kind") != "track":
                    self.out("  По этой ссылке трек не найден.")
                    continue
                chosen = score_candidate(track, Candidate.from_api(data))
                self.out(f"  → выбран по ссылке: {chosen.candidate.display}")
                return MANUAL, chosen, [chosen] + [s for s in ranked if s.candidate.id != chosen.candidate.id]
            queries.append(answer)
            found = [Candidate.from_api(t) for t in self.client.search_tracks(answer)]
            self.search_count += 1
            if not found:
                self.out("  По этому запросу ничего нет. Попробуйте другой или нажмите Enter.")
                continue
            new_ranked = rank(track, found)
            known = {s.candidate.id for s in new_ranked}
            ranked = new_ranked + [s for s in ranked if s.candidate.id not in known]
            shown = new_ranked[:SHOW_CANDIDATES]
            skip_status = SKIPPED
            self.out(f"  Результаты по запросу «{answer}»:")

    # ---------- плейлисты ----------

    def desired_ids(self) -> tuple[list[int], set[str]]:
        """Треки для плейлиста в порядке списка VK, без повторов."""
        ids: list[int] = []
        seen: set[int] = set()
        duplicates: set[str] = set()
        for key in self.keys:
            e = self.state.get(key)
            if not e or e["status"] not in MATCHED or not e.get("match"):
                continue
            tid = int(e["match"]["id"])
            if tid in seen:
                duplicates.add(key)
                continue
            seen.add(tid)
            ids.append(tid)
        return ids, duplicates

    def sync_playlists(self) -> int:
        ids, _ = self.desired_ids()
        pending = sum(1 for k in self.keys if (self.state.get(k) or {}).get("status") == PENDING)
        if pending:
            self.out(f"\n{pending} трек(ов) ждут ручного выбора — их можно добрать позже, запустив скрипт без --no-input.")
        if not ids:
            self.out("\nНе найдено ни одного трека для плейлиста.")
            return 0
        token = os.environ.get("SOUNDCLOUD_OAUTH_TOKEN", "").strip()
        if not token:
            self.out("\nДля создания плейлиста нужен SOUNDCLOUD_OAUTH_TOKEN в файле .env (см. README).")
            self.out("Результаты поиска сохранены: после добавления токена поиск повторяться не будет.")
            return 1
        self.client.set_oauth_token(token)
        me = self.client.me()
        if self.state.pending_create:
            self.recover_pending_create(me)

        title = self.state.playlist_title or DEFAULT_TITLE
        sharing = "public" if self.args.public else "private"
        chunks = [ids[i:i + PLAYLIST_LIMIT] for i in range(0, len(ids), PLAYLIST_LIMIT)]
        plan = []
        for k, chunk in enumerate(chunks):
            if k < len(self.state.playlists):
                pl = self.state.playlists[k]
                if pl.get("track_ids") != chunk:
                    plan.append(("update", k, pl["title"], chunk))
            else:
                plan.append(("create", k, title if k == 0 else f"{title} ({k + 1})", chunk))
        for pl in self.state.playlists[len(chunks):]:
            self.out(f"Плейлист «{pl['title']}» больше не нужен для этого списка — не трогаю его.")
        if not plan:
            self.out("\nПлейлисты уже в актуальном состоянии, менять нечего.")
            return 0

        kind = "публичный" if sharing == "public" else "приватный"
        self.out(f"\nАккаунт SoundCloud: {me.get('username', '?')}")
        for action, k, name, chunk in plan:
            if action == "create":
                self.out(f"  • создать {kind} плейлист «{name}» — {len(chunk)} треков")
            else:
                before = len(self.state.playlists[k].get("track_ids") or [])
                self.out(f"  • обновить свой плейлист «{name}» — было {before}, станет {len(chunk)} треков")
        if not self.interactive:
            self.out("Без подтверждения ничего не создаю. Запустите без --no-input в терминале.")
            return 1
        if self.input("Продолжить? [y/N]: ").strip().lower() not in YES:
            self.out("Отменено, в аккаунте ничего не изменилось.")
            return 0

        for action, k, name, chunk in plan:
            if action == "create":
                self.state.pending_create = {"title": name, "started_at": time.time()}
                self.state.save()
                data = self.client.create_playlist(name, chunk, sharing)
                pl = {"id": int(data["id"]), "title": name, "url": data.get("permalink_url"),
                      "sharing": sharing, "track_ids": chunk}
                self.state.playlists.append(pl)
                self.state.pending_create = None
                self.state.save()
            else:
                pl = self.state.playlists[k]
                try:
                    data = self.client.set_playlist_tracks(pl["id"], chunk)
                except PlaylistNotFound:
                    self.out(f"Плейлист «{pl['title']}» не найден в аккаунте — видимо, его удалили на сайте.")
                    if self.input("Создать его заново? [y/N]: ").strip().lower() not in YES:
                        self.out("Пропускаю. Остальные изменения сохранены.")
                        continue
                    data = self.client.create_playlist(pl["title"], chunk, sharing)
                    pl.update(id=int(data["id"]), url=data.get("permalink_url"), sharing=sharing)
                pl["track_ids"] = chunk
                self.state.save()
            self.verify(pl, data, chunk)
        return 0

    def verify(self, pl: dict, data: dict, chunk: list[int]) -> None:
        count = playlist_track_count(data)
        if count is not None and count != len(chunk):
            log.warning("В «%s» %d треков вместо %d, отправляю список ещё раз", pl["title"], count, len(chunk))
            data = self.client.set_playlist_tracks(pl["id"], chunk)
            count = playlist_track_count(data)
        if count is not None and count != len(chunk):
            self.out(f"  ⚠ «{pl['title']}»: в плейлисте {count} треков из {len(chunk)}, проверьте на сайте.")
        self.out(f"  ✓ «{pl['title']}»: {len(chunk)} треков — {pl.get('url') or ''}")

    def recover_pending_create(self, me: dict) -> None:
        """Прошлый запуск упал между созданием плейлиста и записью его id. Ищем его в аккаунте."""
        p = self.state.pending_create
        found = None
        try:
            for pl in self.client.user_playlists(int(me["id"])):
                created = _parse_time(pl.get("created_at"))
                if pl.get("title") == p["title"] and created and created >= p["started_at"] - 120:
                    if found is None or created > _parse_time(found.get("created_at")):
                        found = pl
        except SoundCloudError as e:
            self.out(f"Не удалось проверить плейлисты аккаунта ({e}).")
            self.out(f"Если в аккаунте есть лишний пустой плейлист «{p['title']}», удалите его вручную.")
        if found:
            self.out(f"Нашёл плейлист «{found['title']}», созданный в прошлый раз, — продолжу в него.")
            self.state.playlists.append({"id": int(found["id"]), "title": found["title"],
                                         "url": found.get("permalink_url"), "track_ids": []})
        self.state.pending_create = None
        self.state.save()


def _parse_time(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d %H:%M:%S +0000"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def main(argv: Optional[list[str]] = None, client: Optional[SoundCloudClient] = None,
         interactive: Optional[bool] = None, input_fn: Callable[[str], str] = input,
         out: Callable[..., None] = print) -> int:
    args = build_parser().parse_args(argv)
    # Старые консоли Windows (cp866/cp1251) не умеют «✓» — заменяем, а не падаем.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    setup_logging(args.verbose)

    try:
        tracks, warnings = read_tracks(args.tracks)
    except TracksFileError as e:
        out(str(e))
        return 1
    for w in warnings:
        out(f"Пропускаю {w}")
    try:
        state = State.load(args.state)
    except StateError as e:
        out(str(e))
        return 1

    if interactive is None:
        interactive = not args.no_input and sys.stdin.isatty()
    if args.review and not interactive:
        out("--review работает только в интерактивном режиме.")
        return 1
    if not 0 <= args.threshold <= 100:
        out("--threshold должен быть от 0 до 100.")
        return 1
    delay = args.delay
    if delay is None:
        raw = (os.environ.get("REQUEST_DELAY") or "1.5").strip().replace(",", ".")
        try:
            delay = float(raw)
        except ValueError:
            out(f"REQUEST_DELAY в .env должен быть числом секунд, например 1.5 (сейчас: {raw!r}).")
            return 1
    if client is None:
        client = SoundCloudClient(min_delay=max(1.0, delay))
    return Runner(args, tracks, state, client, interactive, input_fn, out).run()
