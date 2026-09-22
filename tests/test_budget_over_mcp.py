"""The output budget bounds what a client receives, and cuts nothing it must keep.

Measured on the old ordering, where the budget ran on the payload before it
was shaped for the wire:

- a 92,701-character result passed a 100,000 budget and went out at 196,309,
  because the budget counted compact JSON and the wire carried `indent=2`;
- a large refusal came back with `isError=False`, because its envelope had no
  `error` key;
- a large result with an attachment had the attachment folded into the text.

Every assertion here is on what a real MCP client receives.
"""

import json
from typing import Any

from mcp import Client
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from magone_mcp_core import Budget, ConnectorServer, declare_output_schema

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
LIMIT = 5_000

ATTACHMENT = {
    "type": "resource",
    "resource": {"uri": "attachment://a.bin", "mimeType": "application/pdf", "blob": "QUJD"},
}


def rows(n: int) -> list[dict[str, Any]]:
    return [{"id": i, "name": f"row {i}", "tags": ["a", "b"]} for i in range(n)]


def build() -> ConnectorServer:
    server = ConnectorServer(
        name="budget-test", version="0.0.0", env={}, budget=Budget(max_result_chars=LIMIT)
    )

    @server.tool(name="indented", description="Compact fits, indented does not.", annotations=READ)
    async def indented() -> dict[str, Any]:
        """About 3,000 characters compact and well over 5,000 at indent=2."""
        return {"rows": rows(60)}

    @server.tool(name="big_refusal", description="A large refusal.", annotations=READ)
    async def big_refusal() -> dict[str, Any]:
        """A refusal whose payload is over budget."""
        return {"success": False, "error": "narrow the query", "detail": "x" * 20_000}

    @server.tool(name="big_error_result", description="A handler-built error.", annotations=READ)
    async def big_error_result() -> CallToolResult:
        """A CallToolResult with isError set, over budget."""
        return CallToolResult(content=[TextContent(type="text", text="e" * 20_000)], isError=True)

    @server.tool(
        name="big_with_attachment", description="Large text plus a block.", annotations=READ
    )
    async def big_with_attachment() -> dict[str, Any]:
        """Over budget, carrying an attachment that must survive whole."""
        return {"text": "y" * 20_000, "_mcp_content": ATTACHMENT}

    @server.tool(
        name="big_structured", description="Declared output, over budget.", annotations=READ
    )
    @declare_output_schema({"type": "object"})
    async def big_structured() -> dict[str, Any]:
        """A declared-output tool whose payload is over budget."""
        return {"blob": "z" * 20_000}

    @server.tool(name="small", description="Within budget.", annotations=READ)
    async def small() -> dict[str, Any]:
        """Well within budget."""
        return {"ok": True}

    return server


async def call(name: str):
    async with Client(build().mcp) as client:
        return await client.call_tool(name, {})


def sent_text(result) -> str:
    return "".join(b.text for b in result.content if getattr(b, "type", "") == "text")


class TestTheBudgetMeasuresWhatIsSent:
    async def test_indented_output_over_the_limit_is_cut_to_the_limit(self):
        compact = len(json.dumps({"rows": rows(60)}))
        assert compact < LIMIT, "the fixture must fit when measured the old way"

        result = await call("indented")

        assert len(sent_text(result)) <= LIMIT
        assert json.loads(sent_text(result))["_truncated"] is True

    async def test_a_result_within_budget_is_untouched(self):
        result = await call("small")

        assert json.loads(sent_text(result)) == {"ok": True}


class TestWhatACutMustKeep:
    async def test_a_large_refusal_is_still_an_error(self):
        result = await call("big_refusal")

        assert result.is_error is True
        assert len(sent_text(result)) <= LIMIT
        assert "narrow the query" in sent_text(result)

    async def test_a_large_handler_built_error_is_still_an_error(self):
        result = await call("big_error_result")

        assert result.is_error is True
        assert len(sent_text(result)) <= LIMIT

    async def test_an_attachment_survives_whole_beside_the_cut_text(self):
        result = await call("big_with_attachment")

        kinds = [b.type for b in result.content]
        assert kinds == ["text", "resource"]
        assert result.content[1].resource.blob == "QUJD"
        assert len(sent_text(result)) <= LIMIT

    async def test_a_declared_output_that_does_not_fit_is_an_error_not_a_bypass(self):
        """Neither the full payload past the cut, nor a success a conforming
        client would reject for lacking its structured content."""
        result = await call("big_structured")

        assert result.is_error is True
        assert result.structured_content is None
        assert len(sent_text(result)) <= LIMIT
        assert "Narrow the request" in sent_text(result)


class TestTheFloor:
    def test_a_budget_below_the_floor_is_raised_to_it_not_reset(self):
        assert Budget.from_env({"MCP_MAX_RESULT_CHARS": "500"}).max_result_chars == 1_000
