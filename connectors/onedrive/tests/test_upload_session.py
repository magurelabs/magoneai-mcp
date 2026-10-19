"""onedrive_upload_file, large files: Graph upload sessions."""

import base64

import httpx
import pytest

from ._harness import (
    UPLOAD_URL,
    FakeGraph,
    SessionSim,
    body,
    call,
    constants,
    install,
    upload_graph,
)

# Small fragments keep the tests fast; the real size is pinned separately.
CHUNK = 2 * constants.CHUNK_ALIGN_BYTES
TOTAL = constants.SIMPLE_UPLOAD_MAX_BYTES + 1000
DATA = bytes(i % 251 for i in range(TOTAL))
DATA_B64 = base64.b64encode(DATA).decode()


def _session_graph(sim: SessionSim) -> FakeGraph:
    return (
        FakeGraph()
        .on("POST", "/createUploadSession", httpx.Response(200, json={"uploadUrl": UPLOAD_URL}))
        .on("PUT", UPLOAD_URL, sim.put)
        .on("GET", UPLOAD_URL, sim.status)
        .on("DELETE", UPLOAD_URL, httpx.Response(204))
    )


async def _upload(monkeypatch, sim: SessionSim, graph: FakeGraph | None = None, **kwargs):
    graph = graph or _session_graph(sim)
    waits = install(monkeypatch, graph)
    monkeypatch.setattr(constants, "UPLOAD_CHUNK_BYTES", CHUNK)
    args = {"filename": "big.bin", "folder_path": "Runs", "content_base64": DATA_B64, **kwargs}
    result, _ = await call("onedrive_upload_file", args)
    return result, graph, waits


def _expected_ranges() -> list[str]:
    return [f"bytes {s}-{min(s + CHUNK, TOTAL) - 1}/{TOTAL}" for s in range(0, TOTAL, CHUNK)]


def test_real_fragment_size_is_10_mib_and_a_multiple_of_320_kib():
    assert constants.UPLOAD_CHUNK_BYTES == 10 * 1024 * 1024
    assert constants.UPLOAD_CHUNK_BYTES % constants.CHUNK_ALIGN_BYTES == 0
    assert CHUNK % constants.CHUNK_ALIGN_BYTES == 0


async def test_over_4_mb_creates_a_session_and_sends_aligned_content_ranges(monkeypatch):
    sim = SessionSim(TOTAL)
    result, graph, _ = await _upload(monkeypatch, sim)
    assert result["success"] is True
    assert result["upload_method"] == "session"
    (create,) = graph.matching("POST", "/createUploadSession")
    assert create.url.path == "/v1.0/me/drive/root:/Runs/big.bin:/createUploadSession"
    assert create.headers["Authorization"] == "Bearer AT"
    assert sim.ranges == _expected_ranges()
    assert bytes(sim.received) == DATA
    assert graph.matching("PUT", "/content") == []


async def test_the_preauthorized_upload_url_never_gets_the_bearer_token(monkeypatch):
    sim = SessionSim(TOTAL)
    _, graph, _ = await _upload(monkeypatch, sim)
    sent = graph.matching("PUT", UPLOAD_URL)
    assert sent and all("Authorization" not in p.headers for p in sent)


@pytest.mark.parametrize("behavior", ["rename", "replace", "fail"])
async def test_conflict_behavior_goes_in_the_session_body(monkeypatch, behavior):
    _, graph, _ = await _upload(monkeypatch, SessionSim(TOTAL), conflict_behavior=behavior)
    item = body(graph.matching("POST", "/createUploadSession")[0])["item"]
    assert item == {"@microsoft.graph.conflictBehavior": behavior, "name": "big.bin"}


async def test_a_503_mid_upload_queries_the_session_and_resumes(monkeypatch):
    sim = SessionSim(TOTAL, failures={2: httpx.Response(503)})
    result, graph, waits = await _upload(monkeypatch, sim)
    assert result["success"] is True
    assert bytes(sim.received) == DATA
    expected = _expected_ranges()
    assert sim.ranges == expected[:3] + expected[2:]
    assert len(graph.matching("GET", UPLOAD_URL)) == 1
    assert waits == [2.0]


