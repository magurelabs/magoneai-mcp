"""Credentials, scopes and the tool contract.

The caller's Microsoft credentials arrive per call as request headers, the
framework folds them into `credentials`, and each call exchanges the refresh
token at the Microsoft token endpoint. These tests drive that through a real
MCP client with only the transport mocked.
"""

import importlib
import re
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import structlog
from mcp import Client

from ._harness import HEADERS, FakeGraph, call, file_item, graph_answering, install, module

shared = importlib.import_module("connectors.onedrive._shared")

CONNECTORS = Path(__file__).resolve().parents[2]
OTHER_MICROSOFT = ["microsoft_sharepoint", "microsoft_teams", "outlook_calendar", "outlook_email"]
READ_TOOLS = {
    "onedrive_list_files",
    "onedrive_get_file",
    "onedrive_download_file",
    "onedrive_list_recent",
    "onedrive_search_files",
}
DESTRUCTIVE_TOOLS = {
    "onedrive_upload_file",
    "onedrive_upload_files",
    "onedrive_delete_item",
    "onedrive_move_item",
}


def _listing():
    return graph_answering("GET", "/children", {"value": [file_item()]})


async def test_the_refresh_token_is_exchanged_with_the_callers_own_app(monkeypatch):
    graph = _listing()
    install(monkeypatch, graph)
    await call("onedrive_list_files")
    (token,) = graph.token_requests
    assert token.url.path == "/tenant-1/oauth2/v2.0/token"
    form = {k: v[0] for k, v in parse_qs(token.content.decode()).items()}
    assert form == {
        "client_id": "cid-1",
        "client_secret": "cs-secret",
        "refresh_token": "rt-secret",
        "grant_type": "refresh_token",
        "scope": "https://graph.microsoft.com/.default offline_access",
    }
    assert graph.requests[0].headers["authorization"] == "Bearer AT"


async def test_the_tenant_defaults_to_common(monkeypatch):
    graph = _listing()
    headers = {k: v for k, v in HEADERS.items() if k != "x-microsoft-tenant-id"}
    install(monkeypatch, graph, headers=headers)
    await call("onedrive_list_files")
    assert graph.token_requests[0].url.path == "/common/oauth2/v2.0/token"


@pytest.mark.parametrize(
    "missing,message",
    [
        ("x-microsoft-refresh-token", "Missing X-Microsoft-Refresh-Token header"),
        ("x-microsoft-client-secret", "Missing OAuth credentials"),
    ],
)
async def test_a_missing_credential_is_named_and_nothing_is_sent(monkeypatch, missing, message):
    graph = _listing()
    install(monkeypatch, graph, headers={k: v for k, v in HEADERS.items() if k != missing})
    result, is_error = await call("onedrive_list_files")
    assert is_error is True
    assert message in result["error"]
    assert graph.requests == [] and graph.token_requests == []


async def test_a_refused_refresh_is_reported(monkeypatch):
    graph = _listing()
    real = graph.__call__

    def refuse(request):
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(400, json={"error_description": "AADSTS70000: bad grant"})
        return real(request)

    install(monkeypatch, refuse)
    result, _ = await call("onedrive_list_files")
    assert result == {
        "success": False,
        "error": "Token refresh failed (400): AADSTS70000: bad grant",
    }


async def test_undeclared_headers_never_reach_the_tool(monkeypatch):
    seen = {}

    async def spy(credentials, method, endpoint, json_data=None, params=None):
        seen.update(credentials)
        return {"value": []}

    install(monkeypatch, FakeGraph(), headers={**HEADERS, "x-zoom-client-secret": "zoom"})
    monkeypatch.setattr(module, "graph_request", spy)
    await call("onedrive_list_files")
    assert seen == {
        "microsoft_refresh_token": "rt-secret",
        "microsoft_client_id": "cid-1",
        "microsoft_client_secret": "cs-secret",
        "microsoft_tenant_id": "tenant-1",
    }


async def test_no_secret_reaches_a_log_line_or_a_result(monkeypatch):
    graph = graph_answering("GET", "/children", {"error": {"message": "boom"}}, status=500)
    install(monkeypatch, graph)
    with structlog.testing.capture_logs() as logs:
        result, _ = await call("onedrive_list_files")
    text = repr(logs) + repr(result)
    for secret in ("rt-secret", "cs-secret", "Bearer AT"):
        assert secret not in text


def test_onedrive_requests_only_its_own_least_privilege_scopes():
    assert shared.ONEDRIVE_OAUTH_SCOPES == [
        "offline_access",
        "User.Read",
        "https://graph.microsoft.com/Files.ReadWrite",
    ]
    assert not any(s.endswith(".All") for s in shared.ONEDRIVE_OAUTH_SCOPES)


@pytest.mark.parametrize("package", OTHER_MICROSOFT)
def test_other_microsoft_connectors_ask_for_no_files_write_scope(package):
    """Their consent stays as it was: nothing of OneDrive's write access leaks into them."""
    for path in (CONNECTORS / package).rglob("*.py"):
        assert "Files.ReadWrite" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("package", OTHER_MICROSOFT)
def test_other_microsoft_connectors_keep_their_token_scope(package):
    text = "".join(p.read_text(encoding="utf-8") for p in (CONNECTORS / package).rglob("*.py"))
    scopes = set(re.findall(r'"scope": "([^"]+)"', text))
    assert scopes <= {
        "https://graph.microsoft.com/.default offline_access",
        "https://graph.microsoft.com/.default",
    }


async def test_every_tool_declares_its_annotations_group_and_scope():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert len(tools) == 12
    for name, tool in tools.items():
        meta = tool.meta or {}
        read_only = name in READ_TOOLS
        assert tool.annotations.read_only_hint is read_only, name
        assert tool.annotations.destructive_hint is (name in DESTRUCTIVE_TOOLS), name
        assert meta["io.magone/permission_group"] == (
            "read_onedrive" if read_only else "write_onedrive"
        )
        assert meta["io.magone/required_scopes"] == [shared.FILES_READWRITE_SCOPE]
        assert "credentials" not in (tool.input_schema.get("properties") or {})
