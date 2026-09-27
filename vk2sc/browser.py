"""Скрипт для консоли браузера, который создаёт плейлисты прямо на soundcloud.com.

Изменения в аккаунте (создание плейлиста, замена списка треков) api-v2 SoundCloud
закрыты защитой от ботов DataDome: запрос из Python получает 403 с капчей, хотя
токен верный. Из вкладки soundcloud.com те же запросы проходят — у браузера есть
cookie DataDome, а сайт разрешает заголовок X-Datadome-ClientId. Поэтому программа
пишет готовый скрипт с планом (названия и id треков), а пользователь вставляет его
в консоль, как vk_export.js.

Скрипт не создаёт дублей: плейлист ищется по id, запомненному в localStorage при
прошлом запуске, или по названию и метке «vk2sc» в описании, и тогда его список
треков заменяется целиком.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .soundcloud import PLAYLIST_DESCRIPTION, PLAYLIST_MARK

SCRIPT_NAME = "soundcloud_playlists.js"

_TEMPLATE = r"""// vk2sc: создание плейлистов SoundCloud из браузера (%%DATE%%).
// Откройте https://soundcloud.com в браузере, где вы вошли в аккаунт, нажмите F12,
// перейдите на вкладку Console (Консоль), вставьте этот файл целиком и нажмите Enter.
// Если SoundCloud попросит проверку «я не робот», закройте F12 и пройдите её — скрипт продолжит сам.
// Повторный запуск не создаёт дублей: те же плейлисты просто обновятся.
window.vk2scRun = (async () => {
  "use strict";
  const PLAN = %%PLAN%%;
  const SHARING = %%SHARING%%;
  const FALLBACK_CLIENT_ID = %%CLIENT_ID%%;
  const MARK = %%MARK%%;
  const DESCRIPTION = %%DESCRIPTION%%;
  const STORE = "vk2sc_playlists";
  const API = "https://api-v2.soundcloud.com";
  const PAUSE_MS = %%PAUSE_MS%%;
  const CAPTCHA_POLL_MS = %%CAPTCHA_POLL_MS%%;
  const CAPTCHA_WAIT_MS = %%CAPTCHA_WAIT_MS%%;

  const log = (...a) => console.log("%cvk2sc", "color:#f50;font-weight:bold", ...a);
  const fail = (...a) => console.error("vk2sc:", ...a);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cookie = (name) => {
    const row = document.cookie.split("; ").find((c) => c.startsWith(name + "="));
    return row ? decodeURIComponent(row.slice(name.length + 1)) : "";
  };
  // Ход работы виден и с закрытой панелью разработчика: в заголовке вкладки и в плашке внизу слева.
  const status = (text) => {
    try {
      document.title = "vk2sc: " + text;
      let box = document.getElementById("vk2sc-status");
      if (!box) {
        box = document.createElement("div");
        box.id = "vk2sc-status";
        box.style.cssText = "position:fixed;left:12px;bottom:12px;z-index:2147483000;max-width:440px;" +
          "padding:10px 14px;background:#111;color:#fff;font:14px/1.4 sans-serif;border-radius:6px;" +
          "box-shadow:0 2px 12px rgba(0,0,0,.4);white-space:pre-line";
        document.body.appendChild(box);
      }
      box.textContent = "vk2sc: " + text;
    } catch (e) { /* не страшно */ }
  };
  // На soundcloud.com запросы к api-v2 перехватывает скрипт DataDome и сам добавляет свой
  // заголовок; добавлять его вручную тогда нельзя — можно только помешать.
  const ddTag = !!(window.ddjskey || window.ddoptions ||
    document.querySelector && document.querySelector('script[src*="datadome"], script[src*="/tags.js"]'));

  // Проверка «я не робот» не решается при открытой панели разработчика. Ждём, пока человек
  // закроет её и пройдёт проверку: DataDome после этого выдаёт новую cookie «datadome».
  async function waitForCaptcha(before) {
    const started = Date.now();
    while (Date.now() - started < CAPTCHA_WAIT_MS) {
      await sleep(CAPTCHA_POLL_MS);
      const now = cookie("datadome");
      if (now && now !== before) return true;
      // cookie не читается из скрипта — просто пробуем снова раз в минуту
      if (!before && !now && Date.now() - started >= 60000) return true;
    }
    return false;
  }

  if (!/(^|\.)soundcloud\.com$/.test(location.hostname)) {
    fail("Откройте https://soundcloud.com, войдите в аккаунт и вставьте скрипт в консоль этой вкладки.");
    return { ok: false };
  }
  let token = cookie("oauth_token");
  if (!token) {
    token = (prompt("vk2sc: не нашёл oauth_token в cookie. Вставьте токен SoundCloud (из файла .env):") || "")
      .replace(/^OAuth\s+/i, "").trim();
  }
  if (!token) {
    fail("Нет токена: войдите в аккаунт на soundcloud.com и запустите скрипт снова.");
    return { ok: false };
  }
  // client_id самого сайта надёжнее сохранённого: берём его из запросов, которые уже сделала страница.
  let clientId = FALLBACK_CLIENT_ID;
  for (const e of performance.getEntriesByType("resource")) {
    const m = /[?&]client_id=([0-9A-Za-z]{32})/.exec(e.name);
    if (m) { clientId = m[1]; break; }
  }

  async function api(method, path, body) {
    let url = path.startsWith("http") ? path : API + path;
    if (!url.startsWith(API + "/")) throw new Error("неожиданный адрес " + url.slice(0, 80));
    if (!/[?&]client_id=/.test(url)) url += (url.includes("?") ? "&" : "?") + "client_id=" + clientId;
    let captchas = 0;
    for (let attempt = 1; ; attempt++) {
      const headers = { Authorization: "OAuth " + token, Accept: "application/json" };
      if (body) headers["Content-Type"] = "application/json";
      const dd = cookie("datadome");
      if (dd && !ddTag) headers["X-Datadome-ClientId"] = dd;
      let resp;
      try {
        resp = await fetch(url, { method, headers, credentials: "include", body: body ? JSON.stringify(body) : undefined });
      } catch (e) {
        if (attempt >= 4) throw new Error("сеть недоступна: " + e.message);
        await sleep(3000 * attempt);
        continue;
      }
      const text = await resp.text();
      if ((resp.status === 429 || resp.status >= 500) && attempt < 4) {
        log(`SoundCloud ответил ${resp.status}, жду и повторяю…`);
        await sleep(10000 * attempt);
        continue;
      }
      if (resp.status === 403 && text.includes("captcha-delivery.com")) {
        if (++captchas > 3) {
          const err = new Error("SoundCloud снова и снова просит проверку «я не робот». Подождите 10–15 минут, " +
            "обновите страницу и вставьте скрипт снова — уже созданные плейлисты не задвоятся.");
          err.fatal = true;
          throw err;
        }
        log("SoundCloud просит пройти проверку «я не робот». ЗАКРОЙТЕ панель разработчика (F12) и пройдите " +
          "проверку на странице — скрипт подождёт и продолжит сам. Потом снова откройте F12, чтобы увидеть результат.");
        status("закройте панель разработчика (F12) и пройдите проверку «я не робот» — я подожду и продолжу сам…");
        if (!(await waitForCaptcha(dd))) {
          const err = new Error("Проверка «я не робот» не пройдена за " + Math.round(CAPTCHA_WAIT_MS / 60000) +
            " мин. Обновите страницу, пройдите проверку и вставьте скрипт снова — уже созданное не задвоится.");
          err.fatal = true;
          throw err;
        }
        log("Проверка пройдена, продолжаю.");
        status("проверка пройдена, продолжаю…");
        attempt = 0;
        continue;
      }
      if (resp.status === 401) {
        const err = new Error("SoundCloud не принял токен (HTTP 401). Выйдите и снова войдите на soundcloud.com.");
        err.fatal = true;
        throw err;
      }
      if (!resp.ok) {
        const err = new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
        err.status = resp.status;
        throw err;
      }
      return text ? JSON.parse(text) : {};
    }
  }

  let me;
  try {
    me = await api("GET", "/me");
  } catch (e) {
    fail("Не удалось получить профиль: " + e.message);
    return { ok: false };
  }
  log(`Аккаунт: ${me.username}. Плейлистов: ${PLAN.length}, треков: ${PLAN.reduce((n, p) => n + p.tracks.length, 0)}.`);

  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE) || "{}") || {}; } catch (e) { saved = {}; }
  const mine = [];
  try {
    let next = `/users/${me.id}/playlists_without_albums?limit=50&linked_partitioning=1`;
    for (let page = 0; next && page < 40; page++) {
      const data = await api("GET", next);
      mine.push(...(data.collection || []));
      next = data.next_href || null;
    }
  } catch (e) {
    fail("Не удалось получить список ваших плейлистов: " + e.message);
    return { ok: false };
  }

  const results = [];
  let stopped = false;
  for (const item of PLAN) {
    const existing = mine.find((p) => p.id === saved[item.title]) ||
      mine.find((p) => p.title === item.title && (p.description || "").includes(MARK));
    let data = null;
    try {
      if (existing) {
        log(`Обновляю «${item.title}»: ${item.tracks.length} треков…`);
        status(`обновляю «${item.title}» (${item.tracks.length} треков)…`);
        data = await api("PUT", `/playlists/${existing.id}`, { playlist: { tracks: item.tracks } });
      } else {
        log(`Создаю «${item.title}»: ${item.tracks.length} треков…`);
        status(`создаю «${item.title}» (${item.tracks.length} треков)…`);
        const playlist = { title: item.title, sharing: SHARING, description: DESCRIPTION, tracks: item.tracks };
        try {
          data = await api("POST", "/playlists", { playlist });
        } catch (e) {
          if (e.status !== 400 && e.status !== 422) throw e;
          delete playlist.description;
          data = await api("POST", "/playlists", { playlist });
        }
      }
    } catch (e) {
      fail(`«${item.title}»: ${e.message}`);
      results.push({ плейлист: item.title, треков: 0, ожидалось: item.tracks.length, ссылка: "ошибка" });
      if (e.fatal) { stopped = true; break; }
      continue;
    }
    saved[item.title] = data.id;
    try { localStorage.setItem(STORE, JSON.stringify(saved)); } catch (e) { /* приватный режим */ }
    const count = typeof data.track_count === "number" ? data.track_count : (data.tracks || []).length;
    results.push({ плейлист: item.title, треков: count, ожидалось: item.tracks.length, ссылка: data.permalink_url || "" });
    await sleep(PAUSE_MS);
  }

  console.table(results);
  const ok = !stopped && results.length === PLAN.length && results.every((r) => r.треков === r.ожидалось);
  if (ok) {
    log("Готово. Теперь запустите в папке программы «python -m vk2sc» ещё раз — она найдёт эти плейлисты и допишет отчёт.");
    status("готово ✓ " + results.map((r) => `«${r.плейлист}» — ${r.треков} треков`).join(", ") +
      ". Запустите python -m vk2sc ещё раз.");
  } else {
    status("не всё получилось — откройте F12 → Console, там подробности. Скрипт можно вставить снова.");
    fail("Не всё получилось (см. таблицу и сообщения выше). Запустите скрипт ещё раз — уже созданное не задвоится.");
  }
  return { ok, results };
})();
"""


def build_script(plan: list[tuple[str, list[int]]], sharing: str, client_id: Optional[str],
                 pause_ms: int = 1500, captcha_poll_ms: int = 2000, captcha_wait_ms: int = 15 * 60 * 1000) -> str:
    items = [{"title": title, "tracks": [int(t) for t in ids]} for title, ids in plan]
    values = {
        "%%DATE%%": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "%%PLAN%%": json.dumps(items, ensure_ascii=False, separators=(",", ":")),
        "%%SHARING%%": json.dumps(sharing),
        "%%CLIENT_ID%%": json.dumps(client_id or ""),
        "%%MARK%%": json.dumps(PLAYLIST_MARK),
        "%%DESCRIPTION%%": json.dumps(PLAYLIST_DESCRIPTION, ensure_ascii=False),
        "%%PAUSE_MS%%": str(int(pause_ms)),
        "%%CAPTCHA_POLL_MS%%": str(int(captcha_poll_ms)),
        "%%CAPTCHA_WAIT_MS%%": str(int(captcha_wait_ms)),
    }
    script = _TEMPLATE
    for key, value in values.items():
        script = script.replace(key, value)
    return script


def write_script(path: Path, plan: list[tuple[str, list[int]]], sharing: str, client_id: Optional[str]) -> Path:
    # utf-8-sig: Блокнот в Windows открывает файл без «кракозябр».
    path.write_text(build_script(plan, sharing, client_id), encoding="utf-8-sig")
    return path
