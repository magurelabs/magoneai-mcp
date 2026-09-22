"""A handler's error message must reach the caller.

Connector helpers raise `ValueError` with text written for the model: a Jira
403 names the scope the connection lacks, DocuSign says which field is wrong,
a validation check says what to send instead. The SDK reports any exception
that is not a `ToolError` as a bare "Error executing tool X", so on the port
all of that text was replaced by the same six words. v1 passed ValueError,
TypeError and KeyError through (its `_SAFE_EXCEPTIONS`).

Every test goes through a real MCP client, because the masking happens in the
SDK's call path and a direct call to the handler would not see it.
"""

import pytest
from mcp import Client
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def build() -> ConnectorServer:
    server = ConnectorServer(name="errors-test", version="0.0.0", env={})

    @server.tool(name="vendor_refused", description="Raise a vendor refusal.", annotations=READ)
    async def vendor_refused(credentials: dict) -> dict:
        """Raise the way connector helpers do."""
        raise ValueError(
            "Jira API error 403 on GET /rest/api/3/issue/X-1: missing scope write:jira-work"
        )

    @server.tool(name="bad_type", description="Raise a TypeError.", annotations=READ)
    async def bad_type() -> dict:
        """Raise a TypeError."""
        raise TypeError("start must be an ISO-8601 datetime")

    @server.tool(name="missing_key", description="Raise a KeyError.", annotations=READ)
    async def missing_key() -> dict:
        """Raise a KeyError."""
        raise KeyError("calendar_id")

    @server.tool(name="leaky", description="Raise a message carrying a token.", annotations=READ)
    async def leaky() -> dict:
        """Raise a message a vendor echoed a credential into."""
        raise ValueError("401 from upstream: Authorization: Bearer abcdefghijklmnop0123456789")

    @server.tool(name="huge", description="Raise a very long message.", annotations=READ)
    async def huge() -> dict:
        """Raise a long message."""
        raise ValueError("x" * 5000)

    @server.tool(name="internal", description="Raise an internal error.", annotations=READ)
    async def internal() -> dict:
        """Raise something that must stay masked."""
        raise RuntimeError("connection string postgres://admin:secret@db/prod")

    return server


async def call(name: str):
    async with Client(build().mcp) as client:
        return await client.call_tool(name, {})


def text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


class TestTheCallerSeesTheMessage:
    async def test_a_vendor_refusal_arrives_with_its_reason(self):
        result = await call("vendor_refused")

        assert result.is_error is True
        assert "missing scope write:jira-work" in text(result)

    async def test_a_type_error_arrives_with_its_reason(self):
        result = await call("bad_type")

        assert result.is_error is True
        assert "ISO-8601" in text(result)

    async def test_a_key_error_names_the_key(self):
        result = await call("missing_key")

        assert result.is_error is True
        assert "calendar_id" in text(result)


class TestWhatStillMustNotReachTheCaller:
    async def test_a_credential_in_the_message_is_scrubbed(self):
        result = await call("leaky")

        assert result.is_error is True
        assert "abcdefghijklmnop0123456789" not in text(result)

    async def test_a_long_message_is_capped(self):
        result = await call("huge")

        assert len(text(result)) < 700

    async def test_an_unexpected_exception_stays_masked(self):
        """Only the three input-shaped types pass through. A RuntimeError may
        carry anything, here a connection string, and must not."""
        result = await call("internal")

        assert result.is_error is True
        assert "secret" not in text(result)
        assert "postgres://" not in text(result)


@pytest.mark.parametrize("name", ["vendor_refused", "bad_type", "missing_key"])
async def test_the_masking_it_replaces_is_gone(name):
    """The exact text the SDK produced before: nothing but the tool name."""
    result = await call(name)

    assert text(result).strip() != f"Error executing tool {name}"
