"""generate_image through a real MCP client, with the Gemini HTTP call mocked.

Two transports, because they prove different things:

- served over real HTTP (uvicorn on an ephemeral port), because the caller's
  key arrives as the `X-Gemini-Api-Key` request header and only a served app
  has request headers;
- the SDK's in-memory client for the refusal paths, which need no headers.

No network and no key: Gemini is replaced by an `httpx.MockTransport` that
records what it was sent.
"""

import base64
import contextlib
import importlib
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from mcp import Client

gemini = importlib.import_module("connectors.image_generation.gemini")
module = importlib.import_module("connectors.image_generation.server")

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 8

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


def gemini_body(data: bytes = PNG, mime: str = "image/png") -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Here is your image."},
                        {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}},
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }


@pytest.fixture
def fake_gemini(monkeypatch):
    """Replace Gemini with a recorder. Set `.response` to change the answer."""
    state = SimpleNamespace(requests=[], response=httpx.Response(200, json=gemini_body()))

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return state.response

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    # Only the connector's view of httpx is replaced, so the test's own HTTP
    # client below still reaches the served app.
    monkeypatch.setattr(
        gemini, "httpx", SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError)
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return state


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


async def rpc(url: str, method: str, params: dict, **headers: str) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**INIT_META, **params}}
    routing = {"Mcp-Method": method}
    if "name" in params:
        routing["Mcp-Name"] = str(params["name"])
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:
        response = await client.post("/mcp", headers={**MODERN, **routing, **headers}, json=body)
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise AssertionError(f"no data frame in {response.text!r}")
    return response.json()


async def call(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("generate_image", arguments)


class TestOverHttpWithTheCallersKey:
    async def test_the_tool_is_listed_with_v1s_input_schema(self, fake_gemini):
        with serving() as url:
            answer = await rpc(url, "tools/list", {})
        tools = {t["name"]: t for t in answer["result"]["tools"]}
        assert set(tools) == {"generate_image"}
        schema = tools["generate_image"]["inputSchema"]
        assert schema["required"] == ["prompt"]
        assert set(schema["properties"]) == {"prompt", "filename"}
        assert all(p.get("description") for p in schema["properties"].values())
        output = tools["generate_image"]["outputSchema"]
        assert output["required"] == ["filename", "content_type", "size_bytes"]

    async def test_the_image_comes_back_as_an_image_block(self, fake_gemini):
        with serving() as url:
            answer = await rpc(
                url,
                "tools/call",
                {"name": "generate_image", "arguments": {"prompt": "A Lighthouse at Sunset!"}},
                **{"X-Gemini-Api-Key": "caller-key"},
            )
        result = answer["result"]
        assert result.get("isError") in (None, False)
        kinds = [block["type"] for block in result["content"]]
        assert kinds == ["text", "image"]
        image = result["content"][1]
        assert image["mimeType"] == "image/png"
        assert base64.b64decode(image["data"]) == PNG

        expected = {
            "filename": "a-lighthouse-at-sunset.png",
            "content_type": "image/png",
            "size_bytes": len(PNG),
            "prompt": "A Lighthouse at Sunset!",
        }
        assert result["structuredContent"] == expected
        # The bytes travel once, in the image block, never in the JSON.
        assert json.loads(result["content"][0]["text"]) == expected

    async def test_gemini_is_called_with_the_callers_key_and_v1s_request(self, fake_gemini):
        with serving() as url:
            await rpc(
                url,
                "tools/call",
                {"name": "generate_image", "arguments": {"prompt": "a cat"}},
                **{"X-Gemini-Api-Key": "caller-key"},
            )
        (request,) = fake_gemini.requests
        assert request.method == "POST"
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{gemini.DEFAULT_MODEL}:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "caller-key"
        assert json.loads(request.content) == {"contents": [{"parts": [{"text": "a cat"}]}]}

    async def test_the_header_beats_the_environment(self, fake_gemini, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        with serving() as url:
            await rpc(
                url,
                "tools/call",
                {"name": "generate_image", "arguments": {"prompt": "a cat"}},
                **{"X-Gemini-Api-Key": "header-key"},
            )
        assert fake_gemini.requests[0].headers["x-goog-api-key"] == "header-key"


class TestInMemory:
    async def test_the_environment_key_is_the_standalone_fallback(self, fake_gemini, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        result = await call({"prompt": "a cat", "filename": "My Cat_Picture"})
        assert result.is_error is False
        assert result.structured_content["filename"] == "my-cat-picture.png"
        assert fake_gemini.requests[0].headers["x-goog-api-key"] == "env-key"

    async def test_a_jpeg_is_named_and_typed_as_one(self, fake_gemini, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        fake_gemini.response = httpx.Response(200, json=gemini_body(JPEG, "image/jpeg"))
        result = await call({"prompt": "a dog"})
        assert result.structured_content["filename"] == "a-dog.jpg"
        assert result.structured_content["content_type"] == "image/jpeg"
        assert result.content[1].mime_type == "image/jpeg"

    async def test_no_key_is_a_refusal_naming_the_key(self, fake_gemini):
        result = await call({"prompt": "a cat"})
        assert result.is_error is True
        assert "Missing Gemini API key" in result.content[0].text
        assert fake_gemini.requests == []

    @pytest.mark.parametrize(
        ("prompt", "message"),
        [("   ", "prompt is required."), ("x" * 4001, "4000 character limit")],
    )
    async def test_a_bad_prompt_is_refused_before_any_call(
        self, fake_gemini, monkeypatch, prompt, message
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        result = await call({"prompt": prompt})
        assert result.is_error is True
        assert message in result.content[0].text
        assert fake_gemini.requests == []

    async def test_a_blocked_prompt_is_a_refusal(self, fake_gemini, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        fake_gemini.response = httpx.Response(200, json={"promptFeedback": {"blockReason": "X"}})
        result = await call({"prompt": "a cat"})
        assert result.is_error is True
        assert "blocked by safety filters (X)" in result.content[0].text
        assert len(result.content) == 1

    async def test_an_upstream_error_reports_the_status_not_the_body(
        self, fake_gemini, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        fake_gemini.response = httpx.Response(403, json={"error": {"message": "key k is bad"}})
        result = await call({"prompt": "a cat"})
        assert result.is_error is True
        assert "HTTP 403" in result.content[0].text
        assert "key k is bad" not in result.content[0].text

    async def test_no_image_in_the_answer_is_a_refusal(self, fake_gemini, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        fake_gemini.response = httpx.Response(
            200, json={"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
        )
        result = await call({"prompt": "a cat"})
        assert result.is_error is True
        assert "finish_reason=SAFETY" in result.content[0].text
