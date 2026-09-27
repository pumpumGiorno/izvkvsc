"""Клиент внутреннего API SoundCloud (api-v2.soundcloud.com), которым пользуется сам сайт.

- client_id берётся из JS-файлов soundcloud.com и кэшируется в файле;
  при ответе 401/403 клиент один раз обновляет его и повторяет запрос.
- oauth_token (cookie сайта) нужен только для /me и записи плейлистов.
- Между любыми запросами пауза не меньше min_delay (+ случайная добавка до 0,5 с).
- 429, 5xx и сетевые ошибки: повтор с экспоненциальной задержкой, всего не больше
  max_attempts попыток, после чего исключение — вызывающий код сохраняет прогресс и выходит.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

API = "https://api-v2.soundcloud.com"
SITE = "https://soundcloud.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_SCRIPT_RE = re.compile(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"')
_CLIENT_ID_RES = (
    re.compile(r'client_id\s*[:=]\s*"([0-9A-Za-z]{32})"'),
    re.compile(r"client_id=([0-9A-Za-z]{32})"),
)
MAX_RETRY_AFTER = 120  # если сервер просит ждать дольше — останавливаемся


class SoundCloudError(Exception):
    def __init__(self, message: str = "", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class RetryExhausted(SoundCloudError):
    """429/5xx/сеть после всех попыток. Прогресс нужно сохранить и остановиться."""


class AuthError(SoundCloudError):
    pass


class PlaylistNotFound(SoundCloudError):
    pass


class BlockedError(SoundCloudError):
    """Запрос отклонила защита от ботов (DataDome), а не токен: изменения в аккаунте
    SoundCloud пускает только из настоящего браузера. См. browser.py."""


# Метка в описании плейлистов, созданных программой: по ней их находят повторные запуски
# и браузерный скрипт, не трогая другие плейлисты с таким же названием.
PLAYLIST_MARK = "vk2sc"
PLAYLIST_DESCRIPTION = "Перенесено из VK (vk2sc)"


def is_datadome_block(resp: requests.Response) -> bool:
    """403 от DataDome: заголовок x-datadome и ссылка на капчу в теле."""
    if resp.status_code != 403:
        return False
    headers = {k.lower(): v.lower() for k, v in resp.headers.items()}
    return "x-datadome" in headers or "captcha-delivery.com" in resp.text[:2000]


class Redactor(logging.Filter):
    """Вычищает client_id и токены из логов, даже если они попали в текст ошибки."""

    secrets: set[str] = set()

    @classmethod
    def add(cls, secret: Optional[str]) -> None:
        if secret and len(secret) >= 8:
            cls.secrets.add(secret)

    @classmethod
    def clean(cls, text: str) -> str:
        for s in cls.secrets:
            text = text.replace(s, "***")
        return re.sub(r"(client_id=|OAuth\s+)[\w.-]+", r"\1***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.clean(record.getMessage())
        record.args = ()
        return True


class SoundCloudClient:
    def __init__(
        self,
        oauth_token: Optional[str] = None,
        cache_path: Path = Path(".cache/soundcloud.json"),
        min_delay: float = 1.5,
        max_attempts: int = 3,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.oauth_token: Optional[str] = None
        self.set_oauth_token(oauth_token)
        self.cache_path = cache_path
        self.min_delay = min_delay
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": "https://soundcloud.com",
                "Referer": "https://soundcloud.com/",
            }
        )
        self._sleep = sleep
        self._clock = clock
        self._last_request: Optional[float] = None
        self._client_id: Optional[str] = None
        self.request_count = 0

    def set_oauth_token(self, token: Optional[str]) -> None:
        """Принимает и «2-123-…», и «OAuth 2-123-…» (как в заголовке запроса)."""
        token = (token or "").strip().strip('"').strip("'")
        if token.lower().startswith("oauth "):
            token = token[6:].strip()
        self.oauth_token = token or None
        Redactor.add(self.oauth_token)

    # ---------- низкий уровень ----------

    def _throttle(self) -> None:
        if self._last_request is None:
            return
        pause = self.min_delay + random.uniform(0, 0.5)
        wait = self._last_request + pause - self._clock()
        if wait > 0:
            self._sleep(wait)

    def _backoff(self, attempt: int, base: float) -> float:
        return base * 2 ** (attempt - 1) + random.uniform(0, 1)

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Запрос с паузой и повторами. Возвращает ответ с любым статусом, кроме 429/5xx."""
        last_problem = ""
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                self.request_count += 1
                resp = self.session.request(method, url, timeout=(10, 30), **kwargs)
            except requests.RequestException as e:
                last_problem = f"сетевая ошибка ({type(e).__name__})"
                wait = self._backoff(attempt, 3)
            else:
                if resp.status_code != 429 and resp.status_code < 500:
                    return resp
                last_problem = f"HTTP {resp.status_code}"
                wait = self._backoff(attempt, 10 if resp.status_code == 429 else 3)
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if retry_after is not None:
                    if retry_after > MAX_RETRY_AFTER:
                        raise RetryExhausted(
                            f"SoundCloud просит подождать {retry_after} с ({last_problem}). Попробуйте позже."
                        )
                    wait = max(wait, retry_after)
            finally:
                self._last_request = self._clock()

            if attempt == self.max_attempts:
                break
            log.warning(
                "SoundCloud: %s, жду %.0f с и повторяю (попытка %d из %d)",
                last_problem, wait, attempt + 1, self.max_attempts,
            )
            self._sleep(wait)
        raise RetryExhausted(f"{last_problem} — {self.max_attempts} неудачные попытки подряд")

    # ---------- client_id ----------

    @property
    def client_id(self) -> str:
        if self._client_id is None:
            self._client_id = self._load_cached_client_id() or self.refresh_client_id()
        return self._client_id

    def _load_cached_client_id(self) -> Optional[str]:
        try:
            cid = json.loads(self.cache_path.read_text(encoding="utf-8")).get("client_id")
        except (OSError, ValueError):
            return None
        if cid:
            Redactor.add(cid)
        return cid or None

    def refresh_client_id(self) -> str:
        log.info("Получаю client_id со страницы soundcloud.com")
        resp = self._send("GET", SITE)
        if resp.status_code != 200:
            raise SoundCloudError(f"soundcloud.com ответил HTTP {resp.status_code}")
        scripts = _SCRIPT_RE.findall(resp.text)
        # client_id обычно в одном из последних скриптов.
        for url in reversed(scripts):
            js = self._send("GET", url)
            if js.status_code != 200:
                continue
            for rx in _CLIENT_ID_RES:
                m = rx.search(js.text)
                if m:
                    cid = m.group(1)
                    Redactor.add(cid)
                    self._client_id = cid
                    self._save_client_id(cid)
                    return cid
        raise SoundCloudError(
            "Не удалось найти client_id на soundcloud.com — возможно, сайт поменял вёрстку."
        )

    def _save_client_id(self, cid: str) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"client_id": cid, "fetched_at": int(time.time())}), encoding="utf-8"
            )
        except OSError as e:
            log.warning("Не удалось сохранить кэш client_id: %s", e)

    # ---------- API ----------

    def _api(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None,
             auth: bool = False) -> Optional[dict]:
        if auth and not self.oauth_token:
            raise AuthError("Нужен SOUNDCLOUD_OAUTH_TOKEN в .env")
        headers = {"Authorization": f"OAuth {self.oauth_token}"} if auth else {}
        url = path if path.startswith("http") else API + path
        if not url.startswith(API + "/"):
            # next_href приходит из ответа — токен уходит только на api-v2.soundcloud.com.
            raise SoundCloudError(f"Неожиданный адрес API: {Redactor.clean(url[:100])}")
        refreshed = False
        while True:
            query = dict(params or {}, client_id=self.client_id)
            resp = self._send(method, url, params=query, json=body, headers=headers)
            if resp.status_code >= 400:
                log.debug("SoundCloud %s %s → HTTP %s: %s", method, url.replace(API, ""), resp.status_code,
                          Redactor.clean(resp.text[:300]))
            if is_datadome_block(resp):
                # Обновление client_id тут не поможет — только лишние запросы.
                raise BlockedError(
                    "SoundCloud пропускает изменения в аккаунте только из браузера (защита от ботов DataDome). "
                    "Токен при этом в порядке.", status=403)
            if resp.status_code in (401, 403) and not refreshed:
                log.info("SoundCloud ответил %s — обновляю client_id", resp.status_code)
                self.refresh_client_id()
                refreshed = True
                continue
            break
        if resp.status_code in (401, 403):
            hint = (
                "Проверьте SOUNDCLOUD_OAUTH_TOKEN в .env: возможно, он устарел (перевойдите на сайте и скопируйте заново)."
                if auth else "client_id не принимается даже после обновления."
            )
            raise AuthError(f"SoundCloud отклонил запрос (HTTP {resp.status_code}). {hint}", status=resp.status_code)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise SoundCloudError(f"HTTP {resp.status_code}: {Redactor.clean(resp.text[:300])}", status=resp.status_code)
        return resp.json() if resp.content else {}

    def search_tracks(self, query: str, limit: int = 20) -> list[dict]:
        data = self._api("GET", "/search/tracks", {"q": query, "limit": limit, "offset": 0,
                                                  "linked_partitioning": 1})
        return [t for t in (data or {}).get("collection", []) if t.get("kind", "track") == "track"]

    def resolve(self, url: str) -> Optional[dict]:
        return self._api("GET", "/resolve", {"url": url})

    def me(self) -> dict:
        data = self._api("GET", "/me", auth=True)
        if not data:
            raise AuthError("Не удалось получить профиль SoundCloud по токену.")
        return data

    def user_playlists(self, user_id: int, max_pages: int = 20) -> list[dict]:
        result: list[dict] = []
        path: Optional[str] = f"/users/{user_id}/playlists_without_albums"
        params: Optional[dict] = {"limit": 50, "linked_partitioning": 1}
        for _ in range(max_pages):
            data = self._api("GET", path, params, auth=True) or {}
            result += data.get("collection", [])
            path, params = data.get("next_href"), None
            if not path:
                break
        return result

    def create_playlist(self, title: str, track_ids: list[int], sharing: str = "private") -> dict:
        body = {"playlist": {"title": title, "sharing": sharing, "description": PLAYLIST_DESCRIPTION,
                             "tracks": track_ids}}
        try:
            data = self._api("POST", "/playlists", body=body, auth=True)
        except SoundCloudError as e:
            if e.status not in (400, 422):
                raise
            del body["playlist"]["description"]  # на случай, если описание при создании не принимается
            data = self._api("POST", "/playlists", body=body, auth=True)
        if not data or "id" not in data:
            raise SoundCloudError("SoundCloud не вернул id созданного плейлиста.")
        return data

    def set_playlist_tracks(self, playlist_id: int, track_ids: list[int]) -> dict:
        """Заменяет список треков целиком: повторный вызов не создаёт дублей."""
        body = {"playlist": {"tracks": track_ids}}
        data = self._api("PUT", f"/playlists/{playlist_id}", body=body, auth=True)
        if data is None:
            raise PlaylistNotFound(f"Плейлист {playlist_id} не найден.")
        return data


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        return None


def playlist_track_count(data: dict) -> Optional[int]:
    if isinstance(data.get("track_count"), int):
        return data["track_count"]
    if isinstance(data.get("tracks"), list):
        return len(data["tracks"])
    return None
