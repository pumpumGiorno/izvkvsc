"""Точка входа: python -m vk2sc [--yes] [--dry-run] [--tracks tracks.txt] ...

По умолчанию всё решается автоматически: программа сама пробует несколько
поисковых запросов, сама выбирает лучшего кандидата и пропускает сомнительные
треки. Единственный вопрос — подтверждение перед изменением плейлиста
(его снимает --yes). Старый ручной выбор кандидатов — флаг --interactive.
"""

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
from .envfile import TOKEN_KEY, diagnose_token, load_env
from .matching import (
    AUTO_THRESHOLD,
    MATCHING_VERSION,
    Candidate,
    Decision,
    Scored,
    auto_pick,
    decide,
    rank,
    rival_margin,
    score_candidate,
)
from .normalize import (
    clean_artist_for_query,
    fold,
    has_cyrillic,
    parse_artist,
    search_title,
    simplify,
    translit,
)
from .browser import SCRIPT_NAME, write_script
from .soundcloud import (
    PLAYLIST_MARK,
    AuthError,
    BlockedError,
    PlaylistNotFound,
    Redactor,
    RetryExhausted,
    SoundCloudClient,
    SoundCloudError,
    playlist_track_count,
)
from .state import (
    AUTO,
    AUTOMATIC,
    BROKEN,
    LOW,
    MANUAL,
    MATCHED,
    NOT_FOUND,
    PENDING,
    SKIPPED,
    State,
    StateError,
)
from .tracks import Track, TracksFileError, bitrate_title_kind, format_duration, read_tracks, track_keys

log = logging.getLogger("vk2sc")

DEFAULT_TITLE = "Из VK"
PLAYLIST_LIMIT = 500  # лимит SoundCloud на число треков в плейлисте
SHOW_CANDIDATES = 5
KEEP_CANDIDATES = 10
MAX_QUERIES = 5  # поисковых запросов на один трек, не больше
YES = {"y", "yes", "д", "да"}

# Варианты поискового запроса (подписи идут в лог и в report.csv).
V_ORIGINAL = "исходный"
V_CLEAN = "очищенный"
V_NO_YO = "без ё"
V_TRANSLIT = "транслит"
V_MAIN_ARTIST = "основной исполнитель"
V_TITLE_ARTIST = "название + исполнитель"
V_TITLE_ONLY = "только название"
ALT_SPELLINGS = (V_NO_YO, V_TRANSLIT)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m vk2sc",
        description="Ищет треки из списка VK в SoundCloud и собирает из них новый плейлист. "
                    "Совпадения выбираются автоматически, сомнительные треки пропускаются.",
    )
    p.add_argument("--tracks", type=Path, default=Path("tracks.txt"), help="файл со списком (по умолчанию tracks.txt)")
    p.add_argument("--dry-run", action="store_true", help="только поиск и отчёт: плейлист не создаётся, токен не нужен")
    p.add_argument("-y", "--yes", action="store_true",
                   help="не спрашивать подтверждения перед созданием/изменением плейлиста")
    p.add_argument("--title", help=f"название плейлиста (по умолчанию «{DEFAULT_TITLE}»)")
    p.add_argument("--public", action="store_true", help="сделать плейлист публичным (по умолчанию приватный)")
    p.add_argument("--browser", action="store_true",
                   help=f"не менять плейлисты через API, а создать {SCRIPT_NAME} для консоли браузера на soundcloud.com")
    p.add_argument("--interactive", action="store_true",
                   help="старый режим: спрашивать выбор кандидата, если совпадение сомнительное")
    p.add_argument("--no-input", action="store_true",
                   help="ничего не спрашивать; плейлист меняется только вместе с --yes")
    p.add_argument("--review", action="store_true",
                   help="вместе с --interactive: заново предложить выбор для пропущенных и ненайденных")
    p.add_argument("--rematch", action="store_true",
                   help="пересчитать автоматические совпадения, найденные прошлой версией алгоритма "
                        "(ручной выбор не трогается)")
    p.add_argument("--threshold", type=int, default=AUTO_THRESHOLD,
                   help=f"порог уверенности для автодобавления, 0–100 (по умолчанию {AUTO_THRESHOLD})")
    p.add_argument("--delay", type=float, default=None,
                   help="пауза между запросами, сек (по умолчанию 1.5, минимум 1)")
    p.add_argument("--state", type=Path, default=Path("state.json"), help="файл прогресса (по умолчанию state.json)")
    p.add_argument("--report", type=Path, default=Path("report.csv"), help="файл отчёта (по умолчанию report.csv)")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный вывод: все кандидаты и их оценки")
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


