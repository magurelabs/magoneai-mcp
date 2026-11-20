"""`request_header` reads one routing header, and never a credential.

It is a public helper, so its docstring is a promise: a connector that calls
`request_header(ctx, "authorization")` must not get the caller's token back.
Credentials reach a tool only through that tool's own `credential_headers=`.
"""

from types import SimpleNamespace

import pytest
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, request_header
from magone_mcp_core.server import is_credential_header

SENT = {
    "authorization": "Bearer user-token",
    "proxy-authorization": "Basic cHJveHk=",
    "cookie": "session=abc",
    "x-service-token": "svc-secret",
    "x-microsoft-refresh-token": "refresh-secret",
    "x-example-api-key": "key",
    "x-example-tenant": "declared-elsewhere",
    "x-execution-id": "run-7",
    "x-mcp-enabled-groups": "browser",
}


def _ctx(headers: dict) -> SimpleNamespace:
    request = SimpleNamespace(headers=headers)
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "X-Service-Token",
        "X-Microsoft-Refresh-Token",
        "x-example-api-key",
    ],
)
def test_a_credential_header_is_never_returned(name):
    assert is_credential_header(name)
    assert request_header(_ctx(SENT), name) is None


def test_a_header_some_tool_declared_as_a_credential_is_never_returned():
    server = ConnectorServer(name="declares", version="0.1.0", env={"MCP_MODE": "local"})

    @server.tool(
        name="uses_tenant",
        description="Uses a declared header.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        credential_headers=["X-Example-Tenant"],
    )
    async def uses_tenant(credentials: dict) -> dict:
        """Reads it as a credential."""
        return {}

    assert request_header(_ctx(SENT), "X-Example-Tenant") is None


def test_a_routing_header_is_returned_case_insensitively():
    assert request_header(_ctx(SENT), "X-Execution-Id") == "run-7"
    assert request_header(_ctx(SENT), "x-mcp-enabled-groups") == "browser"
    assert request_header(_ctx(SENT), "X-Absent") is None
    assert request_header(None, "X-Execution-Id") is None
