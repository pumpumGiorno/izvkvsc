"""Чтение списка треков из tracks.txt.

Формат строки: «Исполнитель — Название», по одному треку на строку.
Необязательная длительность в конце через « | » или табуляцию:
«Imagine Dragons — Believer | 3:24». Пустые строки и комментарии «# …» пропускаются
(«#2Маши — Босая» — это трек: после решётки нет пробела).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .normalize import fold

# Сначала пробуем разделители с пробелами, потом без: у «Би-2» дефис внутри имени.
_SEPARATORS = (" — ", " – ", " - ", "—", "–")
_DURATION_RE = re.compile(r"(?:\t+|\s+\|\s*)((?:\d{1,2}:)?\d{1,2}:\d{2})\s*$")

# Бейдж битрейта вместо названия («320», «~128», «256 kbps»): так бывает, когда расширение
# браузера вставляет его в строку трека и экспорт из VK берёт бейдж за название.
BITRATE_TITLE_RE = re.compile(r"^~?\s?\d{2,3}(\s*(?:kbps|kbit/s|kb/s|кбит/с|кбит))?$", re.IGNORECASE)
# Число без «~» и «kbps» считаем битрейтом, только если это стандартное значение:
# «Arctic Monkeys — 505» и «Taylor Swift — 22» — настоящие песни.
_STANDARD_BITRATES = {32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320}


def bitrate_title_kind(title: str) -> Optional[str]:
    """«explicit» — точно битрейт («~128», «256 kbps»); «plain» — голое стандартное
    число («320»): это может быть и настоящая песня; None — обычное название."""
    text = title.strip()
    m = BITRATE_TITLE_RE.match(text)
    if not m:
        return None
    if text.startswith("~") or m.group(1):
        return "explicit"
    return "plain" if int(text) in _STANDARD_BITRATES else None


def is_bitrate_title(title: str, context: bool = True) -> bool:
    """context=True — в списке есть и другие признаки бага экспорта, голое «320» тоже битрейт."""
    kind = bitrate_title_kind(title)
    return kind == "explicit" or (kind == "plain" and context)


class TracksFileError(Exception):
    pass


@dataclass(frozen=True)
class Track:
    artist: str
    title: str
    duration: Optional[int] = None  # сек
    line_no: int = 0
    # В файле есть другие битрейты вместо названий: тогда и голое «320» считаем битрейтом.
    bitrate_context: bool = field(default=False, compare=False)

    @property
    def display(self) -> str:
        text = f"{self.artist} — {self.title}"
        return f"{text} [{format_duration(self.duration)}]" if self.duration else text

    @property
    def broken_title(self) -> bool:
        """Вместо названия битрейт — искать такой трек бессмысленно.

        «~128» и «256 kbps» — всегда битрейт. Голое «320» — только если в том же
        списке есть и другие такие названия (баг экспорта); одиночное «Artist — 320»
        ищется как обычная песня, но совпасть может только с треком «320»."""
        return is_bitrate_title(self.title, self.bitrate_context)

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
    return apply_bitrate_context(tracks), warnings


def apply_bitrate_context(tracks: list[Track]) -> list[Track]:
    """Голые «320»/«128» считаем битрейтом, если в списке есть «~128»/«kbps» или таких чисел несколько."""
    kinds = [bitrate_title_kind(t.title) for t in tracks]
    context = "explicit" in kinds or kinds.count("plain") >= 2
    return [replace(t, bitrate_context=context) for t in tracks]


def track_keys(tracks: list[Track]) -> list[str]:
    """Уникальные ключи; повторы одного трека различаются номером вхождения."""
    seen: dict[str, int] = {}
    keys = []
    for t in tracks:
        base = t.base_key()
        seen[base] = seen.get(base, 0) + 1
        keys.append(f"{base} #{seen[base]}")
    return keys