def query_key(query: str) -> str:
    """Для отсева повторов. Не simplify(): «Ёлка» и «Елка» SoundCloud ищет по-разному."""
    return " ".join(query.lower().split())


def search_variants(track: Track) -> list[tuple[str, str]]:
    """Каскад поисковых запросов от точного к широкому, без повторов, не больше MAX_QUERIES.

    1. исходный: исполнитель и название как в tracks.txt;
    2. очищенный: без «(VK.COM)», «(Official Audio)», feat.-гостей;
    3. то же без «ё» или в транслите (для кириллицы);
    4. первый из нескольких исполнителей + название;
    5. название + исполнитель;
    6. только название (результаты всё равно сверяются с исполнителем).
    Запроса по одному исполнителю нет: при пустом названии результат был бы случайным.
    """
    raw = " ".join(f"{track.artist} {track.title}".split())
    artist = clean_artist_for_query(track.artist) or " ".join(track.artist.split())
    title = search_title(track.title)
    clean = f"{artist} {title}".strip()
    variants = [(V_ORIGINAL, raw), (V_CLEAN, clean)]
    if "ё" in clean.lower():
        variants.append((V_NO_YO, clean.replace("ё", "е").replace("Ё", "Е")))
    elif has_cyrillic(fold(clean)):
        variants.append((V_TRANSLIT, translit(simplify(clean))))
    names = parse_artist(artist).names
    if len(names) > 1:
        variants.append((V_MAIN_ARTIST, f"{names[0]} {title}"))
    variants.append((V_TITLE_ARTIST, f"{title} {artist}"))
    core = simplify(title)
    if len(core) >= 3 and not core.replace(" ", "").isdigit():
        variants.append((V_TITLE_ONLY, title))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, q in variants:
        key = query_key(q)
        if key and key not in seen:
            seen.add(key)
            unique.append((label, q))
    # Перестановка слов даёт в SoundCloud почти те же результаты — её жертвуем первой.
    while len(unique) > MAX_QUERIES and any(label == V_TITLE_ARTIST for label, _ in unique):
        unique = [v for v in unique if v[0] != V_TITLE_ARTIST]
    return unique[:MAX_QUERIES]


def build_queries(track: Track) -> list[str]:
    return [q for _, q in search_variants(track)]


def signed_duration(track: Track, cand: Candidate) -> Optional[str]:
    """«+0 сек», «-3 сек» — насколько кандидат длиннее трека из VK; None — длительность неизвестна."""
    if not track.duration or not cand.duration:
        return None
    return f"{cand.duration - track.duration:+d} сек"


