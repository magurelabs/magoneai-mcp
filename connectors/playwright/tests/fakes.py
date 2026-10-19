"""Test doubles: the Playwright MCP upstream, report payloads and media files.

`FakeUpstream` follows the Streamable HTTP contract the gateway relies on:
initialize hands out an Mcp-Session-Id, every later request must carry a live
one (400 without, 404 for an unknown one), DELETE ends it, and replies come
back as JSON or SSE. It is served through `httpx.MockTransport`, so nothing
leaves the process.
"""

import json
import struct
import zlib
from pathlib import Path

import httpx

TOOLS = [
    {
        "name": "browser_navigate",
        "description": "Go to a URL",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "browser_take_screenshot",
        "description": "Screenshot",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {"name": "browser_close", "description": "Close", "inputSchema": {"type": "object"}},
    {"name": "browser_evaluate", "description": "Run JS", "inputSchema": {"type": "object"}},
    {
        "name": "browser_run_code_unsafe",
        "description": "Run code",
        "inputSchema": {"type": "object"},
    },
    {"name": "publish_artifact", "description": "collides with a local tool"},
]
IMAGE = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}


class FakeUpstream:
    def __init__(self, sse: bool = False, down: bool = False) -> None:
        self.sse, self.down = sse, down
        self.sessions: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.deleted: list[str] = []
        self.calls: list[tuple[str, str]] = []  # (session id, tool name)
        self.big_text = ""
        self.n = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _reply(self, message: dict, headers: dict | None = None) -> httpx.Response:
        if self.sse:
            text = f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx.Response(
                200, text=text, headers={"content-type": "text/event-stream", **(headers or {})}
            )
        return httpx.Response(200, json=message, headers=headers or {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused")
        sid = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.sessions.discard(sid)
            self.deleted.append(sid)
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            self.n += 1
            new = f"s{self.n}"
            self.sessions.add(new)
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
            return self._reply(
                {"jsonrpc": "2.0", "id": body["id"], "result": result}, {"mcp-session-id": new}
            )
        if not sid:
            return httpx.Response(400, json={"error": {"message": "Server not initialized"}})
        if sid not in self.sessions:
            return httpx.Response(404, json={"error": {"message": "Session not found"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return self._reply({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": TOOLS}})
        name = body["params"]["name"]
        self.calls.append((sid, name))
        text = self.big_text or f"ran {name}"
        result = {"content": [{"type": "text", "text": text}, IMAGE]}
        return self._reply({"jsonrpc": "2.0", "id": body["id"], "result": result})


def case(cid: str, status: str = "pass", **extra) -> dict:
    base = {
        "id": cid,
        "title": f"Case {cid}",
        "category": "positive",
        "steps": ["Open the page", "Click Save"],
        "expected": "Saved",
        "actual": "Saved",
        "status": status,
    }
    return {**base, **extra}


def issue(iid: str, severity: str, **extra) -> dict:
    base = {
        "id": iid,
        "severity": severity,
        "title": f"Issue {iid}",
        "test_case_id": "TC-1",
        "steps": ["Open the page"],
        "expected": "Works",
        "actual": "Broken",
        "screenshot_paths": [],
    }
    return {**base, **extra}


def report(**extra) -> dict:
    base = {
        "title": "Checkout QA",
        "url": "https://shop.example.com/checkout",
        "tested_at": "2026-10-15T13:46:38Z",
        "environment": "staging",
        "summary": "Two flows tested.",
        "test_cases": [case("TC-1"), case("TC-2", "fail"), case("TC-3", "blocked")],
        "issues": [issue("BUG-1", "Minor"), issue("BUG-2", "Blocker"), issue("BUG-3", "Major")],
        "console_errors": ["TypeError: x is undefined"],
    }
    return {**base, **extra}


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG" + b"a" * max(0, size - 4))
    return path


def png(width: int = 40, height: int = 30) -> bytes:
    """A real PNG, which fpdf2 can draw."""
    raw = b"".join(b"\x00" + b"\x46\x78\xc8" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
