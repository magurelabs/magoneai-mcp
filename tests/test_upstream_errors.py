"""A failed call says why in a code a host can act on.

A host calling a tool on its own (paging through a history, retrying a
schedule) has to tell "wait and try again" from "stop" without reading the
message. So a vendor error raised as `UpstreamError`, and a request that never
got an answer, arrive as a refusal carrying `error_code` and, when the vendor
said, `retry_after_seconds`. The message the model reads is unchanged.

Every server test goes through a real MCP client, as `test_error_surfacing.py`
does, because the shaping happens in the server's call path.
"""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from mcp import Client
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, UpstreamError, code_for_status, retry_after_seconds
from magone_mcp_core.errors import MAX_RETRY_AFTER_SECONDS, RETRYABLE

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def build() -> ConnectorServer:
    server = ConnectorServer(name="upstream-errors-test", version="0.0.0", env={})

    @server.tool(name="throttled", description="Raise a 429.", annotations=READ)
    async def throttled() -> dict:
        """Raise the way a helper does for a 429 with Retry-After."""
        raise UpstreamError.from_status(
            "Rate limit exceeded. Try again later.", 429, {"Retry-After": "12"}
        )

    @server.tool(name="forbidden", description="Raise a 403.", annotations=READ)
    async def forbidden() -> dict:
        """Raise the way a helper does for a 403."""
        raise UpstreamError.from_status("Access denied. Check permissions.", 403)

    @server.tool(name="unreachable", description="Fail to connect.", annotations=READ)
    async def unreachable() -> dict:
        """Fail the way httpx does when the host is down."""
        raise httpx.ConnectError("connect failed for https://internal.example/secret-path")

    @server.tool(name="slow", description="Time out.", annotations=READ)
    async def slow() -> dict:
        """Fail the way httpx does on a read timeout."""
        raise httpx.ReadTimeout("timed out")

    @server.tool(name="internal", description="Raise a RuntimeError.", annotations=READ)
    async def internal() -> dict:
        """Raise something that must stay masked."""
        raise RuntimeError("connection string postgres://admin:secret@db/prod")

    return server


async def call(name: str):
    async with Client(build().mcp) as client:
        return await client.call_tool(name, {})


def payload(result) -> dict:
    return json.loads("".join(getattr(block, "text", "") for block in result.content))


class TestARefusalCarriesItsCode:
    async def test_a_throttle_says_how_long_to_wait(self):
        result = await call("throttled")

        assert result.is_error is True
        assert payload(result) == {
            "success": False,
            "error": "Rate limit exceeded. Try again later.",
            "error_code": "throttled",
            "retry_after_seconds": 12.0,
        }

    async def test_a_permanent_refusal_has_no_wait(self):
        result = await call("forbidden")

        body = payload(result)
        assert (body["error_code"], body["error"]) == (
            "forbidden",
            "Access denied. Check permissions.",
        )
        assert "retry_after_seconds" not in body

    async def test_an_unreachable_service_is_a_network_error(self):
        result = await call("unreachable")

        body = payload(result)
        assert body["error_code"] == "network_error"
        assert "secret-path" not in json.dumps(body), "the URL is not echoed"

    async def test_a_timeout_is_a_timeout(self):
        assert payload(await call("slow"))["error_code"] == "timeout"

    async def test_any_other_exception_stays_masked(self):
        result = await call("internal")

        assert result.is_error is True
        assert "postgres://" not in "".join(getattr(b, "text", "") for b in result.content)


def test_an_upstream_error_is_still_a_value_error():
    """Every helper caller that catches ValueError keeps catching it."""
    with pytest.raises(ValueError, match="Resource not found."):
        raise UpstreamError.from_status("Resource not found.", 404)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "throttled"),
        (503, "unavailable"),
        (504, "unavailable"),
        (401, "auth_expired"),
        (403, "forbidden"),
        (404, "not_found"),
        (400, "invalid_argument"),
        (500, "upstream_error"),
    ],
)
def test_status_codes(status, code):
    assert code_for_status(status) == code


def test_only_transient_codes_are_retryable():
    assert RETRYABLE == {"throttled", "unavailable", "network_error", "timeout"}


def test_retry_after_reads_seconds_and_dates():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    later = format_datetime(now + timedelta(seconds=90), usegmt=True)

    assert retry_after_seconds({"retry-after": "7"}) == 7.0
    assert retry_after_seconds({"Retry-After": later}, now=now) == 90.0
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds({"Retry-After": "999999"}) == MAX_RETRY_AFTER_SECONDS


def test_a_wait_is_read_only_when_waiting_helps():
    assert UpstreamError.from_status("x", 403, {"Retry-After": "5"}).retry_after is None
    assert UpstreamError.from_status("x", 503, {"Retry-After": "5"}).retry_after == 5.0


def test_a_tool_that_catches_keeps_the_code():
    from magone_mcp_core import error_fields

    throttled = UpstreamError.from_status("slow down", 429, {"Retry-After": "3"})
    assert error_fields(throttled) == {"error_code": "throttled", "retry_after_seconds": 3.0}
    assert error_fields(httpx.ConnectError("down")) == {"error_code": "network_error"}
    assert error_fields(RuntimeError("anything")) == {}
