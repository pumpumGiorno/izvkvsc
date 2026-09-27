"""Чтение .env и подсказки, если токен не прочитался (частые ошибки в Windows)."""

import os

import pytest

from vk2sc import envfile
from vk2sc.envfile import TOKEN_KEY, diagnose_token, load_env

TOKEN = "2-123456-78901234-AbCdEfGhIj"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(envfile, "env_candidates", lambda: [tmp_path / ".env"])
    monkeypatch.delenv(TOKEN_KEY, raising=False)
    monkeypatch.delenv("REQUEST_DELAY", raising=False)
    yield tmp_path
    os.environ.pop(TOKEN_KEY, None)  # load_env пишет в os.environ напрямую
    os.environ.pop("REQUEST_DELAY", None)


@pytest.mark.parametrize(
    "data",
    [
        f"{TOKEN_KEY}={TOKEN}\n".encode(),
        ("﻿" + f"{TOKEN_KEY}={TOKEN}\n").encode("utf-8"),  # Блокнот: «UTF-8 с BOM»
        ("﻿" + f"{TOKEN_KEY}={TOKEN}\r\n").encode("utf-16-le"),  # echo … > .env в PowerShell 5
        f'{TOKEN_KEY} = "{TOKEN}"\r\n'.encode(),
        f"# комментарий\n{TOKEN_KEY}={TOKEN}\nREQUEST_DELAY=1.5\n".encode(),
    ],
)
def test_token_is_read_in_any_windows_encoding(folder, data):
    (folder / ".env").write_bytes(data)
    assert load_env() == folder / ".env"
    assert os.environ[TOKEN_KEY] == TOKEN


def test_empty_environment_variable_does_not_hide_file_value(folder, monkeypatch):
    monkeypatch.setenv(TOKEN_KEY, "")
    (folder / ".env").write_text(f"{TOKEN_KEY}={TOKEN}\n", encoding="utf-8")
    load_env()
    assert os.environ[TOKEN_KEY] == TOKEN


def test_real_environment_variable_wins(folder, monkeypatch):
    monkeypatch.setenv(TOKEN_KEY, "2-1-1-fromenv")
    (folder / ".env").write_text(f"{TOKEN_KEY}={TOKEN}\n", encoding="utf-8")
    load_env()
    assert os.environ[TOKEN_KEY] == "2-1-1-fromenv"


def test_diagnose_missing_file(folder):
    assert "нет файла .env" in diagnose_token()


def test_diagnose_env_txt(folder):
    (folder / ".env.txt").write_text(f"{TOKEN_KEY}={TOKEN}\n", encoding="utf-8")
    message = diagnose_token()
    assert "«.env.txt»" in message and "Rename-Item" in message


def test_diagnose_token_on_next_line(folder):
    (folder / ".env").write_text(f"# токен\n{TOKEN_KEY}=\n{TOKEN}\nREQUEST_DELAY=1.5\n", encoding="utf-8")
    load_env()
    assert not os.environ.get(TOKEN_KEY)
    message = diagnose_token()
    assert "на отдельной строке" in message and TOKEN not in message


def test_diagnose_empty_value(folder):
    (folder / ".env").write_text(f"{TOKEN_KEY}=\nREQUEST_DELAY=1.5\n", encoding="utf-8")
    assert "пустая" in diagnose_token()


def test_diagnose_commented_line(folder):
    (folder / ".env").write_text(f"# {TOKEN_KEY}={TOKEN}\n", encoding="utf-8")
    message = diagnose_token()
    assert "закомментирована" in message and TOKEN not in message


def test_diagnose_misspelled_key(folder):
    (folder / ".env").write_text(f"SOUNDCLOUD_TOKEN={TOKEN}\n", encoding="utf-8")
    message = diagnose_token()
    assert "SOUNDCLOUD_TOKEN" in message and TOKEN not in message


def test_cli_explains_missing_token(folder):
    from vk2sc.cli import main

    (folder / "tracks.txt").write_text("Imagine Dragons — Believer\n", encoding="utf-8")
    (folder / ".env").write_text(f"{TOKEN_KEY}=\n{TOKEN}\n", encoding="utf-8")
    from test_cli import FakeClient

    lines = []
    code = main(["--yes"], client=FakeClient(), interactive=False, out=lambda *a: lines.append(" ".join(map(str, a))))
    out = "\n".join(lines)
    assert code == 1 and "на отдельной строке" in out and TOKEN not in out
