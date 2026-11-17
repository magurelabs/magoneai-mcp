"""onedrive_upload_files: several inline attachments in one call.

A platform that resolves attachments rewrites run-file references into inline
{filename, content_base64, content_type} objects before the call; this tool
only ever sees that inline shape. Graph is faked at the transport.
"""

import base64
import importlib

import httpx
import pytest

from ._harness import UPLOAD_URL, FakeGraph, SessionSim, call, constants, drive_item, install

upload_many = importlib.import_module("connectors.onedrive.tools.upload_many")


def attachment(name: str, data: bytes, mime: str = "application/octet-stream") -> dict:
    return {
        "filename": name,
        "content_base64": base64.b64encode(data).decode(),
        "content_type": mime,
    }


def _graph() -> FakeGraph:
    return FakeGraph().on("PUT", "/content", httpx.Response(201, json=drive_item()))


async def upload(monkeypatch, attachments, graph=None, **kwargs):
    graph = graph or _graph()
    install(monkeypatch, graph)
    result, is_error = await call("onedrive_upload_files", {"attachments": attachments, **kwargs})
    return result, graph, is_error


async def test_each_attachment_is_put_into_the_folder(monkeypatch):
    files = [
        attachment("TC-01.png", b"\x89PNG", "image/png"),
        attachment("recording.webm", b"WEBM", "video/webm"),
    ]
    result, graph, is_error = await upload(monkeypatch, files, folder_path="Test Runs/2026-10-19")
    puts = graph.matching("PUT")
    assert [p.url.path for p in puts] == [
        "/v1.0/me/drive/root:/Test Runs/2026-10-19/TC-01.png:/content",
        "/v1.0/me/drive/root:/Test Runs/2026-10-19/recording.webm:/content",
    ]
    assert puts[1].headers["Content-Type"] == "video/webm"
    assert puts[1].content == b"WEBM"
    assert all(p.headers["Authorization"] == "Bearer AT" for p in puts)
    assert result["success"] is True and is_error is False
    assert len(result["uploaded"]) == 2
    assert result["uploaded"][0]["upload_method"] == "simple"
    assert result["failed"] == []


async def test_one_token_exchange_serves_the_whole_batch(monkeypatch):
    files = [attachment(f"{i}.txt", b"x", "text/plain") for i in range(3)]
    _, graph, _ = await upload(monkeypatch, files)
    assert len(graph.token_requests) == 1
    assert len(graph.matching("PUT")) == 3


async def test_folder_id_addresses_the_folder_by_id(monkeypatch):
    _, graph, _ = await upload(monkeypatch, [attachment("a.txt", b"x")], folder_id="F1")
    assert graph.matching("PUT")[0].url.path == "/v1.0/me/drive/items/F1:/a.txt:/content"


@pytest.mark.parametrize("behavior", ["rename", "replace", "fail"])
async def test_conflict_behavior_reaches_every_put(monkeypatch, behavior):
    files = [attachment("a.txt", b"x"), attachment("b.txt", b"y")]
    _, graph, _ = await upload(monkeypatch, files, conflict_behavior=behavior)
    sent = {p.url.params["@microsoft.graph.conflictBehavior"] for p in graph.matching("PUT")}
    assert sent == {behavior}


async def test_a_type_is_guessed_when_none_is_given(monkeypatch):
    item = {"filename": "shot.png", "content_base64": base64.b64encode(b"x").decode()}
    _, graph, _ = await upload(monkeypatch, [item])
    assert graph.matching("PUT")[0].headers["Content-Type"] == "image/png"


@pytest.mark.parametrize(
    "bad,error_part",
    [
        ({"filename": "bad.png", "content_base64": "!!"}, "not valid base64"),
        ({"filename": "../evil.txt", "content_base64": "eA=="}, "not allow"),
        ({"filename": "", "content_base64": "eA=="}, "filename is required"),
    ],
)
async def test_a_bad_file_fails_alone(monkeypatch, bad, error_part):
    result, graph, is_error = await upload(monkeypatch, [attachment("ok.png", b"x"), bad])
    assert len(graph.matching("PUT")) == 1
    assert result["success"] is False
    assert len(result["uploaded"]) == 1
    (failure,) = result["failed"]
    assert failure["filename"] == bad["filename"]
    assert error_part in failure["error"]


