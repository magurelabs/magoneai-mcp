"""The browser relay end to end: served over real HTTP, upstream faked at the transport.

Served with uvicorn on an ephemeral port because the session key and the
connection's tool selection both come from request headers, and only a served
app has request headers. The upstream is `FakeUpstream` behind an
`httpx.MockTransport`, so nothing leaves the process.
"""

import contextlib
import json
import socket
import threading
import time

import httpx
import pytest
from mcp import Client

from .conftest import module
from .fakes import IMAGE

LOCAL = {"publish_artifact", "list_artifacts", "publish_test_report"}
RELAYED = {"browser_navigate", "browser_take_screenshot", "browser_close"}
HEADERS = {
    "X-Execution-Id": "exec-1",
    "X-Project-Id": "p-1",
    "X-Service-Token": "svc-secret",
    "Authorization": "Bearer user-token",
    "X-Microsoft-Refresh-Token": "refresh-secret",
}
MODERN = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}
INIT_META = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}


@contextlib.contextmanager
def serving():
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(module.server.app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn did not start; this test would prove nothing")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def rpc(url: str, method: str, params: dict | None = None, headers: dict | None = None):
    params = params or {}
    body = {"jsonrpc": "2.0", "id": 7, "method": method, "params": {**INIT_META, **params}}
    routing = {"Mcp-Method": method}
    if "name" in params:
        routing["Mcp-Name"] = str(params["name"])
    sent = HEADERS if headers is None else headers
    async with httpx.AsyncClient(base_url=url, timeout=20) as client:
        response = await client.post("/mcp", headers={**MODERN, **routing, **sent}, json=body)
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])["result"]
        raise AssertionError(f"no data frame in {response.text!r}")
    return response.json()["result"]


async def names(url: str, headers: dict | None = None) -> set[str]:
    return {t["name"] for t in (await rpc(url, "tools/list", headers=headers))["tools"]}


async def call(url: str, name: str, arguments: dict | None = None, headers: dict | None = None):
    params = {"name": name, "arguments": arguments or {}}
    return await rpc(url, "tools/call", params, headers)


def text(result: dict) -> str:
    return result["content"][0]["text"]


async def test_listing_merges_upstream_and_local_and_hides_unsafe(upstream):
    with serving() as url:
        listed = (await rpc(url, "tools/list"))["tools"]
    tools = {t["name"]: t for t in listed}
    assert set(tools) == LOCAL | RELAYED
    navigate = tools["browser_navigate"]
    assert navigate["inputSchema"] == {"type": "object", "properties": {"url": {"type": "string"}}}
    assert navigate["annotations"]["readOnlyHint"] is False
    assert navigate["_meta"]["io.magone/permission_group"] == "browser"
    assert tools["list_artifacts"]["_meta"]["io.magone/permission_group"] == "artifacts"


async def test_a_tool_not_on_the_allowlist_is_never_relayed(upstream):
    """The upstream lists browser_file_upload; the connector does not relay it."""
    with serving() as url:
        listed = await names(url)
        refused = await call(url, "browser_file_upload", {"paths": ["/etc/passwd"]})
    assert "browser_file_upload" not in listed
    assert refused["isError"] is True
    assert "browser_file_upload" not in {name for _, name in upstream.calls}


def test_the_allowlist_withholds_a_tool_whose_schema_drifted():
    from connectors.playwright.allowlist import schema_matches_snapshot

    reviewed = {
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    }
    assert schema_matches_snapshot("browser_navigate", reviewed)
    drifted = {**reviewed, "inputSchema": {"type": "object", "properties": {"href": {}}}}
    assert not schema_matches_snapshot("browser_navigate", drifted)
    assert not schema_matches_snapshot("browser_file_upload", {"inputSchema": {"type": "object"}})


async def test_relayed_result_is_returned_as_the_upstream_sent_it(upstream):
    with serving() as url:
        result = await call(url, "browser_take_screenshot")
    assert result["content"] == [{"type": "text", "text": "ran browser_take_screenshot"}, IMAGE]
    assert result.get("isError") in (None, False)


async def test_a_browser_call_before_any_listing_loads_the_manifest(upstream):
    with serving() as url:
        result = await call(url, "browser_navigate", {"url": "https://example.com"})
    assert text(result) == "ran browser_navigate"


