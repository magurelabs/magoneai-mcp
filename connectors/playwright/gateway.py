"""Relay the official Playwright MCP server's browser tools through this connector.

The upstream's ``tools/list`` is fetched lazily (on the first ``tools/list``
or browser call, then again every ``PLAYWRIGHT_MANIFEST_REFRESH_MINUTES``) and
each tool is registered in the SDK's tool table as a ``RelayedTool``, with the
upstream's own name, description, input schema and annotations. So relayed
tools show up in ``tools/list`` beside the local ones and are subject to the
same framework controls: ``MCP_READ_ONLY`` (on the upstream's own
``readOnlyHint``), per-connection tool selection (group ``browser``) and the
outbound text budget. A relayed call returns the upstream's content items
untouched otherwise, screenshots included as image content.

While the upstream is unreachable the local tools keep working, ``tools/list``
lists only them, and a browser tool answers "not available right now". Failed
fetches back off (5 seconds, doubling to a minute) so a health probe cannot
hammer a dead upstream.

Tools that run arbitrary page or Playwright code are hidden and refused unless
``PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS`` is on.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import structlog
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool as SDKTool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

import magone_mcp_core.server as core_server
from magone_mcp_core import ConnectorServer, assert_enabled, budget_call_result
from magone_mcp_core.readonly import assert_callable

from .upstream_rpc import UpstreamError, UpstreamRpc
from .upstream_sessions import SessionKey, SessionPool, session_key

logger = structlog.get_logger("mcp.playwright.gateway")

DEFAULT_UPSTREAM_URL = "http://playwright:8931/mcp"
UNSAFE_TOOLS = frozenset({"browser_evaluate", "browser_run_code", "browser_run_code_unsafe"})
BROWSER_GROUP = "browser"
BROWSER_PREFIX = "browser_"
RUN_HEADER = "x-execution-id"
SWEEP_SECONDS = 60.0
FIRST_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0
_TRUE = {"1", "true", "yes", "on"}

# Appended to the upstream's own description, learned from live runs: with
# `boxes`, `depth` or `target` these two stalled the page, and a chapter added
# before any page was open failed the recording.
_NO_ARGUMENTS = " Call it with no arguments: boxes, depth and target can stall the page."
GUIDANCE = {
    "browser_snapshot": _NO_ARGUMENTS,
    "browser_take_screenshot": _NO_ARGUMENTS,
    "browser_video_chapter": " Add a chapter only after a page is open.",
}


def described(name: str, upstream: object) -> str:
    """The upstream description plus this connector's guidance for the tool, if any."""
    return (str(upstream or "") + GUIDANCE.get(name, "")).strip()


def _number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def settings() -> dict:
    """Gateway settings from the environment."""
    return {
        "upstream_url": (os.getenv("PLAYWRIGHT_UPSTREAM_URL") or DEFAULT_UPSTREAM_URL).strip(),
        "idle_seconds": _number("PLAYWRIGHT_SESSION_IDLE_MINUTES", 15) * 60,
        "max_sessions": int(_number("PLAYWRIGHT_MAX_SESSIONS", 64)),
        "call_timeout": _number("PLAYWRIGHT_CALL_TIMEOUT_SECONDS", 100),
        "refresh_seconds": _number("PLAYWRIGHT_MANIFEST_REFRESH_MINUTES", 10) * 60,
        "expose_unsafe": (os.getenv("PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS") or "").lower() in _TRUE,
    }


def caller_identity() -> str:
    """The verified caller, when a token verifier authenticated this request."""
    token = get_access_token()
    if token is None:
        return ""
    return "|".join(part or "" for part in principal_components(token))


def request_session_key(context: Any) -> SessionKey:
    headers = {str(k).lower(): v for k, v in core_server._request_headers(context).items()}
    return session_key(caller_identity(), headers.get(RUN_HEADER))


def _placeholder() -> None:
    """Never called: a RelayedTool answers in ``run``."""


class RelayedTool(SDKTool):
    """An upstream tool in the SDK's tool table, answered by the gateway."""

    gateway: Any = Field(default=None, exclude=True)

    async def run(self, arguments: dict, context: Any, convert_result: bool = False) -> Any:
        return await self.gateway.relay(self, arguments, context)


def _annotations(raw: Any) -> ToolAnnotations | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ToolAnnotations.model_validate(raw)
    except ValidationError:
        return None


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


