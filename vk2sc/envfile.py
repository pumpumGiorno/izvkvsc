"""Чтение .env без сюрпризов Windows и понятная диагностика, если токена нет.

python-dotenv сам по себе молча пропускает частые ошибки: файл сохранён как
«.env.txt», токен вставлен на строку ниже «SOUNDCLOUD_OAUTH_TOKEN=», файл в
UTF-16 (так пишет «>» в Windows PowerShell 5) — последний вариант вообще роняет
программу. Здесь файл читается в любой из этих кодировок, а diagnose_token()
объясняет, что не так, не показывая сам токен.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Optional

TOKEN_KEY = "SOUNDCLOUD_OAUTH_TOKEN"
# «2-123456-78901234-AbCdEf», иногда с приставкой «OAuth » из заголовка запроса.
_TOKEN_LIKE_RE = re.compile(r"^(?:OAuth\s+)?\d-\d+-\d+-[A-Za-z0-9_-]+$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def env_candidates() -> list[Path]:
    """Где искать .env: текущая папка и папка с программой (на случай запуска из другого места)."""
    paths = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    unique: list[Path] = []
    for p in paths:
        if p not in unique:
            unique.append(p)
    return unique


def read_text(path: Path) -> str:
    """UTF-8 (с BOM и без), UTF-16 (с BOM) или cp1251."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


def parse(text: str) -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError:
        values: dict[str, str] = {}
        for line in text.splitlines():
            m = _LINE_RE.match(line)
            if m and not line.lstrip().startswith("#"):
                values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        return values
    return {k: v or "" for k, v in dotenv_values(stream=io.StringIO(text)).items()}


def load_env() -> Optional[Path]:
    """Загружает первый найденный .env в os.environ. Уже заданные непустые переменные
    окружения не перезаписываются. Возвращает путь к загруженному файлу."""
    for path in env_candidates():
        if not path.is_file():
            continue
        try:
            values = parse(read_text(path))
        except (OSError, UnicodeDecodeError):
            continue
        for key, value in values.items():
            if value and not os.environ.get(key, "").strip():
                os.environ[key] = value
        return path
    return None


def diagnose_token() -> str:
    """Почему токен не прочитался — одной-двумя фразами, без значения токена."""
    env = next((p for p in env_candidates() if p.is_file()), None)
    if env is None:
        folder = env_candidates()[0].parent
        for p in env_candidates():
            for name in (".env.txt", "env.txt", "env", ".env.example.txt"):
                if (p.parent / name).is_file():
                    return (f"Файл называется «{name}», а нужен ровно «.env» (Блокнот мог добавить «.txt»). "
                            f"Переименуйте его: Rename-Item \"{p.parent / name}\" .env")
        return (f"В папке {folder} нет файла .env. Создайте его: copy .env.example .env, "
                f"затем notepad .env и впишите токен после «{TOKEN_KEY}=».")
    try:
        text = read_text(env)
    except (OSError, UnicodeDecodeError) as e:
        return f"Не удалось прочитать {env} ({type(e).__name__}). Сохраните его в Блокноте в кодировке UTF-8."
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and TOKEN_KEY in stripped and "=" in stripped:
            after = stripped.split("=", 1)[1].strip()
            if after:
                return (f"В {env} строка с токеном закомментирована: уберите «#» в начале строки "
                        f"«{TOKEN_KEY}=…».")
        m = _LINE_RE.match(line)
        if not m or m.group(1) != TOKEN_KEY:
            continue
        if m.group(2).strip().strip('"').strip("'"):
            return (f"В {env} токен есть, но его перекрывает пустая переменная окружения {TOKEN_KEY}. "
                    f"Закройте и заново откройте окно PowerShell.")
        following = next((ln.strip() for ln in lines[i + 1:] if ln.strip()), "")
        if _TOKEN_LIKE_RE.match(following):
            return (f"В {env} токен стоит на отдельной строке под «{TOKEN_KEY}=». "
                    f"Перенесите его в ту же строку, сразу после «=»: {TOKEN_KEY}=2-123456-…")
        return f"В {env} строка «{TOKEN_KEY}=» пустая. Вставьте токен сразу после «=» и сохраните файл."
    similar = [m.group(1) for m in map(_LINE_RE.match, lines) if m and "TOKEN" in m.group(1).upper()]
    if similar:
        return (f"В {env} нет строки «{TOKEN_KEY}=…», но есть «{similar[0]}=…». "
                f"Имя должно быть ровно {TOKEN_KEY}.")
    return f"В {env} нет строки «{TOKEN_KEY}=…». Добавьте её: {TOKEN_KEY}=2-123456-…"