async def test_unsafe_tools_are_refused_at_call_time(upstream):
    with serving() as url:
        for name in ("browser_evaluate", "browser_run_code_unsafe", "browser_run_code"):
            result = await call(url, name, {"function": "() => document.cookie"})
            assert result["isError"] is True and "disabled" in text(result)
    assert fake_tools_called(upstream) == []


def fake_tools_called(fake) -> list[str]:
    return [name for _, name in fake.calls]


async def test_unsafe_tools_exposed_when_enabled(upstream):
    upstream.reset(expose_unsafe=True)
    with serving() as url:
        assert "browser_evaluate" in await names(url)
        result = await call(url, "browser_evaluate")
    assert result.get("isError") in (None, False)
    assert fake_tools_called(upstream) == ["browser_evaluate"]


async def test_no_caller_header_reaches_the_upstream(upstream):
    with serving() as url:
        await call(url, "browser_navigate", {"url": "https://example.com"})
    for request in upstream.requests:
        sent = {k.lower() for k in request.headers}
        assert not sent & {
            "x-service-token",
            "authorization",
            "x-microsoft-refresh-token",
            "x-project-id",
            "x-execution-id",
        }
    bodies = [json.loads(r.content) for r in upstream.requests if r.method == "POST"]
    (tool_call,) = [b for b in bodies if b.get("method") == "tools/call"]
    assert tool_call["params"] == {
        "name": "browser_navigate",
        "arguments": {"url": "https://example.com"},
    }


async def test_session_follows_the_run_header(upstream):
    with serving() as url:
        await call(url, "browser_navigate")
        await call(url, "browser_navigate")
        await call(url, "browser_navigate", headers={**HEADERS, "X-Execution-Id": "exec-2"})
        refused = await call(url, "browser_navigate", headers={})
    sids = [sid for sid, _ in upstream.calls]
    # exec-1 shares a session across its two calls, exec-2 gets its own, and the
    # call with no identity and no run header is refused rather than sharing one.
    assert sids[0] == sids[1] and len(set(sids)) == 2
    assert refused["isError"] is True and "run identity" in text(refused)


async def test_a_browser_call_with_no_identity_or_run_header_is_refused(upstream):
    with serving() as url:
        refused = await call(url, "browser_navigate", headers={})
    assert refused["isError"] is True and "X-Execution-Id" in text(refused)
    assert upstream.calls == []


async def test_the_project_header_is_not_a_session_key(upstream):
    """Tenancy from a header is what this repository refuses; the run header partitions."""
    with serving() as url:
        await call(url, "browser_navigate")
        await call(url, "browser_navigate", headers={**HEADERS, "X-Project-Id": "p-2"})
    assert upstream.calls[0][0] == upstream.calls[1][0]


async def test_a_run_only_sees_and_fetches_its_own_artifacts(upstream, tmp_path, monkeypatch):
    """Isolation: a run can list and fetch the files it produced, not another run's."""
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("ARTIFACTS_ROOT", str(root))
    monkeypatch.setenv("ARTIFACTS_DELETE_AFTER_PUBLISH", "false")  # keep it for both checks
    (root / "page-1.png").write_bytes(b"PNGDATA")
    upstream.saved_path = "page-1.png"  # what the relayed result names, as Playwright does
    run_a = {**HEADERS, "X-Execution-Id": "run-a"}
    run_b = {**HEADERS, "X-Execution-Id": "run-b"}
    with serving() as url:
        await call(url, "browser_take_screenshot", headers=run_a)  # run-a produces page-1.png
        listed_a = json.loads(text(await call(url, "list_artifacts", headers=run_a)))
        got_a = await call(url, "publish_artifact", {"path": "page-1.png"}, headers=run_a)
        listed_b = json.loads(text(await call(url, "list_artifacts", headers=run_b)))
        got_b = await call(url, "publish_artifact", {"path": "page-1.png"}, headers=run_b)
    assert [a["path"] for a in listed_a["artifacts"]] == ["page-1.png"]
    assert got_a.get("isError") in (None, False)
    assert listed_b["artifacts"] == []  # run-b never produced it
    assert got_b["isError"] is True and "not produced by this run" in text(got_b)


