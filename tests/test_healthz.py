"""`GET /healthz` answers a probe, over a real socket, by any Host.

A probe arrives by pod IP, so this must not sit behind the MCP endpoint's Host
allowlist, and it must not disturb that allowlist for `/mcp` either.
"""

import httpx
from mcp.types import ToolAnnotations
from test_custom_route import serving

from magone_mcp_core import ConnectorServer

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def build(env: dict[str, str]) -> ConnectorServer:
    server = ConnectorServer(name="health-test", version="1.2.3", env=env)

    @server.tool(name="read_it", description="Reads.", annotations=READ)
    async def read_it() -> dict:
        """A read."""
        return {}

    @server.tool(name="write_it", description="Writes.", annotations=WRITE)
    async def write_it() -> dict:
        """A write."""
        return {}

    return server


async def test_healthz_answers_by_pod_ip():
    with serving(build({})) as url:
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            response = await client.get("/healthz", headers={"Host": "10.4.2.17:8000"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "name": "health-test",
        "version": "1.2.3",
        "tools": 2,
    }


async def test_healthz_counts_what_tools_list_would_show():
    """In a read-only deployment the write is not listed, so not counted."""
    with serving(build({"MCP_READ_ONLY": "1"})) as url:
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            response = await client.get("/healthz")

    assert response.json()["tools"] == 1


async def test_healthz_does_not_open_the_mcp_endpoint():
    with serving(build({})) as url:
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Host": "10.4.2.17:8000",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Mcp-Method": "tools/list",
                    "MCP-Protocol-Version": "2026-07-28",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )

    assert response.status_code == 421
