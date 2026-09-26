"""Чтение списка треков из tracks.txt.

Формат строки: «Исполнитель — Название», по одному треку на строку.
Необязательная длительность в конце через « | » или табуляцию:
«Imagine Dragons — Believer | 3:24». Пустые строки и комментарии «# …» пропускаются
(«#2Маши — Босая» — это трек: после решётки нет пробела).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .normalize import fold

# Сначала пробуем разделители с пробелами, потом без: у «Би-2» дефис внутри имени.
_SEPARATORS = (" — ", " – ", " - ", "—", "–")
_DURATION_RE = re.compile(r"(?:\t+|\s+\|\s*)((?:\d{1,2}:)?\d{1,2}:\d{2})\s*$")


class TracksFileError(Exception):
    pass


@dataclass(frozen=True)
class Track:
    artist: str
    title: str
    duration: Optional[int] = None  # сек
    line_no: int = 0

    @property
    def display(self) -> str:
        text = f"{self.artist} — {self.title}"
        return f"{text} [{format_duration(self.duration)}]" if self.duration else text

    def base_key(self) -> str:
        """Ключ для state.json. Не зависит от номера строки: файл можно дополнять."""
        return f"{fold(self.artist)} — {fold(self.title)}"


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_duration(text: str) -> int:
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def parse_line(line: str, line_no: int = 0) -> Optional[Track]:
    """Разбирает строку. None — пустая строка или комментарий; ValueError — неверный формат."""
    text = line.strip().lstrip("\ufeff")
    if not text or text == "#" or re.match(r"#\s", text):
        return None

    duration = None
    m = _DURATION_RE.search(text)
    if m:
        duration = parse_duration(m.group(1))
        text = text[: m.start()].rstrip()

    for sep in _SEPARATORS:
        if sep in text:
            artist, title = text.split(sep, 1)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return Track(artist=artist, title=title, duration=duration or None, line_no=line_no)
    raise ValueError("нет разделителя «—» между исполнителем и названием")


def read_tracks(path: Path) -> tuple[list[Track], list[str]]:
    """Возвращает треки и предупреждения о строках, которые не удалось разобрать."""
    if not path.exists():
        raise TracksFileError(f"Файл {path} не найден.")
    raw = path.read_bytes()
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            content = raw.decode("utf-16")
        else:
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                # Старый «Блокнот» в Windows сохраняет в cp1251.
                content = raw.decode("cp1251")
    except UnicodeDecodeError:
        raise TracksFileError(f"Не удалось прочитать {path}: сохраните файл в кодировке UTF-8.") from None

    tracks: list[Track] = []
    warnings: list[str] = []
    for no, line in enumerate(content.splitlines(), start=1):
        try:
            track = parse_line(line, no)
        except ValueError as e:
            warnings.append(f"строка {no}: {e}: {line.strip()[:80]}")
            continue
        if track:
            tracks.append(track)
    if not tracks:
        raise TracksFileError(f"В {path} нет ни одного трека в формате «Исполнитель — Название».")
    return tracks, warnings


def track_keys(tracks: list[Track]) -> list[str]:
    """Уникальные ключи; повторы одного трека различаются номером вхождения."""
    seen: dict[str, int] = {}
    keys = []
    for t in tracks:
        base = t.base_key()
        seen[base] = seen.get(base, 0) + 1
        keys.append(f"{base} #{seen[base]}")
    return keys
