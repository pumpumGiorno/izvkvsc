"""report.csv и итоговая сводка в терминале."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .matching import Candidate
from .normalize import search_title
from .state import AUTO, BROKEN, LOW, MANUAL, NOT_FOUND, PENDING, SKIPPED, State
from .tracks import Track, format_duration

HEADER = [
    "line",
    "original_artist",
    "original_title",
    "search_title",
    "matched_artist",
    "matched_title",
    "soundcloud_url",
    "score",
    "status",
    "search_variant",
    "note",
    # Признаки лучшего кандидата: по ним видно, почему он принят или отвергнут.
    "title_score",
    "artist_score",
    "duration_delta",
    "version_conflict",
    "margin",
]

# Статусы в отчёте
ST_ADDED = "добавлен"
ST_AUTO = "автоматически выбран"
ST_MANUAL = "выбран вручную"
ST_NOT_FOUND = "не найден"
ST_LOW = "пропущен: низкая уверенность"
ST_SKIPPED = "пропущен"
ST_PENDING = "ждёт ручного выбора"
ST_BROKEN = "битое название"
ST_DUPLICATE = "дубликат"
ST_UNPROCESSED = "не обработан"


@dataclass
class Summary:
    total: int = 0
    matched: int = 0  # найдено (авто + вручную), без дублей
    manual: int = 0
    added: int = 0  # реально лежит в плейлистах
    skipped: int = 0
    low: int = 0
    not_found: int = 0
    pending: int = 0
    broken: int = 0
    duplicates: int = 0
    unprocessed: int = 0
    rows: list = field(default_factory=list)


def describe(c: Candidate) -> str:
    text = c.display
    if c.duration:
        text += f" [{format_duration(c.duration)}]"
    if c.flags:
        text += f" ({c.flags})"
    return text


def build(tracks: list[Track], keys: list[str], state: State, dry_run: bool,
          duplicates: set[str], added_ids: set[int]) -> Summary:
    s = Summary(total=len(tracks))
    for track, key in zip(tracks, keys):
        e = state.get(key)
        shown: Optional[Candidate] = None
        score = ""
        notes: list[str] = []
        if e is None:
            status = ST_UNPROCESSED
            s.unprocessed += 1
        else:
            match: Optional[dict] = e.get("match")
            best = match or (e.get("candidates") or [None])[0]
            st = e["status"]
            if best and st not in (NOT_FOUND, BROKEN):
                shown = Candidate.from_dict(best)
                score = f"{best.get('score', '')}"
            if key in duplicates:
                status = ST_DUPLICATE
                notes.append("этот трек уже есть в плейлисте выше по списку")
                s.duplicates += 1
            elif st in (AUTO, MANUAL):
                s.matched += 1
                is_added = match is not None and match["id"] in added_ids
                s.added += is_added
                if st == MANUAL:
                    s.manual += 1
                if is_added:
                    status = ST_ADDED
                    if st == MANUAL:
                        notes.append("выбран вручную")
                else:
                    status = ST_MANUAL if st == MANUAL else ST_AUTO
                    if not dry_run:
                        notes.append("не добавлен в плейлист")
            elif st == LOW:
                status = ST_LOW
                s.low += 1
            elif st == SKIPPED:
                status = ST_SKIPPED
                s.skipped += 1
            elif st == NOT_FOUND:
                status = ST_NOT_FOUND
                s.not_found += 1
            elif st == PENDING:
                status = ST_PENDING
                s.pending += 1
            elif st == BROKEN:
                status = ST_BROKEN
                notes.append("вместо названия битрейт — исправьте строку в tracks.txt")
                s.broken += 1
            else:
                status = st
            reason = e.get("reason")
            if reason and st in (AUTO, LOW, NOT_FOUND) and key not in duplicates:
                notes.insert(0, reason)
        m = (e or {}).get("metrics") or {}
        margin = m.get("margin")
        s.rows.append([
            track.line_no or "",
            track.artist,
            track.title,
            "" if track.broken_title else search_title(track.title),
            shown.artist if shown else "",
            shown.title if shown else "",
            shown.url if shown else "",
            score,
            status,
            (shown.variant or "") if shown else "",
            "; ".join(notes),
            m.get("title", ""),
            m.get("artist", ""),
            "" if m.get("duration_delta") is None else m["duration_delta"],
            {True: "да", False: "нет"}.get(m.get("version_conflict"), ""),
            "" if margin is None else margin,
        ])
    return s


def _safe_cell(value: str) -> str:
    """Защита от формул в Excel: название трека «=HYPERLINK(…)» задаёт любой загрузивший.
    Числа («-3» в duration_delta) оставляем числами."""
    if re.fullmatch(r"-?\d+", value):
        return value
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def write_csv(path: Path, summary: Summary) -> None:
    # utf-8-sig и «;» — чтобы русский Excel открыл файл без танцев с импортом.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADER)
        w.writerows([_safe_cell(str(c)) for c in row] for row in summary.rows)


def print_summary(summary: Summary, dry_run: bool, report_path: Path, out=print) -> None:
    label = "Будет добавлено" if dry_run else "Добавлено"
    count = summary.matched if dry_run else summary.added
    out("")
    out("──────── Сводка ────────")
    out(f"Всего треков:          {summary.total}")
    extra = f" (из них выбрано вручную: {summary.manual})" if summary.manual else ""
    out(f"{label + ':':<23}{count}{extra}")
    if not dry_run and summary.matched > summary.added:
        out(f"Найдено, но не добавлено: {summary.matched - summary.added}")
    out(f"Низкая уверенность:    {summary.low}")
    if summary.skipped:
        out(f"Пропущено вручную:     {summary.skipped}")
    out(f"Не найдено:            {summary.not_found}")
    if summary.pending:
        out(f"Ждут ручного выбора:   {summary.pending}")
    if summary.broken:
        out(f"Битые названия:        {summary.broken} (исправьте их в tracks.txt)")
    if summary.duplicates:
        out(f"Дубликаты:             {summary.duplicates}")
    if summary.unprocessed:
        out(f"Ещё не обработано:     {summary.unprocessed}")
    out(f"Отчёт: {report_path}")
