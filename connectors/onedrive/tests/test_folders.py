"""The folder tools: create, copy, move."""

import httpx
import pytest
from mcp import Client

from ._harness import (
    MONITOR_URL,
    FakeGraph,
    body,
    call,
    constants,
    install,
    module,
    respond,
    run,
)

# ====== create_folder ======


def _created(**extra):
    return FakeGraph().on("POST", "/children", respond({"id": "F9", "name": "New", **extra}))


async def test_create_folder_without_a_parent_creates_at_the_root(monkeypatch):
    graph = _created(webUrl="https://web/F9")
    result = await run(monkeypatch, "onedrive_create_folder", graph, name="New")
    assert result == {"success": True, "id": "F9", "name": "New", "web_url": "https://web/F9"}
    assert graph.requests[0].url.path == "/v1.0/me/drive/root/children"


async def test_create_folder_with_a_parent_creates_inside_it(monkeypatch):
    graph = _created()
    await run(monkeypatch, "onedrive_create_folder", graph, name="New", parent_folder_id="P1")
    assert graph.requests[0].url.path == "/v1.0/me/drive/items/P1/children"


async def test_create_folder_sends_a_trimmed_folder_body(monkeypatch):
    graph = _created()
    await run(monkeypatch, "onedrive_create_folder", graph, name="  New  ")
    assert body(graph.requests[0]) == {
        "name": "New",
        "folder": {},
        "@microsoft.graph.conflictBehavior": "rename",
    }


@pytest.mark.parametrize("behavior", ["rename", "fail", " FAIL "])
async def test_create_folder_passes_the_conflict_behavior_through(monkeypatch, behavior):
    graph = _created()
    await run(monkeypatch, "onedrive_create_folder", graph, name="New", conflict_behavior=behavior)
    expected = behavior.strip().lower()
    assert body(graph.requests[0])["@microsoft.graph.conflictBehavior"] == expected


@pytest.mark.parametrize("behavior", ["replace", "overwrite", ""])
async def test_create_folder_refuses_a_conflict_behavior_outside_its_enum(monkeypatch, behavior):
    """The SDK checks the argument is a str, not that it is in the schema's enum.

    `replace` could replace an existing folder from a tool marked non-destructive,
    so it is refused here before any request. An empty string means the default.
    """
    graph = _created()
    result = await run(
        monkeypatch, "onedrive_create_folder", graph, name="New", conflict_behavior=behavior
    )
    if behavior == "":
        assert body(graph.requests[0])["@microsoft.graph.conflictBehavior"] == "rename"
        return
    assert result == {
        "success": False,
        "error": "conflict_behavior must be one of ['rename', 'fail']",
    }
    assert graph.requests == [] and graph.token_requests == []


async def test_create_folder_reports_a_name_conflict(monkeypatch):
    error = {"error": {"message": "Name already exists"}}
    graph = FakeGraph().on("POST", "/children", respond(error, status=409))
    result = await run(
        monkeypatch, "onedrive_create_folder", graph, name="New", conflict_behavior="fail"
    )
    assert result == {
        "success": False,
        "error": "Graph API error (409): Name already exists",
        "error_code": "invalid_argument",
    }


@pytest.mark.parametrize("name", ["", "   "])
async def test_create_folder_rejects_an_empty_name(monkeypatch, name):
    graph = FakeGraph()
    result = await run(monkeypatch, "onedrive_create_folder", graph, name=name)
    assert result == {"success": False, "error": "name cannot be empty"}
    assert graph.requests == [] and graph.token_requests == []


@pytest.mark.parametrize("name", ["a/b", "../escape", "x/../y"])
async def test_create_folder_rejects_separators_and_traversal(monkeypatch, name):
    """A name is one path segment; a slash would silently retarget the create.

    It now shares `check_name` with uploads, which also refuses the other
    characters OneDrive forbids, hence the shared message.
    """
    graph = FakeGraph()
    result = await run(monkeypatch, "onedrive_create_folder", graph, name=name)
    assert result == {
        "success": False,
        "error": "name contains characters OneDrive does not allow",
    }
    assert graph.requests == []


# ====== copy_item ======


ROOT_ID = "ROOTID!101"
DRIVE_ID = "b!drive-1"
ROOT_LOOKUP = "/me/drive/root?"


def _with_root(graph: FakeGraph) -> FakeGraph:
    """Answer the root lookup the way Graph does: the real id plus the drive id."""
    root = {"id": ROOT_ID, "parentReference": {"driveId": DRIVE_ID, "driveType": "personal"}}
    return graph.first("GET", ROOT_LOOKUP, respond(root))


