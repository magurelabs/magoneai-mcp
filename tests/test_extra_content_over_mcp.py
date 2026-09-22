"""A successful result carrying an extra content block must survive the SDK.

`to_call_result` turns `{"...", "_mcp_content": {...}}` into a CallToolResult
with a second content block. The SDK validates a successful CallToolResult's
`structuredContent` against the output model it derives from a `-> Dict[...]`
annotation, and none was set, so it validated None and raised. Every
successful `outlook_email.download_attachment` failed that way. The unit tests
of `to_call_result` passed throughout, because they never went through the
SDK: this goes through a real MCP client.
"""

from typing import Any

from mcp import Client
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

ATTACHMENT = {
    "type": "resource",
    "resource": {
        "uri": "attachment://report.pdf",
        "mimeType": "application/pdf",
        "blob": "JVBERi0xLjQK",
    },
}


def build() -> ConnectorServer:
    server = ConnectorServer(name="extra-content-test", version="0.0.0", env={})

    @server.tool(name="download", description="Return an attachment.", annotations=READ)
    async def download() -> dict[str, Any]:
        """Shaped like outlook_email.download_attachment."""
        return {"success": True, "name": "report.pdf", "_mcp_content": ATTACHMENT}

    @server.tool(name="refuse_with_block", description="Refuse.", annotations=READ)
    async def refuse_with_block() -> dict[str, Any]:
        """A refusal that also carries a block."""
        return {"success": False, "error": "too large", "_mcp_content": ATTACHMENT}

    return server


async def call(name: str):
    async with Client(build().mcp) as client:
        return await client.call_tool(name, {})


class TestASuccessWithAnAttachment:
    async def test_it_is_a_success(self):
        result = await call("download")

        assert result.is_error is False

    async def test_the_attachment_arrives_as_its_own_block(self):
        result = await call("download")

        kinds = [block.type for block in result.content]
        assert kinds == ["text", "resource"]
        assert result.content[1].resource.uri == "attachment://report.pdf"

    async def test_the_text_block_is_the_payload_without_the_block(self):
        import json

        result = await call("download")

        assert json.loads(result.content[0].text) == {"success": True, "name": "report.pdf"}


class TestARefusalWithAnAttachment:
    async def test_it_is_still_an_error_and_keeps_its_payload(self):
        result = await call("refuse_with_block")

        assert result.is_error is True
        assert "too large" in result.content[0].text
