"""set_email_categories merges with what is already set, in either mailbox.

Graph's `categories` PATCH replaces the whole array, so the tool reads the
current list first: `add` is idempotent, `remove` is case-insensitive, and
anything not named in `remove` survives. Through a real MCP client, with Graph
mocked at the transport.
"""

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import body_of, install, module, payload

pytestmark = pytest.mark.requires_connectors


def graph_with(existing: list[str]):
    def router(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "m-1", "categories": existing})
        return httpx.Response(200, json={"id": "m-1"})

    return router


async def call(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("set_email_categories", arguments)


async def test_the_tool_is_listed_as_a_destructive_write():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    tool = tools["set_email_categories"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.input_schema["required"] == ["email_id"]


async def test_add_and_remove_merge_with_the_existing_list(monkeypatch):
    graph = install(monkeypatch, graph_with(["Unassigned", "VIP"]))

    result = await call({"email_id": "m-1", "add": ["Assigned to Jane"], "remove": ["unassigned"]})

    assert result.is_error is False, result.content[0].text
    assert result.structured_content == {
        "success": True,
        "email_id": "m-1",
        "categories": ["VIP", "Assigned to Jane"],
    }
    get, patch = graph
    assert get.method == "GET" and get.url.params["$select"] == "categories"
    assert patch.method == "PATCH" and patch.url.path == "/v1.0/me/messages/m-1"
    assert body_of(patch) == {"categories": ["VIP", "Assigned to Jane"]}


async def test_adding_a_category_already_present_does_not_duplicate_it(monkeypatch):
    graph = install(monkeypatch, graph_with(["VIP"]))

    result = await call({"email_id": "m-1", "add": ["VIP"]})

    assert result.structured_content["categories"] == ["VIP"]
    assert body_of(graph[-1]) == {"categories": ["VIP"]}


async def test_nothing_to_do_is_refused_without_calling_graph(monkeypatch):
    graph = install(monkeypatch, graph_with([]))

    result = await call({"email_id": "m-1", "add": ["  "], "remove": []})

    assert result.is_error is True
    assert "at least one of add or remove" in payload(result)["error"]
    assert graph == []


async def test_a_shared_mailbox_goes_app_only(monkeypatch):
    graph = install(monkeypatch, graph_with([]))

    result = await call({"email_id": "m-1", "add": ["Done"], "mailbox": "shared@example.com"})

    assert result.is_error is False, result.content[0].text
    assert [r.url.path for r in graph] == ["/v1.0/users/shared@example.com/messages/m-1"] * 2
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph)


async def test_a_mailbox_outside_the_allowlist_is_refused(monkeypatch):
    graph = install(monkeypatch, graph_with([]))

    result = await call({"email_id": "m-1", "add": ["Done"], "mailbox": "other@example.com"})

    assert result.is_error is True
    assert "not permitted" in payload(result)["error"]
    assert graph == []


async def test_read_only_mode_refuses_it(monkeypatch):
    graph = install(monkeypatch, graph_with([]))
    monkeypatch.setenv("MCP_READ_ONLY", "1")

    result = await call({"email_id": "m-1", "add": ["Done"]})

    assert result.is_error is True
    assert graph == []
