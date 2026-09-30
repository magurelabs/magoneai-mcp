"""Every tool in every connector can be called, and none of them crashes inside.

This is the gate the port needed and did not have. Every defect that would
have shipped broken was invisible to static checks, because Python resolves a
name when a line RUNS, and every ported body ends in `except Exception`, so a
NameError came back as "Failed to list events" and read as an expired token:

- 25 tools whose bodies named a method left behind by the port
- Jira and Confluence: a shadowed two-argument `resolve_cloud_id`
- DocuSign, SharePoint, Outlook: token caches left behind as class attributes
- database: `time` shadowed by `datetime.time`, a stdlib logger in structlog's place
- Linear: a function-level import of a module that does not exist

So this calls every tool, over the MCP layer, with arguments generated from
its own schema and every credential header it declares. Outbound HTTP is
answered locally with an empty JSON object and DNS resolves to a public test
address, so nothing leaves the process and the vendor's shape is not what is
under test: a KeyError from an empty response is fine. What is not fine is a
crash in OUR code, so a tracer records every exception raised inside a
connector module, including the ones a handler catches and swallows, and the
test fails on the kinds that mean the code itself is broken.
"""

import asyncio
import importlib
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"
PACKAGES = sorted(p.name for p in CONNECTORS.iterdir() if (p / "server.py").exists())

PUBLIC_TEST_ADDRESS = "93.184.216.34"

# Exceptions that mean the connector's own code is broken, wherever they are
# raised and whether or not a handler swallows them. Each has shipped once.
BROKEN = (NameError, UnboundLocalError, ImportError)
BROKEN_TYPEERROR_MARKERS = ("positional argument", "keyword argument", "not callable")
BROKEN_ATTRIBUTE_MARKERS = ("module '", "has no attribute '_graph_request")


def sample(schema: dict[str, Any], name: str = "") -> Any:
    """A plausible value for a JSON schema, good enough to pass validation."""
    if not isinstance(schema, dict):
        return "x"
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    lowered = name.lower()
    if kind == "integer":
        return max(1, schema.get("minimum", 1))
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        props = schema.get("properties") or {}
        return {k: sample(v, k) for k, v in props.items() if k in (schema.get("required") or [])}
    if "email" in lowered:
        return "someone@example.com"
    if "url" in lowered or "link" in lowered or schema.get("format") == "uri":
        return "https://example.com/page"
    if "date" in lowered or "time" in lowered or schema.get("format") in ("date", "date-time"):
        return "2026-10-01T09:00:00Z"
    return "x"


def arguments_for(tool) -> dict[str, Any]:
    schema = tool.input_schema or {}
    props = schema.get("properties") or {}
    return {k: sample(v, k) for k, v in props.items() if k in (schema.get("required") or [])}


class CrashTracer:
    """Record exceptions raised inside connector modules, swallowed or not."""

    def __init__(self, roots: tuple[str, ...] | None = None) -> None:
        self.crashes: list[str] = []
        # The code under test. A downstream repository can pass its own
        # directory as well, if it registers these tools and its own together.
        self._roots = tuple(roots or (str(CONNECTORS),))

    def __call__(self, frame, event, arg):
        if event == "call":
            return self if frame.f_code.co_filename.startswith(self._roots) else None
        if event == "exception":
            exc_type, exc, _ = arg
            if self._is_broken(exc_type, exc):
                path = Path(frame.f_code.co_filename)
                where = f"{path.parent.name}/{path.name}:{frame.f_lineno}"
                self.crashes.append(f"{exc_type.__name__}: {exc} ({where})")
        return self

    @staticmethod
    def _is_broken(exc_type, exc) -> bool:
        if issubclass(exc_type, BROKEN):
            return True
        text = str(exc)
        if issubclass(exc_type, TypeError):
            return any(m in text for m in BROKEN_TYPEERROR_MARKERS)
        if issubclass(exc_type, AttributeError):
            return any(m in text for m in BROKEN_ATTRIBUTE_MARKERS)
        return False


@pytest.fixture
def isolated(monkeypatch):
    """Nothing leaves the process: HTTP answered locally, DNS answered publicly."""
    install_isolation(monkeypatch)


def install_isolation(monkeypatch) -> None:
    """The isolation itself, callable from another repository's fixture."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_TEST_ADDRESS, port or 443))]

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def headers_for(module) -> dict[str, str]:
    """Every credential header any tool in the module declares, as a dummy value."""
    import inspect

    headers: dict[str, str] = {}
    for name, value in vars(module).items():
        if name.isupper() and isinstance(value, (list, tuple)):
            for h in value:
                if isinstance(h, str) and h.lower().startswith("x-"):
                    headers[h.lower()] = "x"
    source = inspect.getsource(module)
    for chunk in source.split("credential_headers=")[1:]:
        for part in chunk.split("]", 1)[0].split(","):
            h = part.strip().strip("[]'\" \n")
            if h.lower().startswith("x-"):
                headers[h.lower()] = "x"
    return headers


@pytest.mark.parametrize("package", PACKAGES)
async def test_every_tool_is_callable_without_crashing(package, isolated, monkeypatch):
    module = importlib.import_module(f"connectors.{package}.server")
    headers = headers_for(module)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(headers))

    tracer = CrashTracer()
    problems: list[str] = []
    async with Client(module.server.mcp) as client:
        tools = (await client.list_tools()).tools
        assert tools, f"{package} registers no tools"
        for tool in tools:
            before = len(tracer.crashes)
            sys.settrace(tracer)
            try:
                result = await asyncio.wait_for(
                    client.call_tool(tool.name, arguments_for(tool)), timeout=20
                )
            except asyncio.TimeoutError:
                problems.append(f"{tool.name}: timed out")
                continue
            finally:
                sys.settrace(None)
            text = "".join(getattr(b, "text", "") for b in result.content)
            if text.strip() == f"Error executing tool {tool.name}":
                problems.append(f"{tool.name}: masked internal error")
            for crash in tracer.crashes[before:]:
                problems.append(f"{tool.name}: {crash}")

    assert not problems, "\n".join(problems)