class Gateway:
    def __init__(
        self,
        server: ConnectorServer,
        options: dict,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.server, self.clock = server, clock
        self.expose_unsafe = bool(options["expose_unsafe"])
        self.refresh_seconds = float(options["refresh_seconds"])
        rpc = UpstreamRpc(options["upstream_url"], options["call_timeout"], transport)
        self.pool = SessionPool(rpc, options["idle_seconds"], options["max_sessions"], clock)
        self.local_names = frozenset(server.mcp._tool_manager._tools)
        self.upstream: dict[str, dict] = {}
        self._next_fetch = 0.0
        self._backoff = FIRST_RETRY_SECONDS
        self._sweeper: asyncio.Task | None = None

    # -- manifest ----------------------------------------------------------

    def _exposed(self, tool: Any) -> bool:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name or name in self.local_names:
            return False
        return self.expose_unsafe or name not in UNSAFE_TOOLS

    def _forget(self, name: str) -> None:
        self.server.mcp._tool_manager._tools.pop(name, None)
        self.server._annotations.pop(name, None)
        self.server._permission_groups.pop(name, None)
        self.server._required_scopes.pop(name, None)

    def _register(self, name: str, tool: dict) -> None:
        annotations = _annotations(tool.get("annotations"))
        relayed = RelayedTool.from_function(
            _placeholder,
            name=name,
            description=described(name, tool.get("description")),
            title=tool.get("title") if isinstance(tool.get("title"), str) else None,
            annotations=annotations,
            structured_output=False,
        )
        schema = tool.get("inputSchema")
        relayed.parameters = schema if isinstance(schema, dict) else {"type": "object"}
        relayed.meta = {
            "io.magone/permission_group": BROWSER_GROUP,
            "io.magone/required_scopes": [],
        }
        relayed.gateway = self
        self.server.mcp._tool_manager._tools[name] = relayed
        self.server._annotations[name] = annotations
        self.server._permission_groups[name] = BROWSER_GROUP
        self.server._required_scopes[name] = []

    def apply_manifest(self, tools: list) -> bool:
        """Register the upstream tools; True when the exposed set changed."""
        fresh = {t["name"]: t for t in tools if self._exposed(t)}
        if fresh == self.upstream:
            return False
        for name in self.upstream:
            self._forget(name)
        for name, tool in fresh.items():
            self._register(name, tool)
        self.upstream = fresh
        logger.info("upstream_manifest_applied", tool_count=len(fresh))
        return True

    async def refresh(self, force: bool = False) -> bool:
        """Fetch the manifest when due. True when it was fetched; never raises."""
        now = self.clock()
        if not force and now < self._next_fetch:
            return False
        try:
            self.apply_manifest(await self.pool.list_tools())
        except UpstreamError as exc:
            logger.warning("upstream_manifest_unavailable", reason=str(exc)[:200])
            self._next_fetch = now + self._backoff
            self._backoff = min(self._backoff * 2, MAX_RETRY_SECONDS)
            return False
        self._next_fetch = now + self.refresh_seconds
        self._backoff = FIRST_RETRY_SECONDS
        return True

    # -- calls -------------------------------------------------------------

    def handles(self, name: str) -> bool:
        """A name this gateway answers rather than the SDK's unknown-tool error."""
        if name in self.local_names:
            return False
        return name in self.upstream or name in UNSAFE_TOOLS or name.startswith(BROWSER_PREFIX)

    async def before_call(self, name: str) -> None:
        """Refuse an unsafe tool; load the manifest for a browser tool not yet known."""
        if name in UNSAFE_TOOLS and not self.expose_unsafe:
            raise ToolError(f"{name} is disabled on this server.")
        if not self.handles(name) or name in self.upstream:
            return
        await self.refresh(force=not self.upstream)
        if not self.upstream:
            raise ToolError(
                "The Playwright browser service is not available right now. Try again shortly."
            )

    async def relay(self, tool: RelayedTool, arguments: dict, context: Any) -> CallToolResult:
        """The upstream's result for one call, after the framework's own checks."""
        headers = core_server._request_headers(context)
        if self.server._read_only():
            assert_callable(tool.name, tool.annotations, {"MCP_READ_ONLY": "1"})
        assert_enabled(tool.name, headers, self.server._permission_groups)
        self._ensure_sweeper()
        logger.info("tool_relayed", tool=tool.name, argument_keys=sorted(arguments))
        try:
            result = await self.pool.call_tool(request_session_key(context), tool.name, arguments)
        except UpstreamError as exc:
            logger.warning("upstream_call_failed", tool=tool.name, reason=str(exc)[:200])
            return _error(str(exc))
        if not isinstance(result.get("content"), list):
            return _error("The browser service returned no content.")
        try:
            shaped = CallToolResult.model_validate(result)
        except ValidationError:
            return _error("The browser service returned a result this server cannot read.")
        return budget_call_result(shaped, self.server.budget)

    # -- lifecycle -----------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        """Start the idle sweeper in the running loop, once."""
        loop = asyncio.get_running_loop()
        task = self._sweeper
        if task is not None and not task.done() and task.get_loop() is loop:
            return
        self._sweeper = loop.create_task(self._sweep())

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self.pool.evict_idle()
            except Exception as exc:  # noqa: BLE001 - the sweeper must outlive one bad tick
                logger.error("idle_sweep_failed", error_type=type(exc).__name__)

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        await self.pool.close_all()


def install(
    server: ConnectorServer,
    transport: httpx.AsyncBaseTransport | None = None,
    options: dict | None = None,
) -> Gateway:
    """Attach a gateway to ``server``: lazy manifest, call checks, shutdown."""
    gateway = Gateway(server, options or settings(), transport)
    list_tools = server.mcp.list_tools
    call_tool = server.mcp.call_tool

    async def list_with_upstream() -> list:
        await gateway.refresh()
        return await list_tools()

    async def call_with_upstream(name: str, arguments: dict, context: Any = None) -> Any:
        await gateway.before_call(name)
        return await call_tool(name, arguments, context)

    server.mcp.list_tools = list_with_upstream  # type: ignore[method-assign]
    server.mcp.call_tool = call_with_upstream  # type: ignore[method-assign]
    server.on_shutdown(gateway.close)
    return gateway
