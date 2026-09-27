import json
import logging

import pytest
import requests
import responses

from vk2sc.soundcloud import API, SITE, AuthError, BlockedError, Redactor, RetryExhausted, SoundCloudClient

CID = "A" * 32
CID2 = "B" * 32
TOKEN = "2-123456-987654-SecretTokenValue"
HOME = '<html><script src="https://a-v2.sndcdn.com/assets/0-aaa.js"></script>' \
       '<script src="https://a-v2.sndcdn.com/assets/49-bbb.js"></script></html>'


class Sleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def make_client(tmp_path, **kw):
    sleeper = Sleeper()
    kw.setdefault("sleep", sleeper)
    kw.setdefault("clock", lambda: 0.0)
    client = SoundCloudClient(cache_path=tmp_path / "cache.json", **kw)
    return client, sleeper


def mock_home(cid=CID):
    responses.add(responses.GET, SITE, body=HOME)
    responses.add(responses.GET, "https://a-v2.sndcdn.com/assets/49-bbb.js", body=f'x={{client_id:"{cid}",y:1}}')


def search_url():
    return API + "/search/tracks"


@responses.activate
def test_client_id_is_scraped_and_cached(tmp_path):
    mock_home()
    responses.add(responses.GET, search_url(), json={"collection": [{"id": 1, "kind": "track"}]})
    client, _ = make_client(tmp_path)
    assert client.search_tracks("q") == [{"id": 1, "kind": "track"}]
    assert json.loads((tmp_path / "cache.json").read_text())["client_id"] == CID
    assert f"client_id={CID}" in responses.calls[-1].request.url

    # Второй запуск берёт client_id из кэша и не ходит на главную.
    responses.calls.reset()
    client2, _ = make_client(tmp_path)
    client2.search_tracks("q")
    assert [c.request.url.split("?")[0] for c in responses.calls] == [search_url()]


@responses.activate
def test_client_id_refreshed_once_on_401(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.GET, search_url(), status=401)
    mock_home(CID2)
    responses.add(responses.GET, search_url(), json={"collection": []})
    client, _ = make_client(tmp_path)
    assert client.search_tracks("q") == []
    assert f"client_id={CID2}" in responses.calls[-1].request.url
    assert json.loads((tmp_path / "cache.json").read_text())["client_id"] == CID2


@responses.activate
def test_persistent_403_raises_auth_error(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.GET, search_url(), status=403)
    mock_home(CID2)
    client, _ = make_client(tmp_path)
    with pytest.raises(AuthError):
        client.search_tracks("q")
    # Исходный запрос, обновление client_id (2 запроса), повтор — и всё, без бесконечного цикла.
    assert len(responses.calls) == 4


@responses.activate
def test_429_backoff_then_success(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.GET, search_url(), status=429)
    responses.add(responses.GET, search_url(), json={"collection": []})
    client, sleeper = make_client(tmp_path)
    assert client.search_tracks("q") == []
    assert any(s >= 10 for s in sleeper.calls)


