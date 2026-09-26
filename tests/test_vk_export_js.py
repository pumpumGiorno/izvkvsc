"""Проверка vk_export.js в настоящем браузере на разных вариантах вёрстки.

Нужны Node.js и пакет playwright (npm i -g playwright && npx playwright install chromium).
Если их нет, тест пропускается — для работы самого скрипта они не нужны.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vk2sc.tracks import parse_line

RUNNER = Path(__file__).parent / "js" / "run_vk_export.cjs"


def _node_env():
    env = dict(os.environ)
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        root = ""
    if root:
        env["NODE_PATH"] = os.pathsep.join(filter(None, [env.get("NODE_PATH"), root]))
    return env


@pytest.fixture(scope="module")
def results():
    if not shutil.which("node"):
        pytest.skip("Node.js не установлен")
    env = _node_env()
    probe = subprocess.run(["node", "-e", "require('playwright')"], env=env, capture_output=True)
    if probe.returncode != 0:
        pytest.skip("пакет playwright для Node не установлен")
    proc = subprocess.run(["node", str(RUNNER)], env=env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 and "Executable doesn't exist" in proc.stderr:
        pytest.skip("Chromium для playwright не установлен")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def lines_of(res):
    return res["result"]["lines"]


def test_audio_row_markup_with_lazy_loading(results):
    res = results["audio_row.html"]
    assert res["error"] is None
    lines = lines_of(res)
    assert len(lines) == 50  # все 50 из «Мои треки», подгруженные прокруткой
    assert lines[0] == "Исполнитель 1 — Песня 1 | 2:01"
    assert lines[1] == "Miyagi, Andy Panda — Kosandra | 3:36"
    assert lines[2] == "Lady Gaga — Bad Romance (Skrillex Remix) | 4:54"
    assert lines[3] == "Ёлка — Прованс | 3:31"
    assert lines[4] == lines[5] == "Imagine Dragons — Believer | 3:24"  # дубль в VK сохраняется
    assert lines[-1] == "Исполнитель 50 — Песня 50 | 2:50"
    assert not any("Недавний" in l or "Плеер" in l for l in lines)
    assert any("Недавно прослушанные" in w for w in res["result"]["warnings"])
    # Файл скачан и читается нашим парсером без ошибок.
    assert res["file"]["name"] == "tracks.txt"
    body = [l for l in res["file"]["text"].splitlines() if l and not l.startswith("#")]
    assert body == lines
    assert all(parse_line(l) for l in body)


def test_data_audio_only(results):
    res = results["data_audio.html"]
    assert res["result"]["strategy"] == "атрибут data-audio"
    assert lines_of(res) == [
        "Кино — Группа крови | 4:45",
        "The Weeknd feat. Daft Punk — Starboy | 3:50",
        "Led Zeppelin — Rock & Roll | 3:40",
        "Lady Gaga — Bad Romance (Skrillex Remix) | 4:54",
        "Земфира — Хочешь? | 3:45",
    ]


def test_new_markup_in_virtual_scroller(results):
    res = results["new_markup_virtual.html"]
    lines = lines_of(res)
    assert len(lines) == 120
    assert lines[0] == "Artist 1 — Track 1 | 3:00"
    assert lines[7] == "Macan — ASPHALT 8 | 2:30"
    assert lines[-1] == "Artist 120 — Track 120 | 3:19"


def test_heuristic_fallback(results):
    res = results["heuristic.html"]
    assert lines_of(res) == [
        "Imagine Dragons — Believer | 3:24",
        "Кино — Группа крови | 4:45",
        "Miyagi, Andy Panda — Kosandra | 3:36",
    ]
    assert any("эвристикой" in w for w in res["result"]["warnings"])


def test_empty_page_gives_clear_error_and_no_file(results):
    res = results["empty.html"]
    assert res["result"] is None and res["file"] is None
    assert "треки не найдены" in res["error"]
    assert res["dialogs"] and "Не нашёл ни одной аудиозаписи" in res["dialogs"][0]
    assert any("Классы на странице" in c for c in res["console"])
