"""A served connector authenticates its caller, or refuses to start.

v1's platform mode checked `X-Service-Token` on every call. v2 checked
nothing: the token-verifier seam could not even be constructed (the SDK
refuses a verifier without auth settings), so every connector served
unauthenticated on 0.0.0.0. That matters most for a connector deployed with
platform credentials. Over real sockets, because authentication is a
property of the served application, not of the object.
"""

import time

import httpx
import pytest
from mcp.types import ToolAnnotations
from test_custom_route import serving

from magone_mcp_core import ConnectorServer
from magone_mcp_core.auth import PlatformTokenVerifier

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
RESOURCE = "https://mcp.example.com/mcp"

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


def build(env: dict[str, str], **kwargs) -> ConnectorServer:
    server = ConnectorServer(name="auth-test", version="0.0.0", env=env, **kwargs)

    @server.tool(name="ping", description="Return pong.", annotations=READ)
    async def ping() -> dict:
        """A read."""
        return {"pong": True}

    return server


async def list_tools(url: str, **headers: str) -> httpx.Response:
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:
        return await client.post("/mcp", headers={**MODERN, **headers}, json=LIST_TOOLS)


class TestTheServiceToken:
    ENV = {"MCP_MODE": "platform", "MCP_SERVICE_TOKEN": "s3cret-service-token"}

    async def test_the_right_token_is_answered(self):
        with serving(build(self.ENV)) as url:
            response = await list_tools(url, **{"X-Service-Token": "s3cret-service-token"})

        assert response.status_code == 200

    @pytest.mark.parametrize("token", [None, "", "wrong", "s3cret-service-token-extra"])
    async def test_a_missing_or_wrong_token_is_refused(self, token):
        headers = {} if token is None else {"X-Service-Token": token}
        with serving(build(self.ENV)) as url:
            response = await list_tools(url, **headers)

        assert response.status_code == 403

    async def test_the_health_probe_needs_no_token(self):
        with serving(build(self.ENV)) as url:
            async with httpx.AsyncClient(base_url=url, timeout=10) as client:
                response = await client.get("/healthz")

        assert response.status_code == 200

    async def test_local_mode_does_not_ask_for_it(self):
        with serving(build({"MCP_MODE": "local", "MCP_SERVICE_TOKEN": "x" * 20})) as url:
            response = await list_tools(url)

        assert response.status_code == 200


class TestRefusingToStartUnauthenticated:
    def test_platform_without_a_token_or_verifier_is_refused(self):
        server = build({"MCP_MODE": "platform"})

        assert "MCP_SERVICE_TOKEN" in (server.authentication_problem() or "")
        with pytest.raises(SystemExit):
            server.run(host="0.0.0.0", port=0)  # noqa: S104

    def test_hosted_without_a_verifier_is_refused(self):
        assert build({"MCP_MODE": "hosted"}).authentication_problem()

    @pytest.mark.parametrize(
        "env",
        [
            {"MCP_MODE": "platform", "MCP_SERVICE_TOKEN": "t" * 20},
            {"MCP_MODE": "platform", "MCP_ALLOW_UNAUTHENTICATED": "1"},
            {"MCP_MODE": "local"},
        ],
        ids=["service-token", "explicit-waiver", "local"],
    )
    def test_these_start(self, env):
        assert build(env).authentication_problem() is None


def introspector(claims_by_token: dict[str, dict]):
    async def introspect(token: str) -> dict | None:
        return claims_by_token.get(token)

    return introspect


VALID = {"sub": "user-1", "aud": RESOURCE, "scope": "read", "exp": time.time() + 600}


class TestATokenVerifier:
    TOKENS = {
        "good": VALID,
        "other-audience": {**VALID, "aud": "https://elsewhere.example.com/mcp"},
        "expired": {**VALID, "exp": time.time() - 60},
        "inactive": {**VALID, "active": False},
    }

    def served(self):
        verifier = PlatformTokenVerifier(RESOURCE, introspector(self.TOKENS))
        return serving(
            build(
                {"MCP_MODE": "hosted"},
                token_verifier=verifier,
                resource_server_url=RESOURCE,
                issuer_url="https://auth.example.com",
            )
        )

    def test_the_seam_can_be_constructed(self):
        """It could not: the SDK raised on a verifier without auth settings."""
        verifier = PlatformTokenVerifier(RESOURCE, introspector(self.TOKENS))

        server = build(
            {"MCP_MODE": "hosted"},
            token_verifier=verifier,
            resource_server_url=RESOURCE,
            issuer_url="https://auth.example.com",
        )

        assert server.authentication_problem() is None

    async def test_a_valid_token_for_this_resource_is_answered(self):
        with self.served() as url:
            response = await list_tools(url, Authorization="Bearer good")

        assert response.status_code == 200

    @pytest.mark.parametrize("token", ["other-audience", "expired", "inactive", "unknown"])
    async def test_every_other_token_is_401_not_500(self, token):
        with self.served() as url:
            response = await list_tools(url, Authorization=f"Bearer {token}")

        assert response.status_code == 401

    async def test_no_token_is_401_with_a_resource_metadata_challenge(self):
        with self.served() as url:
            response = await list_tools(url)

        assert response.status_code == 401
        assert "resource_metadata" in response.headers.get("www-authenticate", "")
