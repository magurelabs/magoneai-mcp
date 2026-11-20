"""The file tools: list, get, download, delete. Upload has its own suites."""

import pytest

from ._harness import (
    FakeGraph,
    constants,
    file_item,
    folder_item,
    graph_answering,
    params,
    respond,
    run,
)

CHILDREN = "/children"

# ====== list_files ======


async def test_list_files_without_folder_id_lists_the_drive_root(monkeypatch):
    graph = graph_answering("GET", CHILDREN, {"value": [file_item()]})
    result = await run(monkeypatch, "onedrive_list_files", graph)
    assert result["success"] is True
    (request,) = graph.requests
    assert request.url.path == "/v1.0/me/drive/root/children"
    assert request.headers["authorization"] == "Bearer AT"


async def test_list_files_with_folder_id_lists_that_folder(monkeypatch):
    graph = graph_answering("GET", CHILDREN, {"value": []})
    await run(monkeypatch, "onedrive_list_files", graph, folder_id="F1")
    assert graph.requests[0].url.path == "/v1.0/me/drive/items/F1/children"


async def test_list_files_reports_a_further_page_via_next_cursor(monkeypatch):
    page = {"value": [file_item()], "@odata.nextLink": "https://graph/next?$skiptoken=ABC123"}
    result = await run(monkeypatch, "onedrive_list_files", graph_answering("GET", CHILDREN, page))
    assert result["has_more"] is True
    assert result["next_cursor"] == "ABC123"
    assert result["count"] == 1


async def test_list_files_has_more_is_false_on_the_last_page(monkeypatch):
    graph = graph_answering("GET", CHILDREN, {"value": [file_item()]})
    result = await run(monkeypatch, "onedrive_list_files", graph)
    assert result["has_more"] is False
    assert result["next_cursor"] is None


@pytest.mark.parametrize(
    "requested,expected",
    [(500, constants.MAX_RESULTS_CAP), (constants.MAX_RESULTS_CAP, 200), (0, 1), (-7, 1), (50, 50)],
)
async def test_list_files_clamps_max_results_into_range(monkeypatch, requested, expected):
    """An unbounded $top would let one call pull a whole drive into a prompt."""
    graph = graph_answering("GET", CHILDREN, {"value": []})
    await run(monkeypatch, "onedrive_list_files", graph, max_results=requested)
    assert params(graph.requests[0])["$top"] == str(expected)


async def test_list_files_selects_the_fields_and_orders_by_name(monkeypatch):
    graph = graph_answering("GET", CHILDREN, {"value": []})
    await run(monkeypatch, "onedrive_list_files", graph)
    sent = params(graph.requests[0])
    assert sent["$orderby"] == "name asc"
    # The pre-authorized download URL is valid for about an hour with no other
    # auth, so a listing must not request or carry it (onedrive_download_file does).
    assert "@microsoft.graph.downloadUrl" not in sent["$select"]


@pytest.mark.parametrize("cursor,expected", [(None, None), ("", None), ("TOK", "TOK")])
async def test_list_files_sends_the_cursor_as_skiptoken(monkeypatch, cursor, expected):
    # list children does not support $skip; $skipToken is its documented cursor.
    graph = graph_answering("GET", CHILDREN, {"value": []})
    kwargs = {} if cursor is None else {"cursor": cursor}
    await run(monkeypatch, "onedrive_list_files", graph, **kwargs)
    sent = params(graph.requests[0])
    assert sent.get("$skipToken") == expected
    assert "$skip" not in sent


async def test_list_files_distinguishes_files_from_folders(monkeypatch):
    page = {"value": [file_item("1", "a.txt"), folder_item("F1")]}
    result = await run(monkeypatch, "onedrive_list_files", graph_answering("GET", CHILDREN, page))
    items = result["items"]
    assert [i["type"] for i in items] == ["file", "folder"]
    # A listing never carries the pre-authorized download URL, for a file or a folder.
    assert "download_url" not in items[0]
    assert "download_url" not in items[1]
    assert "mime_type" not in items[1]


