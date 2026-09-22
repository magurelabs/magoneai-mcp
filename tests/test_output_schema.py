"""A tool's output contract is the one it declares, or none, as in v1.

Left to itself the SDK derives an output schema from the return annotation.
For `-> Dict[str, Any]`, most of the catalogue, that is `{"result": {...}}`:
a wrapper v1 never published, which describes nothing, and which the SDK then
enforces by validating every result against it. v1 published a real output
schema on 101 tools, which the platform reads to offer a TOOL node's output
fields downstream. These tests pin both halves through a real MCP client.
"""

from typing import Any

from mcp import Client
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

OUTPUT = {
    "type": "object",
    "properties": {"meeting_id": {"type": "string", "description": "The meeting."}},
    "required": ["meeting_id"],
}


def build() -> ConnectorServer:
    server = ConnectorServer(name="output-schema-test", version="0.0.0", env={})

    @server.tool(name="declared", description="Declares its output.", annotations=READ)
    @declare_schema({"type": "object", "properties": {}})
    @declare_output_schema(OUTPUT)
    async def declared() -> dict[str, Any]:
        """Return a payload matching the declared schema."""
        return {"meeting_id": "m-1"}

    @server.tool(name="undeclared", description="Declares nothing.", annotations=READ)
    async def undeclared() -> dict[str, Any]:
        """Return a dict with no declared output schema."""
        return {"anything": 1}

    @server.tool(name="declared_refusal", description="Refuses.", annotations=READ)
    @declare_output_schema(OUTPUT)
    async def declared_refusal() -> dict[str, Any]:
        """A refusal is not held to the output schema."""
        return {"success": False, "error": "no such meeting"}

    return server


async def listed() -> dict:
    async with Client(build().mcp) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def call(name: str):
    async with Client(build().mcp) as client:
        return await client.call_tool(name, {})


class TestWhatIsAdvertised:
    async def test_a_declared_output_schema_is_published_verbatim(self):
        tools = await listed()

        assert tools["declared"].output_schema == OUTPUT

    async def test_an_undeclared_tool_publishes_no_output_schema(self):
        """Not the SDK's `{"result": ...}` wrapper, which v1 never had."""
        tools = await listed()

        assert tools["undeclared"].output_schema is None


class TestWhatIsReturned:
    async def test_a_declared_tool_returns_its_payload_as_structured_content(self):
        result = await call("declared")

        assert result.is_error is False
        assert result.structured_content == {"meeting_id": "m-1"}

    async def test_an_undeclared_tool_is_not_wrapped(self):
        result = await call("undeclared")

        assert result.is_error is False
        assert result.structured_content in (None, {"anything": 1})
        assert '"anything"' in result.content[0].text

    async def test_a_refusal_from_a_declared_tool_is_still_an_error(self):
        result = await call("declared_refusal")

        assert result.is_error is True
        assert "no such meeting" in result.content[0].text
