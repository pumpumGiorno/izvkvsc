"""Файл состояния state.json: что уже найдено, что решено, какие плейлисты созданы.

Пишется атомарно (временный файл + os.replace), поэтому Ctrl+C или падение
в момент сохранения не портят прошлую версию.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

VERSION = 1

# Статусы трека в state.json
AUTO = "auto"  # уверенное совпадение
MANUAL = "manual"  # выбран вручную
SKIPPED = "skipped"  # пропущен пользователем
NOT_FOUND = "not_found"  # ничего похожего
PENDING = "pending"  # нужен ручной выбор, но спросить было нельзя (--no-input)

MATCHED = (AUTO, MANUAL)
DECIDED = (AUTO, MANUAL, SKIPPED, NOT_FOUND)


class StateError(Exception):
    pass


class State:
    def __init__(self, path: Path, data: Optional[dict] = None) -> None:
        self.path = path
        self.data = data or {"version": VERSION, "tracks": {}, "playlists": [], "pending_create": None}

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise StateError(
                f"{path} повреждён ({e}). Переименуйте его, чтобы начать заново, или восстановите из копии."
            ) from e
        if data.get("version") != VERSION:
            raise StateError(f"{path}: неизвестная версия формата {data.get('version')!r}.")
        data.setdefault("tracks", {})
        data.setdefault("playlists", [])
        data.setdefault("pending_create", None)
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.path.parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- треки ----------

    @property
    def tracks(self) -> dict:
        return self.data["tracks"]

    def get(self, key: str) -> Optional[dict]:
        return self.tracks.get(key)

    def put(self, key: str, entry: dict) -> None:
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.tracks[key] = entry
        self.save()

    # ---------- плейлист ----------

    @property
    def playlist_title(self) -> Optional[str]:
        return self.data.get("playlist_title")

    @playlist_title.setter
    def playlist_title(self, value: str) -> None:
        self.data["playlist_title"] = value

    @property
    def playlists(self) -> list:
        return self.data["playlists"]

    @property
    def pending_create(self) -> Optional[dict]:
        return self.data.get("pending_create")

    @pending_create.setter
    def pending_create(self, value: Optional[dict]) -> None:
        self.data["pending_create"] = value
