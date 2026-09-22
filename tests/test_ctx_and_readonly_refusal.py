"""Two framework behaviours a tool author relies on without reading the framework.

1. A handler declaring `ctx: Context`, as the SDK documentation shows, gets the
   request context. `guarded` names its own parameter `ctx` to receive it from
   the SDK and was not passing it on, so such a tool failed with a missing
   argument, or received None when it had a default.

2. A read-only refusal tells the caller why. It was a plain exception, so the
   SDK answered "Error executing tool X" and logged an ERROR traceback for a
   control that had worked.
"""

import logging

from mcp import Client
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def build(env: dict[str, str] | None = None) -> ConnectorServer:
    server = ConnectorServer(name="ctx-test", version="0.0.0", env=env or {})

    @server.tool(name="needs_ctx", description="Uses the context.", annotations=READ)
    async def needs_ctx(ctx: Context) -> dict:
        """Required context, SDK style."""
        return {"has_ctx": isinstance(ctx, Context)}

    @server.tool(name="ctx_and_creds", description="Both.", annotations=READ)
    async def ctx_and_creds(credentials: dict, ctx: Context | None = None) -> dict:
        """Credentials supplied by the framework, context by the SDK."""
        return {"has_ctx": isinstance(ctx, Context), "creds": isinstance(credentials, dict)}

    @server.tool(name="write_thing", description="Writes.", annotations=WRITE)
    async def write_thing() -> dict:
        """A write."""
        return {"written": True}

    return server


async def call(server: ConnectorServer, name: str):
    async with Client(server.mcp) as client:
        return await client.call_tool(name, {})


class TestTheContextReachesTheHandler:
    async def test_a_required_ctx_is_supplied(self):
        result = await call(build(), "needs_ctx")

        assert result.is_error is False
        assert '"has_ctx": true' in result.content[0].text

    async def test_ctx_alongside_framework_credentials(self):
        result = await call(build(), "ctx_and_creds")

        assert result.is_error is False
        assert '"has_ctx": true' in result.content[0].text
        assert '"creds": true' in result.content[0].text


class TestAReadOnlyRefusalExplainsItself:
    async def test_the_reason_reaches_the_caller(self):
        result = await call(build({"MCP_READ_ONLY": "1"}), "write_thing")

        assert result.is_error is True
        assert "MCP_READ_ONLY" in result.content[0].text

    async def test_no_error_traceback_is_logged(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await call(build({"MCP_READ_ONLY": "1"}), "write_thing")

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, [r.getMessage() for r in errors]

    async def test_reads_still_work_in_read_only_mode(self):
        result = await call(build({"MCP_READ_ONLY": "1"}), "needs_ctx")

        assert result.is_error is False