class Runner:
    def __init__(self, args: argparse.Namespace, tracks: list[Track], state: State,
                 client: SoundCloudClient, interactive: bool,
                 input_fn: Callable[[str], str] = input, out: Callable[..., None] = print) -> None:
        self.args = args
        self.tracks = tracks
        self.keys = track_keys(tracks)
        self.state = state
        self.client = client
        # interactive — можно ли вообще задавать вопросы (терминал, нет --no-input);
        # manual — спрашивать выбор кандидатов (только с --interactive).
        self.interactive = interactive
        self.manual = interactive and bool(getattr(args, "interactive", False))
        self.input = input_fn
        self.out = out
        self.search_count = 0
        self.me: Optional[dict] = None

    # ---------- общий сценарий ----------

    def run(self) -> int:
        code = 0
        log.info("=== Запуск: треков %d, dry-run=%s, ручной выбор=%s, алгоритм v%d ===",
                 len(self.tracks), self.args.dry_run, self.manual, MATCHING_VERSION)
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
        elif not st.playlist_title and self.manual and not self.args.dry_run:
            # Спрашиваем только в --interactive. В --dry-run не спрашиваем и ничего не
            # запоминаем: иначе при настоящем запуске вопрос о названии уже не прозвучит.
            answer = self.input(f"Название нового плейлиста [{DEFAULT_TITLE}]: ").strip()
            st.playlist_title = answer or DEFAULT_TITLE
            st.save()

    # ---------- поиск и сопоставление ----------

    def needs_work(self, entry: Optional[dict], track: Optional[Track] = None) -> bool:
        review = self.args.review and self.manual
        if track is not None and track.broken_title:
            # Перекрываем старые автоматические записи: раньше такой трек мог «найтись» случайно.
            # Ручной выбор не трогаем — это могла быть настоящая песня «128».
            if entry is None or entry["status"] not in (BROKEN, MANUAL):
                return True
            return entry["status"] == BROKEN and review
        if entry is None:
            return True
        status = entry["status"]
        if status in (PENDING, BROKEN):
            # PENDING — отложенный ручной выбор из старых версий; BROKEN — трек, который
            # больше не считается битым (одиночное «Artist — 320»).
            return True
        if review and status in (SKIPPED, NOT_FOUND, LOW):
            return True
        if entry.get("algo", 1) < MATCHING_VERSION and status in AUTOMATIC:
            # Новая версия могла бы найти то, что не нашла старая. Найденное раньше
            # пересчитываем только по --rematch: повторять поиск без нужды незачем.
            # Исключение — название-число («Artist — 320»): старые версии могли
            # «найти» такой трек по одному исполнителю.
            if status == AUTO and track is not None and bitrate_title_kind(track.title):
                return True
            return status != AUTO or self.args.rematch
        return False

    def match_all(self) -> None:
        todo = [i for i, k in enumerate(self.keys) if self.needs_work(self.state.get(k), self.tracks[i])]
        broken = [t for t in self.tracks if t.broken_title]
        if broken:
            lines = ", ".join(str(t.line_no) for t in broken[:15]) + (" …" if len(broken) > 15 else "")
            self.out(
                f"Треков с битым названием (вместо названия битрейт, например «{broken[0].title}»): {len(broken)}, "
                f"строки {lines}. Искать их не буду. Перевыгрузите список из VK (см. README) или исправьте tracks.txt."
            )
        broken_todo = sum(1 for i in todo if self.tracks[i].broken_title)
        fresh = sum(1 for i in todo if self.state.get(self.keys[i]) is None and not self.tracks[i].broken_title)
        again = len(todo) - fresh - broken_todo
        self.out(
            f"Треков в списке: {len(self.tracks)}. Решено раньше: {len(self.tracks) - len(todo)}. "
            f"Искать впервые: {fresh}." + (f" Перепроверить: {again}." if again else "")
        )
        if todo and not self.manual:
            self.out("Режим: автоматический выбор. Сомнительные совпадения пропускаются и попадают в отчёт.")
        for i in todo:
            self.match_one(i)

    def match_one(self, i: int) -> None:
        track, key = self.tracks[i], self.keys[i]
        entry = self.state.get(key)
        self.out(f"\n[{i + 1}/{len(self.tracks)}] {track.display}")

        if track.broken_title:
            # Не ищем ни по названию-битрейту, ни по одному исполнителю: результат был бы случайным.
            if entry and entry["status"] == BROKEN and self.args.review and self.manual:
                self.out("  Название похоже на битрейт, автоматически не ищу. Если это настоящее название, найдите трек сами.")
                status, chosen, ranked = self.ask(track, Decision("none", []), [], skip_status=BROKEN)
                self.save(key, track, status, chosen, ranked, [])
                return
            self.out("  ✗ битое название (похоже на битрейт) — не ищу")
            self.save(key, track, BROKEN, None, [], [])
            return

        cached: list[Candidate] = []
        queries: list[str] = []
        online = True
        if entry and entry.get("candidates") is not None and entry["status"] != BROKEN:
            # Уже искали: берём кэш. Если он от текущей версии алгоритма — в SoundCloud
            # не ходим вовсе, от старой — добираем только новые варианты запроса.
            cached = [Candidate.from_dict(c) for c in entry["candidates"]]
            queries = list(entry.get("queries", []))
            online = entry.get("algo", 1) < MATCHING_VERSION
            self.out("  результаты прошлого поиска взяты из state.json")
        candidates, queries = self.search(track, cached, queries, online)
        ranked = rank(track, candidates)

        if self.manual:
            decision = decide(track, ranked, self.args.threshold)
            if decision.kind == "auto":
                metrics = self.explain(track, decision)
                self.out("  → автоматически выбран")
                self.save(key, track, AUTO, decision.best, ranked, queries, decision.reason, metrics)
            else:
                status, chosen, ranked = self.ask(track, decision, queries)
                self.save(key, track, status, chosen, ranked, queries, decision.reason)
            return

        decision = auto_pick(track, ranked, self.args.threshold)
        self.show_verbose(track, decision.ranked)
        metrics = self.explain(track, decision)
        if decision.kind == "auto":
            self.out(f"  → автоматически выбран: {decision.reason}")
            self.save(key, track, AUTO, decision.best, decision.ranked, queries, decision.reason, metrics)
        elif decision.kind == "low":
            self.out(f"  → пропущен: низкая уверенность ({decision.reason})")
            self.save(key, track, LOW, None, decision.ranked, queries, decision.reason, metrics)
        else:
            self.out("  → не найден")
            self.save(key, track, NOT_FOUND, None, decision.ranked, queries, decision.reason)

    def explain(self, track: Track, decision: Decision) -> Optional[dict]:
        """Печатает признаки лучшего кандидата — по ним видно, почему он принят или отвергнут."""
        best = decision.best
        if best is None or decision.kind == "none":
            return None
        metrics = best.metrics()
        metrics["margin"] = rival_margin(decision)
        if track.duration and best.candidate.duration:
            metrics["duration_delta"] = best.candidate.duration - track.duration  # со знаком, как в логе
        self.out(f"  лучший вариант: {report.describe(best.candidate)}")
        self.out(f"  score: {best.score}")
        self.out(f"  title: {metrics['title']}" + (f" (слов совпало: {best.containment:.0%})"
                                                  if best.title_fuzzy < best.title_sim - 1 else ""))
        self.out(f"  artist: {metrics['artist']}" + (" (тот же набор исполнителей)" if best.artist_exact else ""))
        self.out(f"  duration: {signed_duration(track, best.candidate) or 'неизвестна'}")
        self.out(f"  version conflict: {'да' if best.version_conflict else 'нет'}"
                 + ("; похоже на другую часть трека" if best.title_conflict else ""))
        margin = metrics["margin"]
        self.out(f"  margin: {f'{margin:+d}' if margin is not None else 'других вариантов нет'}")
        return metrics

    def show_verbose(self, track: Track, ranked: list[Scored]) -> None:
        if not self.args.verbose or not ranked:
            return
        self.out("  кандидаты:")
        for n, s in enumerate(ranked[:SHOW_CANDIDATES], 1):
            c = s.candidate
            flags = []
            if s.version_conflict:
                flags.append("другая версия")
            if s.title_conflict:
                flags.append("другая часть")
            if s.artist_exact:
                flags.append("те же исполнители")
            tail = f" [{', '.join(flags)}]" if flags else ""
            self.out(f"   {n}. {c.display}  score {s.score} · title {round(s.title_sim)} · "
                     f"artist {round(s.artist_sim)} · {signed_duration(track, c) or 'длительность ?'}{tail}  {c.url}")

    def good_enough(self, track: Track, collected: list[Candidate], remaining: list[tuple[str, str]]) -> bool:
        """Хватит ли уже найденного, чтобы больше не искать."""
        if not collected:
            return False
        decision = auto_pick(track, rank(track, collected), self.args.threshold)
        if decision.kind != "auto" or not decision.confident:
            return False
        # Перезаливку от случайного пользователя считаем достаточной, только если
        # другое написание (транслит, без «ё») не нашло загрузку с аккаунта исполнителя.
        return decision.best.uploader_match or not any(label in ALT_SPELLINGS for label, _ in remaining)

    def search(self, track: Track, collected: list[Candidate], used: list[str],
               online: bool = True) -> tuple[list[Candidate], list[str]]:
        collected, used = list(collected), list(used)
        done = {query_key(q) for q in used}
        todo = [(label, q) for label, q in search_variants(track) if query_key(q) not in done]
        if not online or self.good_enough(track, collected, todo):
            return collected, used
        searched = 0
        while todo and len(used) < MAX_QUERIES:
            label, q = todo.pop(0)
            if searched == 0 and not used:
                self.out(f"  поиск: {q}")
            else:
                self.out(f"  повторный поиск ({label}): {q}")
            used.append(q)
            log.debug("Поиск [%s]: %s", label, q)
            found = [Candidate.from_api(t) for t in self.client.search_tracks(q)]
            for c in found:
                c.variant = label
            collected += found
            self.search_count += 1
            searched += 1
            if self.good_enough(track, collected, todo):
                break
            if todo and len(used) < MAX_QUERIES:
                viable = auto_pick(track, rank(track, collected), self.args.threshold).kind != "none"
                self.out("  результат недостаточно хороший" if viable else "  ничего подходящего")
        return collected, used

    def save(self, key: str, track: Track, status: str, chosen: Optional[Scored],
             ranked: list[Scored], queries: list[str], reason: str = "",
             metrics: Optional[dict] = None) -> None:
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
            "reason": reason,
            "metrics": metrics,  # признаки лучшего кандидата: title, artist, duration_delta, margin…
            "algo": MATCHING_VERSION,
        })

    # ---------- диалог (только --interactive) ----------

    def show(self, shown: list[Scored]) -> None:
        for n, s in enumerate(shown, 1):
            c = s.candidate
            dur = format_duration(c.duration) or "?:??"
            flags = f"  [{c.flags}]" if c.flags else ""
            self.out(f"   {n}. {c.display}  {dur}  {s.score}%  {c.url}{flags}")

    def ask(self, track: Track, decision: Decision, queries: list[str],
            skip_status: Optional[str] = None) -> tuple[str, Optional[Scored], list[Scored]]:
        ranked = decision.ranked
        shown = ranked[:SHOW_CANDIDATES] if decision.kind == "ask" else []
        if skip_status is None:
            skip_status = SKIPPED if shown else NOT_FOUND
            self.out(f"  Нужен ваш выбор: {decision.reason}." if shown else "  Ничего похожего не нашлось.")
        while True:
            if shown:
                self.show(shown)
            answer = self.input("  Номер — выбрать, Enter — пропустить, текст или ссылка — искать иначе: ").strip()
            if not answer:
                self.out({SKIPPED: "  → пропущен", NOT_FOUND: "  → не найден"}.get(skip_status, "  → оставлен как есть"))
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
                cand = Candidate.from_api(data)
                cand.variant = "ваша ссылка"
                chosen = score_candidate(track, cand)
                self.out(f"  → выбран по ссылке: {chosen.candidate.display}")
                return MANUAL, chosen, [chosen] + [s for s in ranked if s.candidate.id != chosen.candidate.id]
            queries.append(answer)
            found = [Candidate.from_api(t) for t in self.client.search_tracks(answer)]
            for c in found:
                c.variant = "ваш запрос"
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

    def playlist_titles(self, count: int) -> list[str]:
        """Названия плейлистов по порядку: уже созданные сохраняют своё, новые — «Из VK», «Из VK (2)»…"""
        title = self.state.playlist_title or DEFAULT_TITLE
        return [self.state.playlists[k]["title"] if k < len(self.state.playlists)
                else (title if k == 0 else f"{title} ({k + 1})") for k in range(count)]

    def sync_playlists(self) -> int:
        ids, _ = self.desired_ids()
        pending = sum(1 for k in self.keys if (self.state.get(k) or {}).get("status") == PENDING)
        if pending:
            self.out(f"\n{pending} трек(ов) ждут ручного выбора — выберите их в режиме --interactive.")
        if not ids:
            self.out("\nНе найдено ни одного трека для плейлиста.")
            return 0
        chunks = [ids[i:i + PLAYLIST_LIMIT] for i in range(0, len(ids), PLAYLIST_LIMIT)]
        if self.args.browser:
            return self.browser_script(chunks)
        token = os.environ.get(TOKEN_KEY, "").strip()
        if not token:
            self.out(f"\nДля создания плейлиста нужен {TOKEN_KEY} в файле .env (см. README).")
            self.out(diagnose_token())
            self.out("Результаты поиска сохранены: после добавления токена поиск повторяться не будет.")
            self.out("Можно и без токена в .env: python -m vk2sc --browser создаст скрипт для консоли браузера.")
            return 1
        self.client.set_oauth_token(token)
        try:
            return self.sync_via_api(chunks)
        except BlockedError as e:
            # POST не прошёл — плейлист не создан, ждать его при следующем запуске не нужно.
            self.state.pending_create = None
            self.state.save()
            self.out(f"\n{e}")
            self.browser_script(chunks)
            return 3

    def sync_via_api(self, chunks: list[list[int]]) -> int:
        me = self.client.me()
        self.me = me
        if self.state.pending_create:
            self.recover_pending_create(me)
        self.adopt_browser_playlists(me, chunks)

        sharing = "public" if self.args.public else "private"
        titles = self.playlist_titles(len(chunks))
        plan = []
        for k, chunk in enumerate(chunks):
            if k < len(self.state.playlists):
                pl = self.state.playlists[k]
                if pl.get("track_ids") != chunk:
                    plan.append(("update", k, pl["title"], chunk))
            else:
                plan.append(("create", k, titles[k], chunk))
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
        if self.args.yes:
            self.out("Подтверждено флагом --yes.")
        elif not self.interactive:
            self.out("Без подтверждения ничего не создаю. Запустите с --yes (или в терминале без --no-input).")
            return 1
        elif self.input("Продолжить? [y/N]: ").strip().lower() not in YES:
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
                    if self.args.yes:
                        self.out("Создаю его заново (--yes).")
                    elif not self.interactive or self.input("Создать его заново? [y/N]: ").strip().lower() not in YES:
                        self.out("Пропускаю. Остальные изменения сохранены.")
                        continue
                    data = self.client.create_playlist(pl["title"], chunk, sharing)
                    pl.update(id=int(data["id"]), url=data.get("permalink_url"), sharing=sharing)
                pl["track_ids"] = chunk
                self.state.save()
            self.verify(pl, data, chunk)
        return 0

    def adopt_browser_playlists(self, me: dict, chunks: list[list[int]]) -> None:
        """Плейлисты, созданные браузерным скриптом, программа ещё не знает — находим их по
        названию и метке «vk2sc» в описании, чтобы не создать второй раз и не спрашивать зря.
        Уже известным плейлистам, которые скрипт обновил, записываем их настоящий список треков."""
        stale = [k for k, pl in enumerate(self.state.playlists[:len(chunks)]) if pl.get("track_ids") != chunks[k]]
        if len(self.state.playlists) >= len(chunks) and not stale:
            return
        try:
            remote = self.client.user_playlists(int(me["id"]))
        except BlockedError:
            raise
        except SoundCloudError as e:
            log.warning("Не удалось получить плейлисты аккаунта: %s", e)
            return
        by_id = {p.get("id"): p for p in remote}
        for k in stale:
            pl = self.state.playlists[k]
            if pl.get("id") in by_id and _remote_track_ids(by_id[pl["id"]]) == chunks[k]:
                pl["track_ids"] = chunks[k]  # обновлён в браузере — менять нечего
        known = {pl.get("id") for pl in self.state.playlists}
        titles = self.playlist_titles(len(chunks))
        for k in range(len(self.state.playlists), len(chunks)):
            found = [p for p in remote if p.get("title") == titles[k] and p.get("id") not in known
                     and PLAYLIST_MARK in (p.get("description") or "")]
            if not found:
                break  # плейлисты идут по порядку: «Из VK», «Из VK (2)»…
            p = max(found, key=lambda x: _parse_time(x.get("created_at")) or 0)
            remote_ids = _remote_track_ids(p)
            # Скрипт ставит ровно нужный список; если SoundCloud отдал его не полностью или
            # отбросил недоступные треки, не гоняем обновление по кругу.
            track_ids = chunks[k] if set(remote_ids) <= set(chunks[k]) else remote_ids
            self.state.playlists.append({"id": int(p["id"]), "title": titles[k], "url": p.get("permalink_url"),
                                         "sharing": p.get("sharing"), "track_ids": track_ids})
            self.out(f"Нашёл плейлист «{titles[k]}», созданный в браузере, — продолжаю с ним.")
        self.state.save()

    def browser_script(self, chunks: list[list[int]]) -> int:
        """Пишет soundcloud_playlists.js и объясняет, как запустить его на soundcloud.com."""
        sharing = "public" if self.args.public else "private"
        plan = list(zip(self.playlist_titles(len(chunks)), chunks))
        client_id = getattr(self.client, "_client_id", None)
        if not client_id and hasattr(self.client, "_load_cached_client_id"):
            client_id = self.client._load_cached_client_id()
        path = Path(SCRIPT_NAME).resolve()
        try:
            write_script(path, plan, sharing, client_id)
        except OSError as e:
            self.out(f"Не удалось записать {path}: {e}")
            return 1
        account = (getattr(self, "me", None) or {}).get("username")
        self.out(f"\nСоздал файл {path} — он создаст плейлисты прямо из браузера "
                 f"({', '.join(f'«{t}» — {len(c)} треков' for t, c in plan)}):")
        self.out("  1. Откройте https://soundcloud.com в браузере, где вы вошли в аккаунт"
                 + (f" {account}." if account else "."))
        self.out("  2. Нажмите F12 и перейдите на вкладку Console (Консоль).")
        self.out(f"  3. Откройте файл в Блокноте (notepad {SCRIPT_NAME}), нажмите Ctrl+A и Ctrl+C,")
        self.out("     вставьте в консоль и нажмите Enter. Если браузер попросит, сначала наберите allow pasting.")
        self.out("  4. Если SoundCloud попросит проверку «я не робот», закройте панель F12 и пройдите её —")
        self.out("     скрипт подождёт и продолжит сам. Ход работы виден в плашке внизу слева на странице.")
        self.out("  5. Дождитесь «vk2sc: готово ✓» (в F12 будет таблица со ссылками).")
        self.out("Повторный запуск скрипта не создаёт дублей. Потом запустите python -m vk2sc ещё раз —")
        self.out("программа найдёт эти плейлисты и отметит треки в отчёте как добавленные.")
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
            # Треки, которые в нём уже есть (например, его заполнил браузерный скрипт):
            # иначе программа будет снова и снова «обновлять» его на тот же список.
            self.state.playlists.append({"id": int(found["id"]), "title": found["title"],
                                         "url": found.get("permalink_url"), "track_ids": _remote_track_ids(found)})
        self.state.pending_create = None
        self.state.save()


def _remote_track_ids(playlist: dict) -> list[int]:
    """id треков плейлиста из ответа API (там есть все треки, у большинства — только id)."""
    return [int(t["id"]) for t in playlist.get("tracks") or [] if isinstance(t, dict) and "id" in t]


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
    load_env()
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
    elif args.no_input:
        interactive = False
    if args.interactive and not interactive:
        out("--interactive нужен терминал для ответов (и несовместим с --no-input).")
        return 1
    if args.review and not args.interactive:
        out("--review работает только вместе с --interactive.")
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
