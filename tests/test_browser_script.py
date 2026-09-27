"""soundcloud_playlists.js: запуск в Node с поддельными soundcloud.com и api-v2 (без браузера)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vk2sc.browser import build_script

RUNNER = Path(__file__).parent / "js" / "run_sc_script.cjs"
PLAN = [("Из VK", [1, 2, 3]), ("Из VK (2)", [4, 5])]


def run_script(tmp_path, scenario="ok", runs=1, plan=PLAN):
    if not shutil.which("node"):
        pytest.skip("Node.js не установлен")
    script = tmp_path / "soundcloud_playlists.js"
    script.write_text(build_script(plan, "private", "F" * 32, pause_ms=0, captcha_poll_ms=10, captcha_wait_ms=300),
                      encoding="utf-8-sig")
    proc = subprocess.run(["node", str(RUNNER), str(script), scenario, str(runs)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def writes(data):
    return [(r["method"], r["path"]) for r in data["requests"] if r["method"] in ("POST", "PUT")]


def test_creates_playlists_with_browser_credentials(tmp_path):
    data = run_script(tmp_path)
    assert data["results"][0]["ok"] is True
    assert writes(data) == [("POST", "/playlists"), ("POST", "/playlists")]
    post = next(r for r in data["requests"] if r["method"] == "POST")
    assert post["headers"]["Authorization"] == "OAuth 2-111-222-TokenFromCookie"
    assert post["headers"]["X-Datadome-ClientId"] == "DDcookieValue"
    assert post["credentials"] == "include"
    assert post["clientId"] == "P" * 32  # client_id самой страницы, а не сохранённый
    assert post["body"] == {"playlist": {"title": "Из VK", "sharing": "private",
                                         "description": "Перенесено из VK (vk2sc)", "tracks": [1, 2, 3]}}
    assert any("Готово" in line for line in data["logs"])


def test_second_run_updates_instead_of_duplicating(tmp_path):
    data = run_script(tmp_path, runs=2)
    assert writes(data) == [("POST", "/playlists"), ("POST", "/playlists"),
                            ("PUT", "/playlists/1000"), ("PUT", "/playlists/1001")]
    assert len(data["playlists"]) == 2 and all(r["ok"] for r in data["results"])
    assert json.loads(data["storage"]["vk2sc_playlists"]) == {"Из VK": 1000, "Из VK (2)": 1001}


def test_finds_marked_playlist_and_ignores_unmarked_one(tmp_path):
    data = run_script(tmp_path, scenario="existing_mark")
    # «Из VK» с меткой обновляется, «Из VK (2)» без метки — чужой, создаётся новый.
    assert writes(data) == [("PUT", "/playlists/555"), ("POST", "/playlists")]
    assert [p["track_count"] for p in data["playlists"]] == [3, 0, 2]


def test_captcha_waits_for_human_then_continues(tmp_path):
    """Реальный случай: POST из консоли получил капчу DataDome. Скрипт ждёт, пока человек
    закроет F12 и пройдёт проверку (меняется cookie datadome), и продолжает сам."""
    data = run_script(tmp_path, scenario="captcha_once")
    assert data["results"][0]["ok"] is True
    assert writes(data) == [("POST", "/playlists"), ("POST", "/playlists"), ("POST", "/playlists")]
    assert any("ЗАКРОЙТЕ панель разработчика" in line for line in data["logs"])
    assert data["title"].startswith("vk2sc: готово") and "«Из VK» — 3 треков" in data["status"]


def test_captcha_not_solved_stops_after_timeout(tmp_path):
    data = run_script(tmp_path, scenario="captcha")
    assert data["results"][0]["ok"] is False
    assert writes(data) == [("POST", "/playlists")]  # без пройденной проверки запрос не повторяет
    assert any("не пройдена" in line for line in data["logs"])
    assert "не всё получилось" in data["status"]


def test_no_manual_datadome_header_when_page_has_datadome_tag(tmp_path):
    data = run_script(tmp_path, scenario="dd_tag", plan=PLAN[:1])
    post = next(r for r in data["requests"] if r["method"] == "POST")
    assert "X-Datadome-ClientId" not in post["headers"] and post["credentials"] == "include"


def test_description_rejected_retries_without_it(tmp_path):
    data = run_script(tmp_path, scenario="reject_description", plan=PLAN[:1])
    posts = [r["body"]["playlist"] for r in data["requests"] if r["method"] == "POST"]
    assert "description" in posts[0] and "description" not in posts[1]
    assert data["results"][0]["ok"] is True
