"""The sharing tools: create share links, list items shared with the user."""

from datetime import UTC, datetime

import pytest

from ._harness import FakeGraph, body, constants, install, module, respond, run

_LINK_RESPONSE = {
    "link": {"webUrl": "https://share/abc", "type": "view", "scope": "organization"},
    "expirationDateTime": "2026-02-01T00:00:00Z",
}


def _item_graph(item=None):
    """The item GET (no `root` facet by default) followed by the createLink POST.

    create_share_link now GETs the item first to refuse the drive root even by its
    real id, so a share-link test answers both requests.
    """
    graph = FakeGraph()
    graph.on("GET", "/items/", respond(item if item is not None else {"id": "X1"}))
    graph.on("POST", "/createLink", respond(_LINK_RESPONSE))
    return graph


def _link_graph():
    return _item_graph()


def _post(graph):
    return graph.matching("POST", "/createLink")[0]


def _sent_expiry_days(graph) -> float:
    sent = body(_post(graph))["expirationDateTime"]
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
    assert _post(graph).url.path == "/v1.0/me/drive/items/X1/createLink"


async def test_create_share_link_defaults_to_an_organization_view_link(monkeypatch):
    """The safer default: not anonymous, not editable, and no expiry sent."""
    graph = _link_graph()
    await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1")
    assert body(_post(graph)) == {"type": "view", "scope": "organization"}


@pytest.mark.parametrize("link_type", constants.VALID_LINK_TYPES)
async def test_create_share_link_accepts_every_advertised_link_type(monkeypatch, link_type):
    graph = _link_graph()
    await run(monkeypatch, "onedrive_create_share_link", graph, item_id="X1", link_type=link_type)
    assert body(_post(graph))["type"] == link_type


@pytest.mark.parametrize("scope", constants.VALID_LINK_SCOPES)
async def test_create_share_link_accepts_every_advertised_scope(monkeypatch, scope):
    graph = _link_graph()
    # An anonymous link requires an expiry, so pass one to exercise both scopes.
    await run(
        monkeypatch, "onedrive_create_share_link", graph, item_id="X1", scope=scope, expiry_days=7
    )
    assert body(_post(graph))["scope"] == scope


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
    assert "expirationDateTime" not in body(_post(graph))


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


async def test_create_share_link_refuses_the_drive_root_by_its_real_id(monkeypatch):
    """The root alias is refused up front; its real id is caught by the `root` facet."""
    graph = _item_graph({"id": "ROOT1", "root": {}})
    result = await run(monkeypatch, "onedrive_create_share_link", graph, item_id="ROOT1")
    assert result["success"] is False and "drive root" in result["error"]
    assert graph.matching("POST", "/createLink") == []  # never reached createLink


async def test_create_share_link_returns_the_error_when_graph_fails(monkeypatch):
    error = {"error": {"message": "sharing disabled by policy"}}
    graph = FakeGraph()
    graph.on("GET", "/items/", respond({"id": "X1"}))
    graph.on("POST", "/createLink", respond(error, status=400))
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