def _copy_graph(*polls, status=202, location=MONITOR_URL, error=None):
    headers = {"Location": location} if location else {}
    graph = _with_root(FakeGraph())
    graph.on("POST", "/copy", respond(error, status=status, headers=headers))
    if polls:
        graph.on("GET", MONITOR_URL, *[respond(p) for p in polls])
    return graph


async def test_copy_polls_the_monitor_url_until_the_copy_completes(monkeypatch):
    """Graph answers a copy with 202 + Location; the result only exists later."""
    graph = _copy_graph({"status": "inProgress"}, {"status": "completed", "resourceId": "NEW1"})
    waits = install(monkeypatch, graph)
    result, is_error = await call("onedrive_copy_item", {"item_id": "S1"})
    assert result == {"success": True, "status": "completed", "resource_id": "NEW1"}
    assert is_error is False
    assert len(graph.matching("GET", MONITOR_URL)) == 2
    assert waits == [constants.COPY_POLL_INTERVAL_SECONDS] * 2


async def test_copy_sends_the_bearer_token_to_graph_but_not_to_the_monitor(monkeypatch):
    graph = _copy_graph({"status": "completed", "resourceId": "NEW1"})
    await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    (post,) = graph.matching("POST", "/copy")
    assert post.url.path == "/v1.0/me/drive/items/S1/copy"
    assert post.headers["authorization"] == "Bearer AT"
    assert post.headers["prefer"] == "respond-async"
    assert all("authorization" not in g.headers for g in graph.matching("GET", MONITOR_URL))


async def test_copy_reports_a_server_side_failure(monkeypatch):
    graph = _copy_graph({"status": "failed"})
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result == {"success": False, "error": "Copy operation failed on server"}


async def test_copy_without_a_monitor_url_reports_acceptance(monkeypatch):
    graph = _copy_graph(location=None)
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result == {"success": True, "status": "accepted", "note": "Copy started asynchronously"}


async def test_copy_still_running_after_the_poll_budget_reports_in_progress(monkeypatch):
    """Five polls is the ceiling; a big copy legitimately outlives it."""
    graph = _copy_graph({"status": "inProgress"})
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result == {"success": True, "status": "in_progress", "note": "Copy is still in progress"}
    assert len(graph.matching("GET", MONITOR_URL)) == constants.COPY_POLL_ATTEMPTS


async def test_copy_translates_409_into_a_name_conflict(monkeypatch):
    graph = _copy_graph(status=409, location=None)
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result["success"] is False
    assert "already exists" in result["error"]


async def test_copy_surfaces_an_unexpected_status_with_graphs_message(monkeypatch):
    graph = _copy_graph(status=403, location=None, error={"error": {"message": "Access denied"}})
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result == {"success": False, "error": "Copy failed (403): Access denied"}


ROOT_REF = {"id": ROOT_ID, "driveId": DRIVE_ID}


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, {"parentReference": ROOT_REF}),
        ({"destination_folder_id": "root"}, {"parentReference": ROOT_REF}),
        ({"destination_folder_id": " ROOT "}, {"parentReference": ROOT_REF}),
        ({"destination_folder_id": "D1"}, {"parentReference": {"id": "D1", "driveId": DRIVE_ID}}),
        ({"new_name": "  copy.txt  "}, {"parentReference": ROOT_REF, "name": "copy.txt"}),
    ],
)
async def test_copy_body_names_the_destination_and_any_new_name(monkeypatch, kwargs, expected):
    """Graph refuses the "root" alias as a destination and wants the drive id on a copy.

    So the root is looked up for its real id, and every copy names the drive.
    """
    graph = _copy_graph(location=None)
    await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1", **kwargs)
    assert body(graph.matching("POST", "/copy")[0]) == expected
    (lookup,) = graph.matching("GET", ROOT_LOOKUP)
    assert lookup.url.path == "/v1.0/me/drive/root"
    assert lookup.url.params["$select"] == "id,parentReference"
    assert lookup.headers["authorization"] == "Bearer AT"


async def test_copy_reports_a_failed_root_lookup_and_sends_no_copy(monkeypatch):
    graph = FakeGraph().on("GET", ROOT_LOOKUP, respond(status=403))
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result == {"success": False, "error": "Access denied. Check permissions.", "error_code": "forbidden"}
    assert graph.matching("POST") == []


async def test_copy_refuses_a_root_lookup_with_no_id(monkeypatch):
    graph = FakeGraph().on("GET", ROOT_LOOKUP, respond({"parentReference": {}}))
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert result == {"success": False, "error": "Could not resolve the id of the drive root"}
    assert graph.matching("POST") == []


