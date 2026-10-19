"""A tool call carrying a base64 attachment must not be refused for its size.

v1 accepted request bodies up to 10 MB. The SDK's default is 4 MiB, and `app()`
left it there, so a call carrying a 3.5 MB file, which base64 turns into a body
of about 4.7 MB, was answered `413 Request body too large` before any tool saw
it. Every attachment tool that worked on v1 had a size band in which it now
failed, and nothing in the suite sent a body big enough to notice.

Over a real socket, because the limit is enforced by an ASGI middleware in
front of the MCP endpoint: an in-process call to the tool never passes through
it, which is how the regression stayed invisible.
"""

import json

import httpx
import pytest
from mcp.types import ToolAnnotations
from test_custom_route import serving

from magone_mcp_core import ConnectorServer
from magone_mcp_core.transport import DEFAULT_MAX_BODY_BYTES, max_body_bytes

MIB = 1024 * 1024

MODERN = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/call",
    "Mcp-Name": "measure",
}


def build(env: dict[str, str], max_body: int | None = None) -> ConnectorServer:
    server = ConnectorServer(
        name="body-limit-test", version="0.0.0", env=env, max_body_bytes=max_body
    )

    @server.tool(
        name="measure",
        description="Return the length of the payload it was given.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def measure(payload: str) -> dict:
        """Echo the size back, so the test proves the body reached the tool."""
        return {"length": len(payload)}

    return server


def call_body(payload_chars: int) -> bytes:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            "name": "measure",
            "arguments": {"payload": "A" * payload_chars},
        },
    }
    return json.dumps(body).encode()


async def post(url: str, body: bytes) -> httpx.Response:
    async with httpx.AsyncClient(base_url=url, timeout=60) as client:
        return await client.post("/mcp", headers=MODERN, content=body)


def result_of(response: httpx.Response) -> dict:
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:]
            break
    return json.loads(text)["result"]


def test_the_default_is_v1s_ten_mebibytes():
    assert DEFAULT_MAX_BODY_BYTES == 10 * MIB
    assert max_body_bytes({}) == 10 * MIB


@pytest.mark.parametrize("raw", ["", "  ", "ten", "0", "-1", "1.5"])
def test_an_unusable_setting_falls_back_to_the_default(raw):
    """A typo in a size must not stop the server, and must not mean zero."""
    assert max_body_bytes({"MCP_MAX_BODY_BYTES": raw}) == DEFAULT_MAX_BODY_BYTES


def test_the_setting_moves_the_limit():
    assert max_body_bytes({"MCP_MAX_BODY_BYTES": " 20971520 "}) == 20 * MIB


async def test_a_six_mebibyte_call_reaches_the_tool():
    """Over the SDK's 4 MiB default, under v1's 10 MB: the band that broke."""
    payload = 6 * MIB
    body = call_body(payload)
    assert 4 * MIB < len(body) < DEFAULT_MAX_BODY_BYTES

    with serving(build({})) as url:
        response = await post(url, body)

    assert response.status_code == 200, response.text[:200]
    result = result_of(response)
    assert result.get("isError") is not True, result
    assert json.loads(result["content"][0]["text"])["length"] == payload


async def test_a_body_over_the_limit_is_still_refused():
    """Raising the cap must not remove it: the SDK buffers the whole body."""
    body = call_body(DEFAULT_MAX_BODY_BYTES)
    assert len(body) > DEFAULT_MAX_BODY_BYTES

    with serving(build({})) as url:
        response = await post(url, body)

    assert response.status_code == 413


async def test_a_deployment_can_lower_the_limit():
    """The override is honoured by the served app, not only by the parser."""
    body = call_body(2 * MIB)

    with serving(build({"MCP_MAX_BODY_BYTES": str(1 * MIB)})) as url:
        response = await post(url, body)

    assert response.status_code == 413


# -- a connector's own default ------------------------------------------------
#
# v1 let a connector raise its cap (Jira ran at 70 MB, Outlook at 75 MB) because
# a call carrying several base64 attachments is legitimately tens of megabytes.
# The port had one process-wide default and nothing per connector, so those
# calls were refused with 413 before any tool ran. Precedence, lowest first:
# framework default, connector default, MCP_MAX_BODY_BYTES.


def test_a_connector_default_replaces_the_framework_default():
    assert max_body_bytes({}, 70 * MIB) == 70 * MIB
    assert build({}, max_body=70 * MIB).max_body_bytes() == 70 * MIB


def test_the_environment_still_wins_over_a_connector_default():
    server = build({"MCP_MAX_BODY_BYTES": str(5 * MIB)}, max_body=70 * MIB)
    assert server.max_body_bytes() == 5 * MIB


@pytest.mark.parametrize("raw", ["", "ten", "0", "-1"])
def test_an_unusable_setting_falls_back_to_the_connector_default(raw):
    assert build({"MCP_MAX_BODY_BYTES": raw}, max_body=70 * MIB).max_body_bytes() == 70 * MIB


@pytest.mark.parametrize("bad", [0, -1, 1.5, "70", True])
def test_a_nonsense_connector_default_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        ConnectorServer(name="x", version="0", env={}, max_body_bytes=bad)


async def test_a_raised_connector_default_lets_a_large_call_through():
    """Over the framework's 10 MiB, under the connector's 16 MiB: reaches the tool."""
    payload = 12 * MIB
    body = call_body(payload)
    assert DEFAULT_MAX_BODY_BYTES < len(body) < 16 * MIB

    with serving(build({}, max_body=16 * MIB)) as url:
        response = await post(url, body)

    assert response.status_code == 200, response.text[:200]
    assert json.loads(result_of(response)["content"][0]["text"])["length"] == payload


async def test_a_raised_connector_default_is_still_a_limit():
    body = call_body(3 * MIB)

    with serving(build({}, max_body=2 * MIB)) as url:
        response = await post(url, body)

    assert response.status_code == 413


async def test_the_environment_can_lower_a_raised_connector_default():
    body = call_body(12 * MIB)

    with serving(build({"MCP_MAX_BODY_BYTES": str(1 * MIB)}, max_body=16 * MIB)) as url:
        response = await post(url, body)

    assert response.status_code == 413
