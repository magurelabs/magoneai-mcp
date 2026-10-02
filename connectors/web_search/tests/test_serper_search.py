import importlib

search_tools = importlib.import_module("connectors.web_search.server")
from connectors.web_search.server import (
    _serper_error,
    _serper_key,
    _serper_web_search,
    _web_search_provider,
)

"""Tests for the Serper-backed search_web path.

Covers the parts of the provider swap that fail silently rather than loudly:
provider selection, the SearXNG-compatible result shape, pagination (Serper
ignores ``num`` and only paginates), SafeSearch/region forwarding, and the
status-code mapping. HTTP is mocked, same approach as test_kb_tools.py, so no
network or key is needed.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _run(coro):
    return asyncio.run(coro)

def _organic(n: int, offset: int=0) -> dict:
    return {'organic': [{'link': f'https://example{i + offset}.com/a', 'title': f'Title {i + offset}', 'snippet': f'Snippet {i + offset}', 'date': '2026-07-01'} for i in range(n)]}

def _client(pages: list[dict], captured: list | None=None):
    """Fake httpx.AsyncClient returning one body per successive POST."""
    calls = {'n': 0}

    async def post(url, json=None, headers=None):
        if captured is not None:
            captured.append(json)
        body = pages[min(calls['n'], len(pages) - 1)]
        calls['n'] += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value=body)
        resp.raise_for_status = MagicMock()
        return resp
    client = MagicMock()
    client.post = AsyncMock(side_effect=post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client

def test_key_present_selects_serper(monkeypatch):
    monkeypatch.setenv('SERPER_API_KEY', 'k')
    monkeypatch.delenv('WEB_SEARCH_PROVIDER', raising=False)
    assert _web_search_provider() == 'serper'

def test_no_key_falls_back_to_searxng(monkeypatch):
    monkeypatch.setenv('SERPER_API_KEY', '')
    monkeypatch.delenv('WEB_SEARCH_PROVIDER', raising=False)
    monkeypatch.setattr(search_tools, 'SERPER_API_KEY', '')
    assert _web_search_provider() == 'searxng'

def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv('SERPER_API_KEY', 'k')
    monkeypatch.setenv('WEB_SEARCH_PROVIDER', 'searxng')
    assert _web_search_provider() == 'searxng'

def test_per_call_credential_beats_env(monkeypatch):
    """A platform-supplied org key must win over the pod-level env var."""
    monkeypatch.setenv('SERPER_API_KEY', 'env-key')
    assert _serper_key({'serper_api_key': 'org-key'}) == 'org-key'
    assert _serper_key({'serper_api_key': ''}) == 'env-key'
    assert _serper_key(None) == 'env-key'

def test_result_shape_matches_searxng_contract():
    """Callers must not be able to tell which provider answered."""
    with patch('httpx.AsyncClient', return_value=_client([_organic(3)])):
        out = _run(_serper_web_search('k', 'q', 10, None, 'en', ''))
    assert out['provider'] == 'serper'
    assert out['query'] == 'q'
    first = out['results'][0]
    for key in ('url', 'title', 'content', 'engine', 'source_domain', 'image_url'):
        assert key in first, key
    assert first['url'] == 'https://example0.com/a'
    assert first['content'] == 'Snippet 0'
    assert first['engine'] == 'serper'

def test_results_are_sliced_to_requested_count():
    with patch('httpx.AsyncClient', return_value=_client([_organic(10)])):
        out = _run(_serper_web_search('k', 'q', 3, None, 'en', ''))
    assert len(out['results']) == 3

def test_duplicate_urls_across_pages_are_deduped():
    page = _organic(10)
    with patch('httpx.AsyncClient', return_value=_client([page, page])):
        out = _run(_serper_web_search('k', 'q', 20, None, 'en', ''))
    urls = [r['url'] for r in out['results']]
    assert len(urls) == len(set(urls))

@pytest.mark.parametrize(('num_results', 'expected_pages'), [(1, 1), (10, 1), (11, 2), (20, 2), (30, 3), (100, 3)])
def test_page_count_is_derived_and_capped(num_results, expected_pages):
    """Serper returns ten per page regardless of `num`, so depth comes from
    pagination. Capped so one agent turn cannot run away with the credits."""
    captured: list = []
    client = _client([_organic(10)], captured)
    with patch('httpx.AsyncClient', return_value=client):
        _run(_serper_web_search('k', 'q', num_results, None, 'en', ''))
    assert client.post.await_count == expected_pages
    assert [c['page'] for c in captured] == list(range(1, expected_pages + 1))

def test_later_page_failure_degrades_instead_of_failing():
    """Page 1 decides success; a later page is depth, not correctness."""

    async def post(url, json=None, headers=None):
        resp = MagicMock()
        resp.status_code = 200
        if json['page'] == 1:
            resp.json = MagicMock(return_value=_organic(10))
            resp.raise_for_status = MagicMock()
        else:
            resp.raise_for_status = MagicMock(side_effect=httpx.ConnectError('boom'))
        return resp
    client = MagicMock()
    client.post = AsyncMock(side_effect=post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch('httpx.AsyncClient', return_value=client):
        out = _run(_serper_web_search('k', 'q', 20, None, 'en', ''))
    assert 'error' not in out
    assert len(out['results']) == 10

@pytest.mark.parametrize(('safe_search', 'expected'), [(0, 'off'), (1, 'active'), (2, 'active')])
def test_safe_search_is_forwarded(safe_search, expected):
    """The provider that replaced SearXNG must not silently drop SafeSearch."""
    captured: list = []
    with patch('httpx.AsyncClient', return_value=_client([_organic(1)], captured)):
        _run(_serper_web_search('k', 'q', 10, None, 'en', '', safe_search))
    assert captured[0]['safe'] == expected

def test_region_and_language_fall_back_to_configured_defaults(monkeypatch):
    """A UAE deployment sets SEARXNG_DEFAULT_REGION=ae; gl drives relevance."""
    monkeypatch.setattr(search_tools, 'DEFAULT_REGION', 'ae')
    monkeypatch.setattr(search_tools, 'DEFAULT_LANGUAGE', 'ar')
    captured: list = []
    with patch('httpx.AsyncClient', return_value=_client([_organic(1)], captured)):
        _run(_serper_web_search('k', 'q', 10, None, '', ''))
    assert captured[0]['gl'] == 'ae'
    assert captured[0]['hl'] == 'ar'

def test_explicit_region_overrides_the_default(monkeypatch):
    monkeypatch.setattr(search_tools, 'DEFAULT_REGION', 'ae')
    captured: list = []
    with patch('httpx.AsyncClient', return_value=_client([_organic(1)], captured)):
        _run(_serper_web_search('k', 'q', 10, 'us', 'en', ''))
    assert captured[0]['gl'] == 'us'

@pytest.mark.parametrize(('time_range', 'expected'), [('day', 'qdr:d'), ('week', 'qdr:w'), ('month', 'qdr:m'), ('year', 'qdr:y')])
def test_time_range_maps_to_qdr(time_range, expected):
    captured: list = []
    with patch('httpx.AsyncClient', return_value=_client([_organic(1)], captured)):
        _run(_serper_web_search('k', 'q', 10, None, 'en', time_range))
    assert captured[0]['tbs'] == expected

def test_no_time_range_means_all_time():
    """An unfiltered search must not silently hide evergreen company pages."""
    captured: list = []
    with patch('httpx.AsyncClient', return_value=_client([_organic(1)], captured)):
        _run(_serper_web_search('k', 'q', 10, None, 'en', ''))
    assert 'tbs' not in captured[0]

def _http_error(status: int, body: dict | None=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=body or {})
    return httpx.HTTPStatusError('e', request=MagicMock(), response=resp)

@pytest.mark.parametrize(('exc', 'fragment'), [(_http_error(403), 'Invalid SERPER_API_KEY'), (_http_error(401), 'Invalid SERPER_API_KEY'), (_http_error(402), 'credit balance exhausted'), (_http_error(429), 'rate limit'), (_http_error(400, {'message': 'Missing query parameter'}), 'Missing query parameter'), (httpx.ConnectError('x'), 'Cannot reach Serper'), (httpx.ReadTimeout('x'), 'timed out')])
def test_error_mapping(exc, fragment):
    assert fragment in _serper_error(exc)

def test_config_errors_are_not_reported_as_no_results():
    """A broken key must surface as a tool failure, not an empty result set, or
    the agent will conclude the web has nothing on the topic."""

    async def post(url, json=None, headers=None):
        resp = MagicMock()
        resp.status_code = 403
        resp.raise_for_status = MagicMock(side_effect=_http_error(403))
        return resp
    client = MagicMock()
    client.post = AsyncMock(side_effect=post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch('httpx.AsyncClient', return_value=client):
        out = _run(_serper_web_search('bad', 'q', 10, None, 'en', ''))
    assert 'error' in out
    assert 'results' not in out

def test_empty_result_set_is_marked_not_authoritative():
    """An empty list alone reads to the agent as "no such information exists",
    which is how the web lane over-abstains on findable public queries."""
    with patch('httpx.AsyncClient', return_value=_client([{'organic': []}])):
        out = _run(_serper_web_search('k', 'q', 10, None, 'en', ''))
    assert out['results'] == []
    assert out['note'] == 'no_results'
    assert out['total_results'] == 0
    assert 'error' not in out

def test_non_empty_result_set_carries_no_note():
    with patch('httpx.AsyncClient', return_value=_client([_organic(3)])):
        out = _run(_serper_web_search('k', 'q', 10, None, 'en', ''))
    assert 'note' not in out
    assert out['total_results'] == 3
