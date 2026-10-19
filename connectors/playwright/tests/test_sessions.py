"""SessionPool against the fake upstream: keys, re-init, eviction, SSE, failures."""

import json

import pytest

from connectors.playwright.upstream_rpc import UpstreamError, UpstreamRpc, sse_messages
from connectors.playwright.upstream_sessions import SessionPool, session_key

from .fakes import FakeUpstream


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pool(fake: FakeUpstream, idle: float = 60, cap: int = 64, clock=None) -> SessionPool:
    rpc = UpstreamRpc("http://playwright:8931/mcp", 5, fake.transport)
    return SessionPool(rpc, idle, cap, clock or Clock())


async def test_one_session_per_caller_and_run():
    fake = FakeUpstream()
    pool = _pool(fake)
    await pool.call_tool(session_key("c1", "e1"), "browser_navigate", {"url": "a"})
    await pool.call_tool(session_key("c1", "e1"), "browser_navigate", {"url": "b"})
    await pool.call_tool(session_key("c1", "e2"), "browser_navigate", {})
    await pool.call_tool(session_key("c1", None), "browser_navigate", {})
    assert [sid for sid, _ in fake.calls] == ["s1", "s1", "s2", "s3"]
    assert pool.active_keys()[-1] == ("c1", "default")


async def test_two_verified_callers_never_share_a_session():
    fake = FakeUpstream()
    pool = _pool(fake)
    await pool.call_tool(session_key("c1", "same"), "browser_navigate", {})
    await pool.call_tool(session_key("c2", "same"), "browser_navigate", {})
    assert fake.calls[0][0] != fake.calls[1][0]


def test_a_blank_run_id_is_the_default_session():
    assert session_key(None, "  ") == ("", "default")
    assert session_key("", None) == ("", "default")


async def test_reinitializes_once_when_the_session_is_gone():
    fake = FakeUpstream()
    pool = _pool(fake)
    key = session_key("c1", "e1")
    await pool.call_tool(key, "browser_navigate", {})
    fake.sessions.clear()  # upstream restarted
    result = await pool.call_tool(key, "browser_navigate", {})
    assert result["content"][0]["text"] == "ran browser_navigate"
    assert [sid for sid, _ in fake.calls] == ["s1", "s2"]


async def test_initialize_sends_initialized_notification_and_session_header():
    fake = FakeUpstream()
    await _pool(fake).call_tool(session_key("c1", "e1"), "browser_navigate", {})
    methods = [json.loads(r.content).get("method") for r in fake.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    call = fake.requests[-1]
    assert call.headers["mcp-session-id"] == "s1"
    assert call.headers["accept"] == "application/json, text/event-stream"
    assert call.headers["mcp-protocol-version"] == "2025-06-18"


async def test_idle_eviction_deletes_the_upstream_session():
    fake, clock = FakeUpstream(), Clock()
    pool = _pool(fake, idle=60, clock=clock)
    await pool.call_tool(session_key("c1", "e1"), "browser_navigate", {})
    clock.now += 30
    assert await pool.evict_idle() == 0
    clock.now += 61
    assert await pool.evict_idle() == 1
    assert fake.deleted == ["s1"] and pool.active_keys() == []
    assert fake.requests[-1].method == "DELETE"


async def test_browser_close_evicts_the_session():
    fake = FakeUpstream()
    pool = _pool(fake)
    await pool.call_tool(session_key("c1", "e1"), "browser_close", {})
    assert fake.deleted == ["s1"] and pool.active_keys() == []


async def test_capacity_evicts_least_recently_used():
    fake = FakeUpstream()
    pool = _pool(fake, cap=2)
    for execution in ("a", "b", "a", "c"):
        await pool.call_tool(session_key("c", execution), "browser_navigate", {})
    assert fake.deleted == ["s2"]
    assert pool.active_keys() == [("c", "a"), ("c", "c")]


async def test_close_all_ends_every_session():
    fake = FakeUpstream()
    pool = _pool(fake)
    for execution in ("a", "b"):
        await pool.call_tool(session_key("c", execution), "browser_navigate", {})
    await pool.close_all()
    assert sorted(fake.deleted) == ["s1", "s2"] and pool.active_keys() == []


async def test_sse_replies_are_parsed():
    fake = FakeUpstream(sse=True)
    pool = _pool(fake)
    result = await pool.call_tool(session_key("c1", "e1"), "browser_take_screenshot", {})
    assert result["content"][1]["type"] == "image"
    tools = await pool.list_tools()
    assert "browser_navigate" in {t["name"] for t in tools}
    assert sse_messages('data: {"id": 1}\n\nevent: x\ndata: {"id":\ndata: 2}\n') == [
        {"id": 1},
        {"id": 2},
    ]


async def test_list_tools_uses_a_short_lived_session():
    fake = FakeUpstream()
    await _pool(fake).list_tools()
    assert fake.deleted == ["s1"]


async def test_upstream_down_is_an_upstream_error():
    pool = _pool(FakeUpstream(down=True))
    with pytest.raises(UpstreamError, match="unreachable"):
        await pool.call_tool(session_key("c1", "e1"), "browser_navigate", {})


def test_a_verified_caller_is_part_of_the_key():
    """With a token verifier, the access token's principal separates callers."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from connectors.playwright.gateway import request_session_key

    assert request_session_key(None) == ("", "default")
    token = AccessToken(token="t", client_id="client-a", scopes=[], subject="user-1")  # noqa: S106
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        identity, run = request_session_key(None)
    finally:
        auth_context_var.reset(reset)
    assert "client-a" in identity and "user-1" in identity and run == "default"