async def test_a_429_waits_for_retry_after_before_resuming(monkeypatch):
    throttled = httpx.Response(429, headers={"Retry-After": "12"})
    sim = SessionSim(TOTAL, failures={1: throttled})
    result, _, waits = await _upload(monkeypatch, sim)
    assert result["success"] is True
    assert waits == [12.0]
    assert bytes(sim.received) == DATA


async def test_retry_after_is_capped(monkeypatch):
    throttled = httpx.Response(429, headers={"Retry-After": "9999"})
    _, _, waits = await _upload(monkeypatch, SessionSim(TOTAL, failures={1: throttled}))
    assert waits == [upload_graph.MAX_BACKOFF_SECONDS]


async def test_persistent_failure_gives_up_and_cancels_the_session(monkeypatch):
    failures = {i: httpx.Response(503) for i in range(1, 20)}
    result, graph, waits = await _upload(monkeypatch, SessionSim(TOTAL, failures=failures))
    assert result["success"] is False
    assert "503" in result["error"]
    assert len(waits) == upload_graph.MAX_RETRIES
    assert len(graph.matching("DELETE", UPLOAD_URL)) == 1


async def test_an_expired_session_is_reported(monkeypatch):
    sim = SessionSim(TOTAL, failures={1: httpx.Response(503)})
    graph = _session_graph(sim).first("GET", UPLOAD_URL, httpx.Response(404))
    result, _, _ = await _upload(monkeypatch, sim, graph)
    assert result == {
        "success": False,
        "error": "The upload session expired. Try the upload again.",
    }


async def test_a_name_conflict_at_session_creation_is_reported(monkeypatch):
    graph = FakeGraph().on("POST", "/createUploadSession", httpx.Response(409, json={}))
    result, graph, _ = await _upload(
        monkeypatch, SessionSim(TOTAL), graph, conflict_behavior="fail"
    )
    assert "already exists" in result["error"]
    assert graph.matching("PUT") == []


async def test_a_non_https_upload_url_is_refused(monkeypatch):
    plain = httpx.Response(200, json={"uploadUrl": "http://evil.example/upload"})
    graph = FakeGraph().on("POST", "/createUploadSession", plain)
    result, graph, _ = await _upload(monkeypatch, SessionSim(TOTAL), graph)
    assert result == {"success": False, "error": "Graph returned no usable upload URL."}
    assert graph.matching("PUT") == []


async def test_total_size_is_capped_by_env_before_any_request(monkeypatch):
    graph = FakeGraph()
    install(monkeypatch, graph)
    monkeypatch.setenv("ONEDRIVE_MAX_UPLOAD_MB", "1")
    result, _ = await call(
        "onedrive_upload_file", {"filename": "a.txt", "content": "x" * (1024 * 1024 + 1)}
    )
    assert result["success"] is False
    assert "upload limit" in result["error"]
    assert graph.requests == [] and graph.token_requests == []


async def test_base64_size_is_checked_before_decoding(monkeypatch):
    graph = FakeGraph()
    install(monkeypatch, graph)
    monkeypatch.setenv("ONEDRIVE_MAX_UPLOAD_MB", "1")
    encoded = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()
    result, _ = await call("onedrive_upload_file", {"filename": "a.bin", "content_base64": encoded})
    assert "upload limit" in result["error"]
    assert graph.requests == []


@pytest.mark.parametrize("raw,expected_mb", [(None, 250), ("10", 10), ("junk", 250), ("0", 1)])
def test_max_upload_defaults_to_250_mb(monkeypatch, raw, expected_mb):
    if raw is None:
        monkeypatch.delenv("ONEDRIVE_MAX_UPLOAD_MB", raising=False)
    else:
        monkeypatch.setenv("ONEDRIVE_MAX_UPLOAD_MB", raw)
    assert constants.max_upload_bytes() == expected_mb * 1024 * 1024
