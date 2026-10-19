"""onedrive_upload_file, small files: one PUT to .../content."""

import base64

import httpx
import pytest

from ._harness import FakeGraph, call, constants, drive_item, install, module, run

DEFAULT_ITEM = httpx.Response(201, json=drive_item())


def _graph(*responses):
    return FakeGraph().on("PUT", "/content", *(responses or (DEFAULT_ITEM,)))


async def test_small_text_upload_puts_to_the_folder_path_and_returns_the_item(monkeypatch):
    graph = _graph()
    result = await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="a.txt",
        folder_path="Reports/2026",
        content="hello",
    )
    (put,) = graph.matching("PUT")
    assert put.url.path == "/v1.0/me/drive/root:/Reports/2026/a.txt:/content"
    assert put.url.params["@microsoft.graph.conflictBehavior"] == "rename"
    assert put.headers["Authorization"] == "Bearer AT"
    assert put.headers["Content-Type"] == "text/plain"
    assert put.content == b"hello"
    assert result == {
        "success": True,
        "upload_method": "simple",
        "id": "ITEM1",
        "name": "a.txt",
        "size": 1,
        "web_url": "https://onedrive.example/a.txt",
        "parent_id": "PARENT1",
        "parent_path": "/drive/root:/Reports",
    }


async def test_no_folder_means_the_drive_root(monkeypatch):
    graph = _graph()
    await run(monkeypatch, "onedrive_upload_file", graph, filename="a.txt", content="x")
    assert graph.matching("PUT")[0].url.path == "/v1.0/me/drive/root:/a.txt:/content"


async def test_folder_id_addresses_the_folder_by_id_and_wins_over_a_path(monkeypatch):
    graph = _graph()
    await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="a.txt",
        folder_id="F1",
        folder_path="Ignored",
        content="x",
    )
    assert graph.matching("PUT")[0].url.path == "/v1.0/me/drive/items/F1:/a.txt:/content"


async def test_names_are_percent_encoded(monkeypatch):
    graph = _graph()
    await run(monkeypatch, "onedrive_upload_file", graph, filename="run #1 report.txt", content="x")
    raw = graph.matching("PUT")[0].url.raw_path.decode()
    assert "/root:/run%20%231%20report.txt:/content" in raw


async def test_base64_content_is_decoded_and_typed_from_the_filename(monkeypatch):
    payload = bytes(range(256))
    graph = _graph()
    await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="blob.png",
        content_base64=base64.b64encode(payload).decode(),
    )
    put = graph.matching("PUT")[0]
    assert put.content == payload
    assert put.headers["Content-Type"] == "image/png"


async def test_an_explicit_content_type_wins_over_the_guess(monkeypatch):
    graph = _graph()
    await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="a.txt",
        content="x",
        content_type="text/markdown",
    )
    assert graph.matching("PUT")[0].headers["Content-Type"] == "text/markdown"


async def test_empty_text_is_a_legitimate_empty_file(monkeypatch):
    graph = _graph()
    result = await run(monkeypatch, "onedrive_upload_file", graph, filename="e.txt", content="")
    assert result["success"] is True
    assert graph.matching("PUT")[0].content == b""


async def test_a_file_exactly_at_the_simple_limit_still_goes_in_one_put(monkeypatch):
    graph = _graph()
    result = await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="a.txt",
        content="x" * constants.SIMPLE_UPLOAD_MAX_BYTES,
    )
    assert result["upload_method"] == "simple"
    assert graph.matching("POST") == []


@pytest.mark.parametrize("behavior", ["rename", "replace", "fail"])
async def test_conflict_behavior_reaches_graph(monkeypatch, behavior):
    graph = _graph()
    await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="a.txt",
        content="x",
        conflict_behavior=behavior,
    )
    assert graph.matching("PUT")[0].url.params["@microsoft.graph.conflictBehavior"] == behavior


async def test_fail_on_conflict_reports_the_existing_file(monkeypatch):
    graph = _graph(httpx.Response(409, json={"error": {}}))
    result = await run(
        monkeypatch,
        "onedrive_upload_file",
        graph,
        filename="a.txt",
        content="x",
        conflict_behavior="fail",
    )
    assert result == {
        "success": False,
        "error": "A file with this name already exists (conflict_behavior=fail).",
    }


async def test_429_waits_for_retry_after_then_succeeds(monkeypatch):
    graph = _graph(httpx.Response(429, headers={"Retry-After": "7"}), DEFAULT_ITEM)
    waits = install(monkeypatch, graph)
    result, _ = await call("onedrive_upload_file", {"filename": "a.txt", "content": "x"})
    assert result["success"] is True
    assert waits == [7.0]
    assert len(graph.matching("PUT")) == 2


async def test_graph_error_message_is_surfaced(monkeypatch):
    graph = _graph(httpx.Response(400, json={"error": {"message": "Name is invalid"}}))
    result = await run(monkeypatch, "onedrive_upload_file", graph, filename="a.txt", content="x")
    assert result == {"success": False, "error": "Upload failed (400): Name is invalid"}


@pytest.mark.parametrize(
    "kwargs,error_part",
    [
        ({"filename": "a.txt"}, "exactly one of"),
        ({"filename": "a.txt", "content": "x", "content_base64": "eA=="}, "exactly one of"),
        ({"filename": "", "content": "x"}, "filename is required"),
        ({"filename": "a.txt", "content_base64": "not base64!"}, "not valid base64"),
        ({"filename": "a.txt", "content": "x", "conflict_behavior": "merge"}, "conflict_behavior"),
        ({"filename": "../a.txt", "content": "x"}, "not allow"),
        ({"filename": "..", "content": "x"}, "not a valid"),
        ({"filename": "a:b.txt", "content": "x"}, "not allow"),
        ({"filename": "a.txt", "folder_path": "Docs/../..", "content": "x"}, "not a valid"),
        ({"filename": "a.txt", "folder_id": "a/b", "content": "x"}, "invalid characters"),
    ],
)
async def test_bad_arguments_are_refused_before_any_request(monkeypatch, kwargs, error_part):
    graph = _graph()
    result = await run(monkeypatch, "onedrive_upload_file", graph, **kwargs)
    assert result["success"] is False
    assert error_part in result["error"]
    assert graph.requests == [] and graph.token_requests == []


async def test_there_is_no_platform_file_id_input():
    """v1's `file_id` read a MagOne platform file; a public connector cannot."""
    schema = module.server.mcp._tool_manager._tools["onedrive_upload_file"].parameters
    assert "file_id" not in schema["properties"]
    assert schema["required"] == ["filename"]