async def test_a_graph_refusal_for_one_file_fails_alone(monkeypatch):
    graph = FakeGraph().on(
        "PUT",
        "/content",
        httpx.Response(409, json={"error": {}}),
        httpx.Response(201, json=drive_item("b.txt")),
    )
    files = [attachment("a.txt", b"x"), attachment("b.txt", b"y")]
    result, _, _ = await upload(monkeypatch, files, graph, conflict_behavior="fail")
    assert result["failed"] == [
        {
            "filename": "a.txt",
            "error": "A file with this name already exists (conflict_behavior=fail).",
        }
    ]
    assert [u["name"] for u in result["uploaded"]] == ["b.txt"]


async def test_a_file_over_the_size_cap_fails_alone(monkeypatch):
    install(monkeypatch, _graph())
    monkeypatch.setenv("ONEDRIVE_MAX_UPLOAD_MB", "1")
    files = [attachment("big.bin", b"x" * (2 * 1024 * 1024)), attachment("ok.txt", b"x")]
    result, _ = await call("onedrive_upload_files", {"attachments": files})
    assert result["failed"][0]["filename"] == "big.bin"
    assert "upload limit" in result["failed"][0]["error"]
    assert len(result["uploaded"]) == 1


async def test_a_large_file_goes_through_an_upload_session(monkeypatch):
    total = constants.SIMPLE_UPLOAD_MAX_BYTES + 1000
    data = bytes(i % 251 for i in range(total))
    sim = SessionSim(total)
    graph = (
        FakeGraph()
        .on("POST", "/createUploadSession", httpx.Response(200, json={"uploadUrl": UPLOAD_URL}))
        .on("PUT", UPLOAD_URL, sim.put)
        .on("PUT", "/content", httpx.Response(201, json=drive_item()))
    )
    install(monkeypatch, graph)
    monkeypatch.setattr(constants, "UPLOAD_CHUNK_BYTES", 2 * constants.CHUNK_ALIGN_BYTES)
    files = [attachment("big.bin", data), attachment("small.txt", b"x")]
    result, _ = await call("onedrive_upload_files", {"attachments": files})
    assert [u["upload_method"] for u in result["uploaded"]] == ["session", "simple"]
    assert bytes(sim.received) == data
    assert all("Authorization" not in p.headers for p in graph.matching("PUT", UPLOAD_URL))


@pytest.mark.parametrize(
    "attachments",
    [
        ["exports/p1/abc_recording.webm"],
        [{"file_id": "f-123"}],
        [{"filename": "a.txt"}],
        [{"filename": "ok.txt", "content_base64": "eA=="}, "exports/p1/x.png"],
    ],
)
async def test_unresolved_references_are_refused_without_a_request(monkeypatch, attachments):
    result, graph, is_error = await upload(monkeypatch, attachments)
    assert result == {"success": False, "error": upload_many.UNRESOLVED_MESSAGE}
    assert "resolves attachments" in result["error"]
    assert is_error is True
    assert graph.requests == [] and graph.token_requests == []


async def test_empty_and_oversized_batches_are_refused(monkeypatch):
    empty, graph, _ = await upload(monkeypatch, [])
    assert empty == {"success": False, "error": "attachments must be a non-empty list"}
    too_many = [attachment(f"{i}.txt", b"x") for i in range(upload_many.MAX_FILES_PER_CALL + 1)]
    many, graph, _ = await upload(monkeypatch, too_many)
    assert many["success"] is False
    assert graph.requests == [] and graph.token_requests == []


async def test_twenty_files_is_allowed(monkeypatch):
    files = [attachment(f"{i}.txt", b"x") for i in range(upload_many.MAX_FILES_PER_CALL)]
    result, graph, _ = await upload(monkeypatch, files)
    assert result["success"] is True
    assert len(graph.matching("PUT")) == upload_many.MAX_FILES_PER_CALL


async def test_a_bad_conflict_behavior_is_refused_before_any_request(monkeypatch):
    install(monkeypatch, _graph())
    result = await upload_many_direct(conflict_behavior="merge")
    assert "conflict_behavior must be one of" in result["error"]


async def upload_many_direct(**kwargs):
    """The handler directly: the schema's enum would stop this over MCP."""
    module = importlib.import_module("connectors.onedrive.server")
    return await module.onedrive_upload_files({}, attachments=[attachment("a", b"x")], **kwargs)


async def test_a_failed_token_exchange_fails_the_call(monkeypatch):
    graph = _graph()
    install(monkeypatch, graph, headers={})
    result, _ = await call("onedrive_upload_files", {"attachments": [attachment("a.txt", b"x")]})
    assert result == {"success": False, "error": "Missing X-Microsoft-Refresh-Token header"}
    assert graph.requests == []
