"""report.csv и итоговая сводка в терминале."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .matching import Candidate
from .state import AUTO, BROKEN, MANUAL, NOT_FOUND, PENDING, SKIPPED, State
from .tracks import Track, format_duration

HEADER = ["Трек из VK", "Найдено в SoundCloud", "Ссылка", "Уверенность", "Статус"]


@dataclass
class Summary:
    total: int = 0
    matched: int = 0  # найдено (авто + вручную), без дублей
    manual: int = 0
    added: int = 0  # реально лежит в плейлистах
    skipped: int = 0
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
        found = url = conf = ""
        if e is None:
            status = "не обработан"
            s.unprocessed += 1
        else:
            match: Optional[dict] = e.get("match")
            best = match or (e.get("candidates") or [None])[0]
            if best and e["status"] != NOT_FOUND:
                c = Candidate.from_dict(best)
                found, url = describe(c), c.url
                conf = f"{best.get('score', '')}"
            st = e["status"]
            if key in duplicates:
                status = "дубликат (уже есть в плейлисте)"
                s.duplicates += 1
            elif st in (AUTO, MANUAL):
                s.matched += 1
                is_added = match is not None and match["id"] in added_ids
                s.added += is_added
                if st == MANUAL:
                    s.manual += 1
                    status = "выбран вручную" if is_added or dry_run else "выбран вручную, не добавлен"
                else:
                    status = "добавлен" if is_added else ("будет добавлен" if dry_run else "найден, не добавлен")
            elif st == SKIPPED:
                status = "пропущен"
                s.skipped += 1
            elif st == NOT_FOUND:
                status = "не найден"
                s.not_found += 1
            elif st == PENDING:
                status = "ждёт ручного выбора"
                s.pending += 1
            elif st == BROKEN:
                status = "битое название"
                s.broken += 1
            else:
                status = st
        s.rows.append([track.display, found, url, conf, status])
    return s


def _safe_cell(value: str) -> str:
    """Защита от формул в Excel: название трека «=HYPERLINK(…)» задаёт любой загрузивший."""
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
    out(f"Пропущено:             {summary.skipped}")
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