async def test_copy_uses_one_token_exchange_for_the_lookup_and_the_copy(monkeypatch):
    graph = _copy_graph(location=None)
    await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1")
    assert len(graph.token_requests) == 1


async def test_copy_rejects_a_new_name_onedrive_forbids(monkeypatch):
    """new_name goes through the same check_name as an upload filename."""
    graph = FakeGraph()
    result = await run(monkeypatch, "onedrive_copy_item", graph, item_id="S1", new_name="a/b.txt")
    assert result == {
        "success": False,
        "error": "new_name contains characters OneDrive does not allow",
    }
    assert graph.requests == []


# ====== move_item ======


def _moved(**item):
    return FakeGraph().on("PATCH", "/items/S1", respond({"id": "S1", "name": "a.txt", **item}))


async def test_move_patches_the_item_onto_the_new_parent(monkeypatch):
    graph = _moved(webUrl="https://web/S1")
    result = await run(
        monkeypatch, "onedrive_move_item", graph, item_id="S1", destination_folder_id="D1"
    )
    assert result == {"success": True, "id": "S1", "name": "a.txt", "web_url": "https://web/S1"}
    (patch,) = graph.requests
    assert patch.url.path == "/v1.0/me/drive/items/S1"
    assert body(patch) == {"parentReference": {"id": "D1"}}


@pytest.mark.parametrize("alias", ["root", "Root", " root "])
async def test_move_to_the_root_sends_the_roots_real_id(monkeypatch, alias):
    """Move docs: "your app can't use the "id": "root" syntax"; send the actual id."""
    graph = _with_root(_moved())
    result = await run(
        monkeypatch, "onedrive_move_item", graph, item_id="S1", destination_folder_id=alias
    )
    assert result["success"] is True
    (lookup,) = graph.matching("GET", ROOT_LOOKUP)
    assert lookup.url.params["$select"] == "id,parentReference"
    (patch,) = graph.matching("PATCH")
    assert body(patch) == {"parentReference": ROOT_REF}


async def test_move_to_a_folder_does_not_look_up_the_root(monkeypatch):
    graph = _with_root(_moved())
    await run(monkeypatch, "onedrive_move_item", graph, item_id="S1", destination_folder_id="D1")
    assert graph.matching("GET") == []
    assert body(graph.matching("PATCH")[0]) == {"parentReference": {"id": "D1"}}


async def test_move_without_a_destination_is_refused_before_any_request(monkeypatch):
    """Move has no default destination: an omitted one never moves the item to the root."""
    graph = _with_root(_moved())
    install(monkeypatch, graph)
    async with Client(module.server.mcp) as client:
        result = await client.call_tool("onedrive_move_item", {"item_id": "S1"})
    assert result.is_error is True
    assert "destination_folder_id" in result.content[0].text
    assert graph.requests == [] and graph.token_requests == []


async def test_move_reports_a_failed_root_lookup_and_moves_nothing(monkeypatch):
    graph = _moved().first("GET", ROOT_LOOKUP, respond(status=403))
    result = await run(
        monkeypatch, "onedrive_move_item", graph, item_id="S1", destination_folder_id="root"
    )
    assert result == {"success": False, "error": "Access denied. Check permissions.", "error_code": "forbidden"}
    assert graph.matching("PATCH") == []


async def test_move_can_rename_while_relocating(monkeypatch):
    graph = _moved()
    await run(
        monkeypatch,
        "onedrive_move_item",
        graph,
        item_id="S1",
        destination_folder_id="D1",
        new_name="  b.txt  ",
    )
    assert body(graph.requests[0])["name"] == "b.txt"


async def test_move_rejects_a_new_name_onedrive_forbids(monkeypatch):
    graph = FakeGraph()
    result = await run(
        monkeypatch,
        "onedrive_move_item",
        graph,
        item_id="S1",
        destination_folder_id="D1",
        new_name="bad:name",
    )
    assert result == {
        "success": False,
        "error": "new_name contains characters OneDrive does not allow",
    }
    assert graph.requests == []


async def test_move_returns_the_error_when_graph_fails(monkeypatch):
    error = httpx.Response(409, json={"error": {"message": "conflict"}})
    graph = FakeGraph().on("PATCH", "/items/S1", error)
    result = await run(
        monkeypatch, "onedrive_move_item", graph, item_id="S1", destination_folder_id="D1"
    )
    assert result == {
        "success": False,
        "error": "Graph API error (409): conflict",
        "error_code": "invalid_argument",
    }