async def test_list_files_returns_graphs_error(monkeypatch):
    error = {"error": {"message": "graph exploded"}}
    graph = graph_answering("GET", CHILDREN, error, status=500)
    result = await run(monkeypatch, "onedrive_list_files", graph)
    assert result == {"success": False, "error": "Graph API error (500): graph exploded"}


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "Authentication failed. Please re-authenticate."),
        (403, "Access denied. Check permissions."),
        (404, "Resource not found."),
        (429, "Rate limit exceeded. Try again later."),
    ],
)
async def test_graph_statuses_become_messages_for_the_model(monkeypatch, status, message):
    graph = graph_answering("GET", CHILDREN, {}, status=status)
    result = await run(monkeypatch, "onedrive_list_files", graph)
    assert result == {"success": False, "error": message}


# ====== get_file ======


async def test_get_file_returns_the_fuller_projection(monkeypatch):
    item = file_item(
        "1",
        "a.txt",
        createdDateTime="2025-12-01T00:00:00Z",
        webUrl="https://web/1",
        parentReference={"id": "P1", "path": "/drive/root:/Docs"},
    )
    graph = graph_answering("GET", "/items/1", item)
    result = await run(monkeypatch, "onedrive_get_file", graph, item_id="1")
    assert result["success"] is True
    assert result["created_date"] == "2025-12-01T00:00:00Z"
    assert result["web_url"] == "https://web/1"
    assert result["parent_id"] == "P1"
    assert result["parent_path"] == "/drive/root:/Docs"
    assert graph.requests[0].url.path == "/v1.0/me/drive/items/1"


async def test_get_file_on_a_folder_reports_its_child_count(monkeypatch):
    graph = graph_answering("GET", "/items/F1", folder_item("F1", "Docs", child_count=9))
    result = await run(monkeypatch, "onedrive_get_file", graph, item_id="F1")
    assert result["type"] == "folder"
    assert result["child_count"] == 9


# ====== download_file ======


async def test_download_returns_the_preauthorized_url(monkeypatch):
    graph = graph_answering("GET", "/items/1", file_item("1", "a.txt"))
    result = await run(monkeypatch, "onedrive_download_file", graph, item_id="1")
    assert result == {
        "success": True,
        "id": "1",
        "name": "a.txt",
        "size": 42,
        "download_url": "https://dl.example/1",
    }


async def test_download_refuses_a_folder(monkeypatch):
    graph = graph_answering("GET", "/items/F1", folder_item("F1"))
    result = await run(monkeypatch, "onedrive_download_file", graph, item_id="F1")
    assert result == {"success": False, "error": "Item is a folder, not a file"}


async def test_download_reports_a_file_with_no_url_rather_than_returning_none(monkeypatch):
    item = file_item("1")
    del item["@microsoft.graph.downloadUrl"]
    result = await run(
        monkeypatch, "onedrive_download_file", graph_answering("GET", "/items/1", item), item_id="1"
    )
    assert result["success"] is False
    assert "No download URL" in result["error"]


# ====== delete_item ======


async def test_delete_issues_a_delete_against_the_item(monkeypatch):
    graph = FakeGraph().on("DELETE", "/items/X1", respond(status=204))
    result = await run(monkeypatch, "onedrive_delete_item", graph, item_id="X1")
    assert result == {"success": True, "deleted_id": "X1"}
    assert graph.requests[0].url.path == "/v1.0/me/drive/items/X1"


async def test_delete_reports_a_missing_item(monkeypatch):
    graph = FakeGraph().on("DELETE", "/items/X1", respond(status=404))
    result = await run(monkeypatch, "onedrive_delete_item", graph, item_id="X1")
    assert result == {"success": False, "error": "Resource not found."}
