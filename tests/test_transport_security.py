"""A served connector must answer the names it is called by, and only those.

Found by running a connector the way a cluster calls it. Every request carrying
`Host: <service-name>:<port>` was answered `421 Misdirected Request`, because
`app()` left the SDK on its loopback-only default. Every test here goes over a
real socket with a real Host header, since that header is the whole defect: an
in-process client sends whatever base URL the test chose, which is how the
suite stayed green while every deployed call would have failed.
"""

import json

import httpx
import pytest
from mcp.types import ToolAnnotations
from test_custom_route import serving

from magone_mcp_core import ConnectorServer
from magone_mcp_core.transport import transport_security

MODERN = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Mcp-Method": "tools/list",
    "MCP-Protocol-Version": "2026-07-28",
}

LIST_TOOLS = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}


def build(env: dict[str, str]) -> ConnectorServer:
    server = ConnectorServer(name="transport-test", version="0.0.0", env=env)

    @server.tool(
        name="ping",
        description="Return pong.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def ping() -> dict:
        """Return a fixed value."""
        return {"pong": True}

    return server


async def post(base_url: str, *, host: str, origin: str | None = None) -> httpx.Response:
    headers = {**MODERN, "Host": host}
    if origin:
        headers["Origin"] = origin
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        return await client.post("/mcp", headers=headers, json=LIST_TOOLS)


def tool_names(response: httpx.Response) -> list[str]:
    body = response.text
    for line in body.splitlines():
        if line.startswith("data: "):
            body = line[6:]
            break
    return [t["name"] for t in json.loads(body)["result"]["tools"]]


@pytest.fixture
def served():
    env = {
        "MCP_ALLOWED_HOSTS": "mcp-transport, mcp-transport.tools.svc.cluster.local:8443",
        "MCP_ALLOWED_ORIGINS": "https://studio.example.com",
    }
    with serving(build(env)) as url:
        yield url


class TestTheNamesACallerUses:
    async def test_the_service_name_with_a_port_is_answered(self, served):
        response = await post(served, host="mcp-transport:8000")

        assert response.status_code == 200
        assert tool_names(response) == ["ping"]

    async def test_the_service_name_without_a_port_is_answered(self, served):
        response = await post(served, host="mcp-transport")

        assert response.status_code == 200

    async def test_a_name_given_with_its_port_is_answered_on_that_port(self, served):
        response = await post(served, host="mcp-transport.tools.svc.cluster.local:8443")

        assert response.status_code == 200

    async def test_loopback_is_always_answered(self, served):
        """`docker run -p 8000:8000` arrives as localhost, configured or not."""
        response = await post(served, host="localhost:8000")

        assert response.status_code == 200


class TestEverythingElseIsRefused:
    async def test_an_unknown_host_is_refused(self, served):
        response = await post(served, host="evil.example.com")

        assert response.status_code == 421

    async def test_a_named_host_on_another_port_is_refused(self, served):
        response = await post(served, host="mcp-transport.tools.svc.cluster.local:9999")

        assert response.status_code == 421

    async def test_an_unlisted_origin_is_refused(self, served):
        response = await post(served, host="mcp-transport", origin="https://evil.example.com")

        assert response.status_code == 403

    async def test_a_listed_origin_is_answered(self, served):
        response = await post(served, host="mcp-transport", origin="https://studio.example.com")

        assert response.status_code == 200

    async def test_a_server_to_server_call_carries_no_origin_and_is_answered(self, served):
        response = await post(served, host="mcp-transport")

        assert response.status_code == 200


class TestTheExplicitOptOut:
    async def test_star_disables_the_check(self):
        with serving(build({"MCP_ALLOWED_HOSTS": "*"})) as url:
            response = await post(url, host="anything.example.com", origin="https://x.example")

        assert response.status_code == 200

    async def test_unconfigured_still_protects_rather_than_opening(self):
        """The trap in the SDK: the obvious fix for 421 turns protection off.
        Unconfigured must mean loopback only, never everything."""
        with serving(build({})) as url:
            refused = await post(url, host="mcp-transport:8000")
            answered = await post(url, host="127.0.0.1:8000")

        assert refused.status_code == 421
        assert answered.status_code == 200


class TestStatelessLegacyClients:
    async def test_a_pre_2026_client_needs_no_session_between_requests(self, served):
        """Stateless, so a second replica can answer a legacy client.

        With sessions held in process memory, the follow-up request below is
        answered `400 Missing session ID` by any replica but the one that ran
        `initialize`.
        """
        legacy = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Host": "mcp-transport",
        }
        async with httpx.AsyncClient(base_url=served, timeout=15) as client:
            init = await client.post(
                "/mcp",
                headers=legacy,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
            listed = await client.post(
                "/mcp",
                headers={**legacy, "MCP-Protocol-Version": "2025-06-18"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )

        assert init.status_code == 200
        assert listed.status_code == 200
        assert tool_names(listed) == ["ping"]


class TestTheSettings:
    def test_a_bare_name_is_allowed_with_and_without_a_port(self):
        settings = transport_security({"MCP_ALLOWED_HOSTS": "mcp-jira"})

        assert "mcp-jira" in settings.allowed_hosts
        assert "mcp-jira:*" in settings.allowed_hosts

    def test_loopback_is_present_when_nothing_is_configured(self):
        settings = transport_security({})

        assert settings.enable_dns_rebinding_protection is True
        assert "localhost:*" in settings.allowed_hosts
        assert "[::1]:*" in settings.allowed_hosts
        assert "http://localhost:*" in settings.allowed_origins

    def test_blank_entries_are_ignored(self):
        settings = transport_security({"MCP_ALLOWED_HOSTS": " , mcp-a ,,"})

        assert "" not in settings.allowed_hosts
        assert "mcp-a" in settings.allowed_hosts

    def test_an_origin_with_a_trailing_slash_still_matches(self):
        settings = transport_security({"MCP_ALLOWED_ORIGINS": "https://studio.example.com/"})

        assert "https://studio.example.com" in settings.allowed_origins