@responses.activate
def test_429_three_times_stops(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    for _ in range(5):
        responses.add(responses.GET, search_url(), status=429)
    client, sleeper = make_client(tmp_path, min_delay=1.0)
    with pytest.raises(RetryExhausted):
        client.search_tracks("q")
    assert len(responses.calls) == 3
    backoffs = [s for s in sleeper.calls if s >= 10]
    assert len(backoffs) == 2 and backoffs[1] > backoffs[0]  # экспоненциальный рост


@responses.activate
def test_network_errors_three_times_stop(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    for _ in range(3):
        responses.add(responses.GET, search_url(), body=requests.ConnectionError("boom"))
    client, _ = make_client(tmp_path)
    with pytest.raises(RetryExhausted):
        client.search_tracks("q")
    assert len(responses.calls) == 3


@responses.activate
def test_huge_retry_after_stops_immediately(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.GET, search_url(), status=429, headers={"Retry-After": "3600"})
    client, _ = make_client(tmp_path)
    with pytest.raises(RetryExhausted, match="3600"):
        client.search_tracks("q")
    assert len(responses.calls) == 1


@responses.activate
def test_pause_between_requests(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.GET, search_url(), json={"collection": []})
    responses.add(responses.GET, search_url(), json={"collection": []})
    client, sleeper = make_client(tmp_path, min_delay=1.5)
    client.search_tracks("a")
    client.search_tracks("b")
    assert len(sleeper.calls) == 1 and 1.5 <= sleeper.calls[0] <= 2.0


@responses.activate
def test_create_and_update_playlist_bodies(tmp_path, caplog):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.POST, API + "/playlists", json={"id": 77, "track_count": 2}, status=201)
    responses.add(responses.PUT, API + "/playlists/77", json={"id": 77, "track_count": 3})
    client, _ = make_client(tmp_path, oauth_token=TOKEN)
    with caplog.at_level(logging.DEBUG):
        client.create_playlist("Из VK", [1, 2], "private")
        client.set_playlist_tracks(77, [1, 2, 3])
    post, put = responses.calls[0].request, responses.calls[1].request
    assert json.loads(post.body) == {"playlist": {"title": "Из VK", "sharing": "private",
                                                  "description": "Перенесено из VK (vk2sc)", "tracks": [1, 2]}}
    assert json.loads(put.body) == {"playlist": {"tracks": [1, 2, 3]}}
    assert post.headers["Authorization"] == f"OAuth {TOKEN}"


def test_write_without_token_is_refused(tmp_path):
    client, _ = make_client(tmp_path)
    with pytest.raises(AuthError):
        client.create_playlist("x", [1])


def test_redactor_hides_secrets():
    Redactor.add(TOKEN)
    text = f"GET https://api-v2.soundcloud.com/me?client_id={CID} Authorization: OAuth {TOKEN}"
    cleaned = Redactor.clean(text)
    assert TOKEN not in cleaned and CID not in cleaned


def test_redactor_filter_in_logs(caplog):
    Redactor.add(TOKEN)
    logger = logging.getLogger("test-redact")
    handler = logging.StreamHandler()
    handler.addFilter(Redactor())
    record = logger.makeRecord("x", logging.WARNING, __file__, 1, "token %s", (TOKEN,), None)
    handler.filter(record)
    assert TOKEN not in record.getMessage()


@responses.activate
def test_user_playlists_follow_pagination(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    nxt = API + "/users/42/playlists_without_albums?offset=50&limit=50&linked_partitioning=1"
    responses.add(responses.GET, API + "/users/42/playlists_without_albums",
                  json={"collection": [{"id": 1}], "next_href": nxt})
    responses.add(responses.GET, nxt.split("?")[0], json={"collection": [{"id": 2}], "next_href": None})
    client, _ = make_client(tmp_path, oauth_token=TOKEN)
    assert [p["id"] for p in client.user_playlists(42)] == [1, 2]


def test_set_oauth_token_strips_prefix_and_quotes(tmp_path):
    client, _ = make_client(tmp_path)
    client.set_oauth_token(f'"OAuth {TOKEN}"')
    assert client.oauth_token == TOKEN


@responses.activate
def test_token_never_sent_outside_api_host(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.GET, API + "/users/42/playlists_without_albums",
                  json={"collection": [], "next_href": "https://evil.example.com/steal"})
    client, _ = make_client(tmp_path, oauth_token=TOKEN)
    with pytest.raises(Exception, match="Неожиданный адрес"):
        client.user_playlists(42)
    assert all("evil" not in c.request.url for c in responses.calls)


DATADOME_BODY = '{"url":"https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAA&cid=Se2LM6&hash=X"}'


@responses.activate
def test_datadome_block_is_recognized_without_client_id_refresh(tmp_path):
    """Реальный ответ api-v2 на POST /playlists из Python: 403 от DataDome, а не от токена."""
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.POST, API + "/playlists", body=DATADOME_BODY, status=403,
                  headers={"x-datadome": "protected", "x-dd-b": "2"})
    client, _ = make_client(tmp_path, oauth_token=TOKEN)
    with pytest.raises(BlockedError) as e:
        client.create_playlist("Из VK", [1, 2])
    assert "браузер" in str(e.value) and "Токен при этом в порядке" in str(e.value)
    assert len(responses.calls) == 1  # client_id не обновлялся, повторов нет


@responses.activate
def test_plain_403_is_still_an_auth_error(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    mock_home(CID2)
    responses.add(responses.GET, API + "/me", status=403, body="forbidden")
    client, _ = make_client(tmp_path, oauth_token=TOKEN)
    with pytest.raises(AuthError):
        client.me()


@responses.activate
def test_description_is_dropped_if_rejected(tmp_path):
    (tmp_path / "cache.json").write_text(json.dumps({"client_id": CID}))
    responses.add(responses.POST, API + "/playlists", json={"error": "bad field"}, status=422)
    responses.add(responses.POST, API + "/playlists", json={"id": 5, "track_count": 1}, status=201)
    client, _ = make_client(tmp_path, oauth_token=TOKEN)
    assert client.create_playlist("x", [1])["id"] == 5
    assert "description" not in json.loads(responses.calls[1].request.body)["playlist"]
