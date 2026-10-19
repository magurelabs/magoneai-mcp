"""A Jira call carrying several attachments is not refused for its size.

addJiraIssueAttachment takes up to 10 base64 files per call, and base64 makes
each a third bigger. Under the framework's 10 MiB default any call whose files
added up to more than about 7.5 MB was answered 413 before the tool saw it.
v1 ran this connector at 70 MB; this one declares the same default.

Over a real socket, because the cap is enforced by middleware in front of the
MCP endpoint, which an in-process call never passes through.
"""

import contextlib
import importlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from magone_mcp_core.transport import DEFAULT_MAX_BODY_BYTES

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.atlassian_jira.server")

MIB = 1024 * 1024


@contextlib.contextmanager
def serving(app):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
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


def attachment_call(payload_bytes: int) -> bytes:
    """addJiraIssueAttachment with `payload_bytes` of base64, and no credentials."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
                "name": "addJiraIssueAttachment",
                "arguments": {
                    "issueIdOrKey": "PROJ-1",
                    "fileName": "big.bin",
                    "fileContentBase64": "QUFB" * (payload_bytes // 4),
                },
            },
        }
    ).encode()


def test_jira_declares_v1s_seventy_mebibytes():
    assert module.MAX_REQUEST_BODY_BYTES == 70 * MIB
    assert module.server.max_body_bytes() == 70 * MIB


async def test_a_twelve_mebibyte_attachment_call_reaches_the_tool(monkeypatch):
    monkeypatch.delenv("MCP_MAX_BODY_BYTES", raising=False)
    body = attachment_call(12 * MIB)
    assert len(body) > DEFAULT_MAX_BODY_BYTES

    with serving(module.server.app()) as url:
        async with httpx.AsyncClient(base_url=url, timeout=60) as client:
            response = await client.post(
                "/mcp",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "addJiraIssueAttachment",
                },
            )

    # Past the size check: the tool ran and stopped at the missing credentials.
    assert response.status_code == 200, response.text[:300]
    assert "too large" not in response.text.lower()
