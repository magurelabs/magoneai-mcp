"""onedrive_search_files."""

import pytest

from ._harness import constants, file_item, folder_item, graph_answering, params, run

SEARCH = "/search("


def _search_graph(*items, **page):
    return graph_answering("GET", SEARCH, {"value": list(items), **page})


def _search_term(graph) -> str:
    return graph.requests[0].url.path.removeprefix("/v1.0/me/drive/root")


async def test_search_puts_the_term_in_graphs_search_function(monkeypatch):
    graph = _search_graph(file_item())
    result = await run(monkeypatch, "onedrive_search_files", graph, query="budget")
    assert result["success"] is True
    assert _search_term(graph) == "/search(q='budget')"


async def test_search_echoes_the_original_query_not_the_sanitized_one(monkeypatch):
    result = await run(monkeypatch, "onedrive_search_files", _search_graph(), query="o'brien")
    assert result["query"] == "o'brien"


@pytest.mark.parametrize(
    "query,expected_raw",
    [
        # A single quote is doubled (OData string escape) then percent-encoded,
        # so it cannot close the q='...' literal.
        ("o'brien", "/v1.0/me/drive/root/search(q='o%27%27brien')"),
        # '#' would otherwise truncate the path to a fragment, sending q='C'.
        ("C# notes", "/v1.0/me/drive/root/search(q='C%23%20notes')"),
        # '?' and '&' would otherwise start or extend a query string.
        ("a&b?c", "/v1.0/me/drive/root/search(q='a%26b%3Fc')"),
        ("  padded  ", "/v1.0/me/drive/root/search(q='padded')"),
    ],
)
async def test_search_escapes_and_encodes_the_term_so_it_cannot_break_the_url(
    monkeypatch, query, expected_raw
):
    """The term is interpolated into q='...'; escaping keeps it inside the literal
    and inside the path (the raw, on-the-wire bytes are what must be safe)."""
    graph = _search_graph()
    await run(monkeypatch, "onedrive_search_files", graph, query=query)
    raw_path = graph.requests[0].url.raw_path.decode().split("?", 1)[0]
    assert raw_path == expected_raw


@pytest.mark.parametrize("query", ["", "   "])
async def test_search_rejects_an_empty_query_without_calling_graph(monkeypatch, query):
    graph = _search_graph()
    result = await run(monkeypatch, "onedrive_search_files", graph, query=query)
    assert result == {"success": False, "error": "query cannot be empty"}
    assert graph.requests == [] and graph.token_requests == []


@pytest.mark.parametrize("requested,expected", [(999, constants.MAX_RESULTS_CAP), (0, 1), (10, 10)])
async def test_search_clamps_max_results(monkeypatch, requested, expected):
    graph = _search_graph()
    await run(monkeypatch, "onedrive_search_files", graph, query="x", max_results=requested)
    assert params(graph.requests[0])["$top"] == str(expected)


@pytest.mark.parametrize("file_type", ["pdf", ".pdf", "PDF"])
async def test_search_filters_by_extension_client_side(monkeypatch, file_type):
    """Graph's search has no extension filter, so the tool applies one itself."""
    graph = _search_graph(
        file_item("1", "a.pdf"), file_item("2", "b.docx"), file_item("3", "C.PDF")
    )
    result = await run(monkeypatch, "onedrive_search_files", graph, query="x", file_type=file_type)
    assert result["count"] == 2
    assert {i["name"] for i in result["items"]} == {"a.pdf", "C.PDF"}


async def test_search_drops_folders_when_an_extension_filter_is_applied(monkeypatch):
    graph = _search_graph(folder_item("F1", "x.pdf"), file_item("1", "a.pdf"))
    result = await run(monkeypatch, "onedrive_search_files", graph, query="x", file_type="pdf")
    assert [i["type"] for i in result["items"]] == ["file"]


async def test_search_keeps_folders_when_no_extension_filter_is_applied(monkeypatch):
    graph = _search_graph(folder_item("F1"), file_item("1"))
    result = await run(monkeypatch, "onedrive_search_files", graph, query="x")
    assert {i["type"] for i in result["items"]} == {"file", "folder"}


async def test_search_reports_a_further_page_via_has_more(monkeypatch):
    graph = _search_graph(**{"@odata.nextLink": "https://graph/next"})
    result = await run(monkeypatch, "onedrive_search_files", graph, query="x")
    assert result["has_more"] is True


async def test_search_returns_the_error_when_graph_fails(monkeypatch):
    error = {"error": {"message": "search unavailable"}}
    graph = graph_answering("GET", SEARCH, error, status=503)
    result = await run(monkeypatch, "onedrive_search_files", graph, query="x")
    assert result == {"success": False, "error": "Graph API error (503): search unavailable"}
