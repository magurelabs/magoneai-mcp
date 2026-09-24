import importlib
_client = importlib.import_module("connectors.zoom.server")
from connectors.zoom.server import create_meeting, delete_meeting, get_meeting

"""Tests for the Zoom meeting tools and the Server-to-Server OAuth client.

Three behaviours are worth pinning down here:

* Zoom's own error text survives. Zoom answers failures with
  {"code": 3001, "message": "Meeting does not exist"}; a tool that reports only
  "Zoom API error: 404" leaves the agent nothing to act on.
* A meeting id arrives as an int or a string. Ids are numeric, but every hop
  that carries one (workflow variable, model argument) may stringify it, and
  protocol.py validates arguments against input_schema before the handler runs.
* The token cache is per connection. One token is minted per credential set and
  reused until shortly before expiry; rotating the secret must not reuse it.
"""
import asyncio
import time
from unittest.mock import MagicMock
import httpx
import jsonschema
import pytest
CREDS = {'zoom_account_id': 'acct-1', 'zoom_client_id': 'client-1', 'zoom_client_secret': 'secret-1'}

def _run(coro):
    return asyncio.run(coro)

def _response(status: int, body=None) -> httpx.Response:
    request = httpx.Request('GET', 'https://api.zoom.us/v2/test')
    if body is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, json=body, request=request)

class _FakeClient:
    """Stands in for httpx.AsyncClient, serving queued responses in order."""

    def __init__(self, queue: list, calls: list) -> None:
        self._queue = queue
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def _next(self, record):
        self._calls.append(record)
        return self._queue.pop(0)

    async def post(self, url, headers=None, data=None, **kwargs):
        return self._next({'method': 'POST', 'url': url, 'data': data, 'headers': headers})

    async def request(self, method, url, headers=None, json=None, **kwargs):
        return self._next({'method': method, 'url': url, 'json': json, 'headers': headers})

@pytest.fixture(autouse=True)
def _clear_token_cache():
    _client._token_cache.clear()
    _client._token_cache_lock = asyncio.Lock()
    yield
    _client._token_cache.clear()

@pytest.fixture
def http(monkeypatch):
    """Queue responses; returns the recorded calls list."""
    queue: list = []
    calls: list = []
    monkeypatch.setattr(_client.httpx, 'AsyncClient', lambda **kw: _FakeClient(queue, calls))
    return type('Http', (), {'queue': queue, 'calls': calls})()

def _token_ok(expires_in: int=3600) -> httpx.Response:
    return _response(200, {'access_token': 'tok-abc', 'expires_in': expires_in})

def test_zoom_error_message_reaches_the_caller(http):
    """A 404 must carry Zoom's explanation, not just the status code."""
    http.queue.extend([_token_ok(), _response(404, {'code': 3001, 'message': 'Meeting does not exist: 123.'})])
    result = _run(get_meeting(credentials=CREDS, meeting_id=123))
    assert result['success'] is False
    assert 'Meeting does not exist' in result['error']
    assert '3001' in result['error']

def test_auth_failure_is_reported_as_an_auth_failure(http):
    """A bad secret must not look like a meeting problem."""
    http.queue.append(_response(400, {'reason': 'Invalid client_id or client_secret', 'error': 'invalid_client'}))
    result = _run(create_meeting(credentials=CREDS, topic='T', start_time='2026-10-01T10:00:00Z'))
    assert result['success'] is False
    assert 'Zoom authentication failed' in result['error']
    assert 'Invalid client_id or client_secret' in result['error']

def test_missing_credentials_names_the_missing_fields():
    result = _run(create_meeting(credentials={}, topic='T', start_time='2026-10-01T10:00:00Z'))
    assert result['success'] is False
    for field in ('zoom_account_id', 'zoom_client_id', 'zoom_client_secret'):
        assert field in result['error']

def test_expected_failures_do_not_log_a_traceback(http, logger):
    """Missing credentials is an expected outcome — warning, not exception."""
    _run(get_meeting(credentials={}, meeting_id=1))
    assert logger.warning.called
    assert not logger.exception.called

def test_string_and_spaced_ids_resolve_to_the_same_path(http):
    http.queue.extend([_token_ok(), _response(200, {'id': 82345678901, 'topic': 'T'})])
    result = _run(get_meeting(credentials=CREDS, meeting_id='823 4567 8901'))
    assert result['success'] is True
    assert http.calls[-1]['url'].endswith('/meetings/82345678901')

def test_non_numeric_meeting_id_is_rejected_before_any_request(http):
    result = _run(get_meeting(credentials=CREDS, meeting_id='not-an-id'))
    assert result['success'] is False
    assert 'numeric Zoom meeting id' in result['error']
    assert http.calls == []

