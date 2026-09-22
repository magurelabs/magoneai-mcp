"""A declared schema must reach the wire unchanged.

This is the property the whole port depends on. Regenerating schemas from
type hints is the single largest risk in a port like this one, because a
schema that shifts silently changes behaviour for a live agent. Declaring the
schema removes the risk rather than mitigating it, but only if the declaration
is genuinely what gets emitted.

So these tests assert on the emitted schema, including over a real MCP session,
rather than on the object we put in.
"""

import json

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema, declared_schema, register_tool

GOLDEN = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "num_results": {
            "type": "integer",
            "description": "How many results to return",
            "default": 5,
        },
        "site": {"type": "string", "description": "Restrict to one domain", "default": ""},
    },
    "required": ["query"],
}


@declare_schema(GOLDEN)
async def search_web(query: str, num_results: int = 5, site: str = "") -> dict:
    """Search the web."""
    return {"query": query, "num_results": num_results, "site": site}


async def derived(query: str, num_results: int = 5) -> dict:
    """No declared schema; the SDK derives one from type hints."""
    return {"query": query}


def test_declare_schema_records_the_schema() -> None:
    assert declared_schema(search_web) == GOLDEN
    assert declared_schema(derived) is None


def test_registered_tool_keeps_the_declared_schema() -> None:
    mcp = MCPServer(name="t", version="0")
    tool = register_tool(mcp, search_web, name="search_web", description="Search the web.")
    assert tool.parameters == GOLDEN


def test_derivation_would_have_lost_the_descriptions() -> None:
    """The concrete damage declaration avoids.

    Not a hypothetical: derivation drops every description and invents titles.
    If this test ever fails because derivation improved, the declaration path
    is still correct, but the argument for it has changed and should be re-read.
    """
    mcp = MCPServer(name="t", version="0")
    tool = register_tool(mcp, derived, name="derived", description="d")
    props = tool.parameters["properties"]
    assert "description" not in props["query"], "derivation unexpectedly kept descriptions"
    assert "title" in props["query"], "derivation unexpectedly stopped inventing titles"


@pytest.mark.asyncio
async def test_declared_schema_is_byte_identical_over_a_real_session() -> None:
    """The authoritative check: what a standard client actually receives."""
    server = ConnectorServer(name="proof", version="0.1.0", env={"MCP_MODE": "local"})

    @server.tool(
        name="search_web",
        description="Search the web.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @declare_schema(GOLDEN)
    async def _search(query: str, num_results: int = 5, site: str = "") -> dict:
        return {"query": query, "num_results": num_results, "site": site}

    async with Client(server.mcp) as client:
        listed = await client.list_tools()
        wire = listed.tools[0].model_dump(by_alias=True, exclude_none=True)

        # camelCase, which is what v1 got wrong and what breaks every client.
        assert "inputSchema" in wire
        assert "input_schema" not in wire

        assert wire["inputSchema"] == GOLDEN, (
            "emitted schema differs from the declared one:\n"
            f"  emitted: {json.dumps(wire['inputSchema'], sort_keys=True)}\n"
            f"  golden : {json.dumps(GOLDEN, sort_keys=True)}"
        )


@pytest.mark.asyncio
async def test_arguments_actually_arrive() -> None:
    """The empty-arguments failure mode, asserted directly.

    The most severe failure a platform can have here is a tool called with
    no arguments, silently, that presents as working. A test that only checks the
    schema would not catch it.
    """
    server = ConnectorServer(name="proof", version="0.1.0", env={"MCP_MODE": "local"})

    @server.tool(
        name="echo",
        description="Echo the arguments.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @declare_schema(
        {
            "type": "object",
            "properties": {"value": {"type": "string", "description": "Anything"}},
            "required": ["value"],
        }
    )
    async def _echo(value: str) -> dict:
        return {"received": value}

    async with Client(server.mcp) as client:
        result = await client.call_tool("echo", {"value": "hello"})
        assert result.is_error is False
        assert "hello" in result.content[0].text


def test_sdk_private_tool_registry_still_exists() -> None:
    """Pin the one private SDK API this framework depends on.

    `register_tool` writes into `MCPServer._tool_manager._tools` because the
    SDK exposes no public way to supply a tool's `parameters`. That is a real
    coupling, and it should fail here with an explanation rather than somewhere
    confusing after an SDK upgrade.
    """
    mcp = MCPServer(name="t", version="0")
    assert hasattr(mcp, "_tool_manager"), "SDK no longer exposes _tool_manager"
    assert hasattr(mcp._tool_manager, "_tools"), "SDK no longer exposes _tool_manager._tools"
