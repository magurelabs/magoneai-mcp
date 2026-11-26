"""Graph throttling is waited out the way Graph asks, within a fixed budget.

https://learn.microsoft.com/en-us/graph/throttling: on 429, wait Retry-After,
then retry; never retry immediately. Through a real MCP client, with Graph
mocked at the transport and the sleep recorded instead of slept.
"""

import httpx
import pytest
from mcp import Client

from connectors.microsoft_teams.tests._graph_mock import install, message, module, payload

pytestmark = pytest.mark.requires_connectors

CHAT = "19:abc123@thread.v2"


async def list_chat():
    async with Client(module.server.mcp) as client:
        return payload(await client.call_tool("list_chat_messages", {"chat_id": CHAT}))


def answers(*responses: httpx.Response):
    queue = list(responses)
    return lambda request: queue.pop(0)


def ok() -> httpx.Response:
    return httpx.Response(200, json={"value": [message("1", "2025-11-01T00:00:00Z")]})


async def test_429_waits_retry_after_then_succeeds(monkeypatch):
    seen = install(monkeypatch, answers(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(429, headers={"Retry-After": "3"}),
        ok(),
    ))

    out = await list_chat()

    assert out["success"] is True
    assert seen.sleeps == [2.0, 3.0]
    assert len(seen.requests) == 3


async def test_429_without_retry_after_backs_off(monkeypatch):
    seen = install(monkeypatch, answers(httpx.Response(429), ok()))

    out = await list_chat()

    assert out["success"] is True
    assert len(seen.sleeps) == 1 and 0.5 <= seen.sleeps[0] <= 1.0


async def test_429_past_the_retries_reports_throttled_with_retry_after(monkeypatch):
    seen = install(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "5"}))

    out = await list_chat()

    assert out["error_code"] == "throttled"
    assert out["retry_after_seconds"] == 5.0
    assert len(seen.requests) == 4  # the call and three retries


async def test_retry_after_longer_than_the_budget_is_handed_back_not_shortened(monkeypatch):
    seen = install(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "120"}))

    out = await list_chat()

    assert out["error_code"] == "throttled"
    assert out["retry_after_seconds"] == 120.0
    assert seen.sleeps == []
    assert len(seen.requests) == 1


@pytest.mark.parametrize("status", [503, 504])
async def test_unavailable_is_retried_then_reported(monkeypatch, status):
    seen = install(monkeypatch, lambda request: httpx.Response(status))

    out = await list_chat()

    assert out["error_code"] == "unavailable"
    assert len(seen.requests) == 4


@pytest.mark.parametrize(("status", "code"), [
    (401, "auth_expired"), (403, "forbidden"), (404, "not_found"), (400, "upstream_error"),
])
async def test_other_failures_are_not_retried(monkeypatch, status, code):
    seen = install(monkeypatch, lambda request: httpx.Response(status, json={"error": {"message": "no"}}))

    out = await list_chat()

    assert out["error_code"] == code
    assert len(seen.requests) == 1
    assert seen.sleeps == []


async def test_missing_token_is_auth_expired(monkeypatch):
    seen = install(monkeypatch, lambda request: ok(), headers={})

    out = await list_chat()

    assert out["error_code"] == "auth_expired"
    assert seen.requests == []


async def test_existing_tools_keep_their_message_after_retrying(monkeypatch):
    seen = install(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "1"}))

    async with Client(module.server.mcp) as client:
        out = payload(await client.call_tool("read_chat_messages", {"chat_id": CHAT}))

    assert out == {
        "success": False,
        "error": "Failed to read chat messages: Rate limit exceeded. Try again later.",
    }
    assert len(seen.requests) == 4


async def test_existing_tools_recover_from_one_429(monkeypatch):
    install(monkeypatch, answers(httpx.Response(429, headers={"Retry-After": "1"}), ok()))

    async with Client(module.server.mcp) as client:
        out = payload(await client.call_tool("read_chat_messages", {"chat_id": CHAT}))

    assert out["success"] is True
    assert out["count"] == 1


async def send_chat():
    async with Client(module.server.mcp) as client:
        return payload(
            await client.call_tool("send_chat_message", {"chat_id": CHAT, "content": "hello"})
        )


def posts(seen) -> list:
    return [r for r in seen.requests if r.method == "POST"]


@pytest.mark.parametrize("status", [503, 504])
async def test_a_write_answered_unavailable_is_not_resent(monkeypatch, status):
    """A 504 does not say Graph did nothing, so resending could post the message twice."""
    seen = install(monkeypatch, answers(
        httpx.Response(status, json={}), httpx.Response(201, json={"id": "M1"})
    ))

    out = await send_chat()

    assert out["success"] is False
    assert out["error"].startswith(f"Failed to send chat message: Graph API error ({status})")
    assert len(posts(seen)) == 1
    assert seen.sleeps == []


async def test_a_throttled_write_waits_retry_after_then_is_sent(monkeypatch):
    """A 429 was turned away by the rate limiter, so sending it again is what Graph asks."""
    seen = install(monkeypatch, answers(
        httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(201, json={"id": "M1"})
    ))

    out = await send_chat()

    assert out["success"] is True
    assert len(posts(seen)) == 2
    assert seen.sleeps == [2.0]