def test_create_meeting_sends_a_scheduled_utc_meeting(http):
    http.queue.extend([_token_ok(), _response(201, {'id': 82345678901, 'topic': 'Consultation', 'start_time': '2026-10-01T10:00:00Z', 'duration': 30, 'join_url': 'https://zoom.us/j/82345678901', 'start_url': 'https://zoom.us/s/82345678901', 'password': 'pw123'})])
    result = _run(create_meeting(credentials=CREDS, topic='Consultation', start_time='2026-10-01T10:00:00Z', duration=30, agenda='Intake call'))
    assert result['success'] is True
    assert result['meeting_id'] == 82345678901
    assert result['join_url'] == 'https://zoom.us/j/82345678901'
    sent = http.calls[-1]
    assert sent['method'] == 'POST'
    assert sent['url'].endswith('/users/me/meetings')
    assert sent['json']['type'] == 2
    assert sent['json']['timezone'] == 'UTC'
    assert sent['json']['agenda'] == 'Intake call'
    assert sent['headers']['Authorization'] == 'Bearer tok-abc'

def test_delete_meeting_handles_an_empty_204_body(http):
    http.queue.extend([_token_ok(), _response(204)])
    result = _run(delete_meeting(credentials=CREDS, meeting_id='82345678901'))
    assert result == {'success': True, 'meeting_id': '82345678901'}
    assert http.calls[-1]['method'] == 'DELETE'

def test_token_is_minted_once_and_reused(http):
    http.queue.extend([_token_ok(), _response(200, {'id': 1, 'topic': 'A'}), _response(200, {'id': 2, 'topic': 'B'})])
    _run(get_meeting(credentials=CREDS, meeting_id=1))
    _run(get_meeting(credentials=CREDS, meeting_id=2))
    assert sum((1 for c in http.calls if 'oauth/token' in c['url'])) == 1

def test_token_is_refreshed_before_expiry(http):
    """A token inside the refresh leeway is not handed to the API."""
    http.queue.append(_token_ok())
    _run(_client.get_access_token(CREDS))
    cache_key, (token, _) = next(iter(_client._token_cache.items()))
    _client._token_cache[cache_key] = (token, time.time() + _client._TOKEN_REFRESH_LEEWAY - 1)
    http.queue.append(_response(200, {'access_token': 'tok-new', 'expires_in': 3600}))
    assert _run(_client.get_access_token(CREDS)) == 'tok-new'

def test_separate_connections_do_not_share_a_token(http):
    other = {**CREDS, 'zoom_account_id': 'acct-2'}
    http.queue.extend([_token_ok(), _response(200, {'access_token': 'tok-2', 'expires_in': 3600})])
    first = _run(_client.get_access_token(CREDS))
    second = _run(_client.get_access_token(other))
    assert (first, second) == ('tok-abc', 'tok-2')
    assert len(_client._token_cache) == 2

def test_rotated_secret_does_not_reuse_the_old_token(http):
    rotated = {**CREDS, 'zoom_client_secret': 'secret-2'}
    http.queue.extend([_token_ok(), _response(200, {'access_token': 'tok-2', 'expires_in': 3600})])
    _run(_client.get_access_token(CREDS))
    after_rotation = _run(_client.get_access_token(rotated))
    assert after_rotation == 'tok-2'

def test_concurrent_cold_calls_mint_one_token(http):
    """The cache lock collapses a burst into a single token exchange."""
    http.queue.extend([_token_ok(), _response(200, {'id': 1, 'topic': 'A'}), _response(200, {'id': 2, 'topic': 'B'}), _response(200, {'id': 3, 'topic': 'C'})])

    async def burst():
        return await asyncio.gather(get_meeting(credentials=CREDS, meeting_id=1), get_meeting(credentials=CREDS, meeting_id=2), get_meeting(credentials=CREDS, meeting_id=3))
    results = _run(burst())
    assert all((r['success'] for r in results))
    assert sum((1 for c in http.calls if 'oauth/token' in c['url'])) == 1


# Injected when the tests were ported.
#
# v1 tools reached their logger through `self.logger`, supplied by the mixin's
# host class. The port made the tools module-level functions sharing one
# module-level `logger`, so assertions about what was logged now patch that.
@pytest.fixture(autouse=True)
def logger(monkeypatch):
    """The connector's module logger, as a mock, for the duration of a test."""
    # importlib, not `import connectors.zoom.server as _server`.
    # That form binds the ConnectorServer object the package re-exports, and
    # that object HAS a `.logger`, so the patch succeeds against the wrong
    # target and nothing raises - the handler keeps using the real logger and
    # the assertion fails with no clue why.
    _server = importlib.import_module("connectors.zoom.server")

    fake = MagicMock()
    monkeypatch.setattr(_server, "logger", fake)
    return fake