async def test_local_tools_stay_local(root, upstream):
    with serving() as url:
        result = await call(url, "list_artifacts")
    assert json.loads(text(result))["count"] == 0
    assert upstream.calls == []


async def test_upstream_down_keeps_local_tools_and_errors_browser_tools(root, upstream):
    upstream.down = True
    with serving() as url:
        assert await names(url) == LOCAL
        result = await call(url, "browser_navigate")
        assert result["isError"] is True and "not available" in text(result)
        assert (await call(url, "list_artifacts")).get("isError") in (None, False)


async def test_a_failed_fetch_backs_off_and_a_recovered_upstream_is_picked_up(upstream):
    upstream.down = True
    async with Client(module.server.mcp) as client:
        await client.list_tools()
        attempts = len(upstream.requests)
        await client.list_tools()
        assert len(upstream.requests) == attempts  # inside the backoff: no new attempt
        upstream.down = False
        module.browser._next_fetch = 0.0  # the backoff has passed
        listed = {t.name for t in (await client.list_tools()).tools}
    assert listed == LOCAL | RELAYED


async def test_unknown_names_are_errors(upstream):
    with serving() as url:
        assert "Unknown tool" in text(await call(url, "browser_fly"))
        assert (await call(url, "not_a_tool"))["isError"] is True


async def test_connection_tool_selection_applies_to_relayed_tools(upstream):
    selected = {**HEADERS, "X-MCP-Enabled-Tools": "browser_navigate"}
    with serving() as url:
        assert await names(url, selected) == {"browser_navigate"}
        assert (await call(url, "browser_close", headers=selected))["isError"] is True
        ok = await call(url, "browser_navigate", headers=selected)
        assert ok.get("isError") in (None, False)
        groups = {**HEADERS, "X-MCP-Enabled-Groups": "artifacts"}
        assert await names(url, groups) == LOCAL
        assert (await call(url, "browser_navigate", headers=groups))["isError"] is True
    assert fake_tools_called(upstream) == ["browser_navigate"]


async def test_read_only_mode_uses_the_upstream_annotations(upstream, monkeypatch):
    monkeypatch.setenv("MCP_READ_ONLY", "1")
    with serving() as url:
        assert await names(url) == {"list_artifacts", "browser_take_screenshot"}
        refused = await call(url, "browser_navigate")
        assert refused["isError"] is True and "MCP_READ_ONLY" in text(refused)
        assert (await call(url, "browser_take_screenshot")).get("isError") in (None, False)
    assert fake_tools_called(upstream) == ["browser_take_screenshot"]


async def test_the_outbound_budget_cuts_relayed_text_and_keeps_the_image(upstream):
    upstream.big_text = "x" * (module.server.budget.max_result_chars + 5_000)
    with serving() as url:
        result = await call(url, "browser_take_screenshot")
    kinds = [block["type"] for block in result["content"]]
    assert kinds == ["text", "image"]
    assert json.loads(text(result))["_truncated"] is True


async def test_shutdown_ends_every_browser_session(upstream, monkeypatch):
    # An in-process client sends no headers; local mode is the single-caller
    # case where that is allowed, which is what this test exercises.
    monkeypatch.setattr(module.server, "mode", "local")
    async with Client(module.server.mcp) as client:
        await client.call_tool("browser_navigate", {})
    assert module.browser.pool.active_keys()
    await module.browser.close()
    assert upstream.deleted == ["s1", "s2"]  # s1 was the short-lived manifest session
    assert module.browser.pool.active_keys() == []


@pytest.mark.parametrize("sse", [False, True])
async def test_json_and_event_stream_upstreams_relay_alike(upstream, sse, monkeypatch):
    monkeypatch.setattr(module.server, "mode", "local")
    upstream.sse = sse
    async with Client(module.server.mcp) as client:
        result = await client.call_tool("browser_take_screenshot", {})
    assert [block.type for block in result.content] == ["text", "image"]


async def test_live_run_guidance_is_added_to_the_relayed_descriptions(upstream):
    from connectors.playwright.gateway import described

    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    shot = tools["browser_take_screenshot"].description
    assert shot.startswith("Screenshot") and "no arguments" in shot
    assert tools["browser_navigate"].description == "Go to a URL"
    assert "after a page is open" in described("browser_video_chapter", "Add a chapter.")
    assert "no arguments" in described("browser_snapshot", None)
