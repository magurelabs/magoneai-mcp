"""get_issue returns comments by default, through the MCP layer.

`include_comments` defaults to true, and that branch imported
`comment_to_dict` from a `._client` module that does not exist in the port,
so a default call to get_issue raised ModuleNotFoundError. A function-level
import is invisible to every static gate: the module imports fine.
"""

import importlib

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.linear.server")

ISSUE = {
    "id": "i-1",
    "identifier": "ENG-1",
    "title": "Fix it",
    "url": "https://linear.app/x/issue/ENG-1",
    "state": {"name": "Todo"},
    "children": {"nodes": []},
    "comments": {
        "nodes": [
            {"id": "c-2", "body": "second", "url": "u2", "createdAt": "2026-09-21", "user": {"name": "b"}},
            {"id": "c-1", "body": "first", "url": "u1", "createdAt": "2026-09-20", "user": {"name": "a"}},
        ]
    },
}


async def test_a_default_get_issue_returns_comments_oldest_first(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issues": {"nodes": [ISSUE]}}})

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(
        "magone_mcp_core.server._request_headers", lambda ctx: {"x-linear-api-key": "k"}
    )

    async with Client(module.server.mcp) as client:
        result = await client.call_tool("get_issue", {"issue": "ENG-1"})

    import json

    assert result.is_error is False, result.content[0].text
    payload = json.loads(result.content[0].text)
    assert [c["body"] for c in payload["comments"]] == ["first", "second"]
