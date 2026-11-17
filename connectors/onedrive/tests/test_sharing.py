"""The sharing tools: create share links, list items shared with the user."""

from datetime import UTC, datetime

import pytest

from ._harness import FakeGraph, body, constants, graph_answering, install, module, params, run

_LINK_RESPONSE = {
    "link": {"webUrl": "https://share/abc", "type": "view", "scope": "organization"},
    "expirationDateTime": "2026-02-01T00:00:00Z",
}


def _link_graph():
    return graph_answering("POST", "/createLink", _LINK_RESPONSE)


def _sent_expiry_days(graph) -> float:
    sent = body(graph.requests[0])["expirationDateTime"]
    parsed = datetime.strptime(sent, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (parsed - datetime.now(UTC)).total_seconds() / 86400


# ====== create_share_link ======


async def test_create_share_link_posts_to_create_link_and_unwraps_the_response(monkeypatch):
    graph = _link_graph()
    result = await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1")
    assert result == {
        "success": True,
        "share_url": "https://share/abc",
        "link_type": "view",
        "scope": "organization",
        "expiry": "2026-02-01T00:00:00Z",
    }
    assert graph.requests[0].url.path == "/v1.0/me/drive/items/X1/createLink"


async def test_create_share_link_defaults_to_an_organization_view_link(monkeypatch):
    """The safer default: not anonymous, not editable, and no expiry sent."""
    graph = _link_graph()
    await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1")
    assert body(graph.requests[0]) == {"type": "view", "scope": "organization"}


@pytest.mark.parametrize("link_type", constants.VALID_LINK_TYPES)
async def test_create_share_link_accepts_every_advertised_link_type(monkeypatch, link_type):
    graph = _link_graph()
    await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1", link_type=link_type)
    assert body(graph.requests[0])["type"] == link_type


@pytest.mark.parametrize("scope", constants.VALID_LINK_SCOPES)
async def test_create_share_link_accepts_every_advertised_scope(monkeypatch, scope):
    graph = _link_graph()
    # An anonymous link requires an expiry, so pass one to exercise both scopes.
    await run(
        monkeypatch, "onedrive_create_share_link", graph, item_id="X1", scope=scope, expiry_days=7
    )
    assert body(graph.requests[0])["scope"] == scope


async def test_create_share_link_refuses_the_drive_root(monkeypatch):
    """`/items/root` is the whole drive; an anonymous link to it leaks everything."""
    graph = FakeGraph()
    result = await run(
        monkeypatch, "onedrive_create_share_link", graph, item_id="root", scope="anonymous"
    )
    assert result["success"] is False
    assert "drive root" in result["error"]
    assert graph.requests == [] and graph.token_requests == []


async def test_create_share_link_requires_an_expiry_for_an_anonymous_link(monkeypatch):
    graph = FakeGraph()
    result = await run(
        monkeypatch, "onedrive_create_share_link", graph, item_id="X1", scope="anonymous"
    )
    assert result["success"] is False
    assert "expiry_days" in result["error"] and "anonymous" in result["error"]
    assert graph.requests == [] and graph.token_requests == []


async def test_create_share_link_allows_an_organization_link_without_an_expiry(monkeypatch):
    graph = _link_graph()
    result = await run(
        monkeypatch, "onedrive_create_share_link", graph, item_id="X1", scope="organization"
    )
    assert result["success"] is True
    assert "expirationDateTime" not in body(graph.requests[0])


async def test_create_share_link_sends_a_utc_expiry_in_graphs_format(monkeypatch):
    graph = _link_graph()
    await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1", expiry_days=7)
    assert 6.9 < _sent_expiry_days(graph) < 7.1


@pytest.mark.parametrize("requested,expected", [(0, 1), (-5, 1), (90, 30), (30, 30)])
async def test_create_share_link_clamps_expiry_to_one_to_thirty_days(
    monkeypatch, requested, expected
):
    graph = _link_graph()
    await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1", expiry_days=requested)
    assert expected - 0.1 < _sent_expiry_days(graph) < expected + 0.1


async def test_create_share_link_returns_the_error_when_graph_fails(monkeypatch):
    error = {"error": {"message": "sharing disabled by policy"}}
    graph = graph_answering("POST", "/createLink", error, status=400)
    result = await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1")
    assert result == {
        "success": False,
        "error": "Graph API error (400): sharing disabled by policy",
    }


async def test_an_unknown_link_type_is_refused_before_any_request(monkeypatch):
    """The schema's enum guards this over MCP; the handler re-checks for direct calls."""
    graph = FakeGraph()
    install(monkeypatch, graph)
    result = await module.onedrive_create_share_link({}, item_id="X1", link_type="admin")
    assert result["success"] is False
    assert "link_type must be one of" in result["error"]
    result = await module.onedrive_create_share_link({}, item_id="X1", scope="public")
    assert "scope must be one of" in result["error"]
    assert graph.requests == [] and graph.token_requests == []


# ====== list_recent ======
#
# Replaces onedrive_list_shared_with_me: Microsoft deprecated /me/drive/sharedWithMe
# (it stops returning data after November 2026) with no successor, so recent items
# are the supported way to resurface files, including ones shared from other drives.


def _recent_own(id_="S1", name="recent.txt"):
    """A recently used item that lives on the caller's own drive."""
    return {
        "id": id_,
        "name": name,
        "file": {"mimeType": "text/plain"},
        "size": 10,
        "lastModifiedDateTime": "2026-01-01T00:00:00Z",
        "webUrl": "https://web/S1",
    }


def _recent_shared(id_="S1", name="shared.txt", owner="Ada Lovelace", remote_id="R1"):
    """A recently used item shared from another drive, nested under remoteItem."""
    return {
        "id": id_,
        "name": name,
        "remoteItem": {
            "id": remote_id,
            "size": 10,
            "webUrl": "https://web/S1",
            "parentReference": {"driveId": "D-OWNER"},
            "shared": {"owner": {"user": {"displayName": owner}}},
        },
    }


def _recent_graph(*items):
    return graph_answering("GET", "/me/drive/recent", {"value": list(items)})


async def test_recent_reads_the_recent_collection(monkeypatch):
    graph = _recent_graph(_recent_own())
    result = await run(monkeypatch, "onedrive_list_recent", graph)
    assert result["success"] is True and result["count"] == 1
    assert graph.requests[0].url.path == "/v1.0/me/drive/recent"


async def test_recent_digs_the_sharer_and_ids_out_of_the_nested_remote_item(monkeypatch):
    graph = _recent_graph(_recent_shared(owner="Grace Hopper"))
    item = (await run(monkeypatch, "onedrive_list_recent", graph))["items"][0]
    assert item["shared_by"] == "Grace Hopper"
    assert item["remote_item_id"] == "R1"
    assert item["remote_drive_id"] == "D-OWNER"


async def test_recent_tolerates_an_own_item_with_no_remote(monkeypatch):
    """An item on the caller's own drive has no remoteItem; the tool must not raise."""
    result = await run(monkeypatch, "onedrive_list_recent", _recent_graph(_recent_own()))
    item = result["items"][0]
    assert item["shared_by"] is None
    assert item["remote_item_id"] is None
    assert item["remote_drive_id"] is None


async def test_recent_labels_folders_as_folders(monkeypatch):
    graph = _recent_graph({"id": "S4", "name": "Team", "folder": {"childCount": 2}})
    item = (await run(monkeypatch, "onedrive_list_recent", graph))["items"][0]
    assert item["type"] == "folder"


@pytest.mark.parametrize("requested,expected", [(999, constants.MAX_RESULTS_CAP), (0, 1), (5, 5)])
async def test_recent_clamps_max_results(monkeypatch, requested, expected):
    graph = _recent_graph()
    await run(monkeypatch, "onedrive_list_recent", graph, max_results=requested)
    assert params(graph.requests[0])["$top"] == str(expected)


async def test_recent_bounds_the_list_even_if_graph_ignores_top(monkeypatch):
    """/me/drive/recent does not always honour $top, so the tool slices too."""
    graph = _recent_graph(*[_recent_own(id_=str(i)) for i in range(10)])
    result = await run(monkeypatch, "onedrive_list_recent", graph, max_results=3)
    assert result["count"] == 3


async def test_recent_returns_the_error_when_graph_fails(monkeypatch):
    graph = graph_answering("GET", "/me/drive/recent", {}, status=403)
    result = await run(monkeypatch, "onedrive_list_recent", graph)
    assert result == {"success": False, "error": "Access denied. Check permissions."}
