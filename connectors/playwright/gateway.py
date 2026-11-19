"""Relay the official Playwright MCP server's browser tools through this connector.

The upstream's ``tools/list`` is fetched lazily (on the first ``tools/list``
or browser call, then again every ``PLAYWRIGHT_MANIFEST_REFRESH_MINUTES``) and
each tool is registered in the SDK's tool table as a ``RelayedTool``, with the
upstream's own name, description, input schema and annotations. So relayed
tools show up in ``tools/list`` beside the local ones and are subject to the
same framework controls: ``MCP_READ_ONLY`` (on the upstream's own
``readOnlyHint``), per-connection tool selection (group ``browser``), the
outbound text budget and the untrusted-content envelope around every text item.
Screenshots come back as image content, up to ``ARTIFACTS_MAX_BYTES`` per
result; a larger one is replaced by a short note (``cap_media``).

Each run's artifact scope (``artifact_scope.py``) records only the files its
own calls reported writing and that were written during that call, so the
local tools never serve one run another run's files.

While the upstream is unreachable the local tools keep working, ``tools/list``
lists only them, and a browser tool answers "not available right now". Failed
fetches back off (5 seconds, doubling to a minute) so repeated listings cannot
hammer a dead upstream. ``/healthz`` never triggers a fetch: the framework
counts the registered tools without running the before-list hook.

Tools that run arbitrary page or Playwright code are hidden and refused unless
``PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS`` is on.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import structlog
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from pydantic import ValidationError

from magone_mcp_core import ConnectorServer, request_header

from . import config
from .allowlist import ALLOWED_NAMES, schema_matches_snapshot
from .artifact_scope import RUN_UNKNOWN, ArtifactScope, Produced
from .paths import ArtifactError, reported_paths
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
# A file counts as written by a call when its mtime is no older than the call's
# start less this much: room for filesystems that keep whole-second mtimes.
MTIME_ALLOWANCE_NS = 2_000_000_000

# Appended to the upstream's own description. For browser_take_screenshot (0.0.85)
# no arguments captures the viewport; fullPage: true captures the whole scrollable
# page. browser_snapshot is the one to use before acting on a page.
GUIDANCE = {
    "browser_take_screenshot": (
        " With no arguments it captures the viewport; pass fullPage: true for the "
        "whole scrollable page. Use browser_snapshot before acting on a page."
    ),
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
    max_sessions = int(_number("PLAYWRIGHT_MAX_SESSIONS", 64))
    # None lets SessionPool pick its default (a quarter of the pool); an explicit
    # value overrides it.
    per_caller = None
    if os.getenv("PLAYWRIGHT_MAX_SESSIONS_PER_CALLER"):
        per_caller = int(_number("PLAYWRIGHT_MAX_SESSIONS_PER_CALLER", max(4, max_sessions // 4)))
    return {
        "upstream_url": (os.getenv("PLAYWRIGHT_UPSTREAM_URL") or DEFAULT_UPSTREAM_URL).strip(),
        "idle_seconds": _number("PLAYWRIGHT_SESSION_IDLE_MINUTES", 15) * 60,
        "max_sessions": max_sessions,
        "max_per_caller": per_caller,
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


def request_run(context: Any) -> tuple[str, str]:
    """The verified identity and the run header for this request, each possibly empty."""
    return caller_identity(), (request_header(context, RUN_HEADER) or "").strip()


def request_session_key(context: Any) -> SessionKey:
    identity, execution_id = request_run(context)
    return session_key(identity, execution_id)


def _annotations(raw: Any) -> ToolAnnotations | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ToolAnnotations.model_validate(raw)
    except ValidationError:
        return None


def _encoded_bytes(item: Any) -> int:
    """Decoded size of a binary content item (image, audio, blob resource), else 0."""
    if isinstance(item, (ImageContent, AudioContent)):
        data = item.data
    elif isinstance(item, EmbeddedResource) and isinstance(item.resource, BlobResourceContents):
        data = item.resource.blob
    else:
        return 0
    return len(data or "") * 3 // 4


def cap_media(result: CallToolResult, limit: int) -> CallToolResult:
    """Keep a relayed result's binary items under ``limit`` bytes in total.

    The outbound budget measures text only, so without this a full-page
    screenshot of a very long page would go back however big it is. An item
    that would take the total over the limit is replaced by a note naming its
    size and what to do instead; text items and smaller binaries are kept.
    """
    total, content, replaced = 0, [], 0
    for item in result.content:
        size = _encoded_bytes(item)
        if size and total + size > limit:
            replaced += 1
            content.append(
                TextContent(
                    type="text",
                    text=(
                        f"[A {item.type} of {size:,} bytes was left out of this result: it is "
                        f"over the {limit:,} byte limit (ARTIFACTS_MAX_BYTES). Take a smaller "
                        "screenshot (the viewport rather than fullPage, or a lower scale), or "
                        "fetch a saved file with publish_artifact.]"
                    ),
                )
            )
            continue
        total += size
        content.append(item)
    if not replaced:
        return result
    logger.warning("relayed_media_over_limit", items=replaced, limit=limit)
    return result.model_copy(update={"content": content})


def produced_files(paths: list[str], since_ns: int) -> list[Produced]:
    """The reported paths that are real returnable files written since ``since_ns``."""
    found: list[Produced] = []
    for real in paths:
        try:
            info = os.stat(real)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_mtime_ns < since_ns:
            continue
        if config.content_type_for(Path(real)) is None:
            continue
        found.append(Produced.of(real, info))
    return found


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
        rpc = UpstreamRpc(
            options["upstream_url"],
            options["call_timeout"],
            transport,
            client_name=server.name,
            client_version=server.version,
        )
        self.pool = SessionPool(
            rpc,
            options["idle_seconds"],
            options["max_sessions"],
            clock,
            max_per_caller=options.get("max_per_caller"),
        )
        self.local_names = server.tool_names()
        self.upstream: dict[str, dict] = {}
        # Per caller run, the files its own calls reported writing and wrote
        # during the call. The local tools scope to this set.
        self.scope = ArtifactScope(options["idle_seconds"], clock)
        self._next_fetch = 0.0
        self._backoff = FIRST_RETRY_SECONDS
        self._sweeper: asyncio.Task | None = None

    # -- manifest ----------------------------------------------------------

    def _exposed(self, tool: Any) -> bool:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name or name in self.local_names:
            return False
        # The code-running tools are an explicit, snapshot-free opt-in.
        if name in UNSAFE_TOOLS:
            return self.expose_unsafe
        # Everything else is relayed only if it is on the reviewed allowlist AND
        # its schema still matches the checked-in snapshot. A tool nobody has
        # looked at, or one whose schema an upstream upgrade changed, is withheld.
        if name not in ALLOWED_NAMES:
            return False
        if not schema_matches_snapshot(name, tool):
            logger.warning("upstream_tool_schema_drift", tool=name)
            return False
        return True

    def _forget(self, name: str) -> None:
        self.server.unregister_relayed_tool(name)

    def _register(self, name: str, tool: dict) -> None:
        schema = tool.get("inputSchema")

        async def handler(arguments: dict, context: Any, _name: str = name) -> Any:
            return await self._relay(_name, arguments, context)

        self.server.register_relayed_tool(
            name,
            description=described(name, tool.get("description")),
            input_schema=schema if isinstance(schema, dict) else {"type": "object"},
            annotations=_annotations(tool.get("annotations")),
            permission_group=BROWSER_GROUP,
            required_scopes=[],
            handler=handler,
            title=tool.get("title") if isinstance(tool.get("title"), str) else None,
        )

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

    async def _relay(self, name: str, arguments: dict, context: Any) -> CallToolResult:
        """The upstream's result for one call. Read-only, selection, the budget and
        the untrusted-content envelope are applied by ``ConnectorServer._run_relayed``
        around this; here is the browser-specific part: run identity, the session,
        the media size cap and recording which files this run produced.

        Refusals are raised as ``ToolError``: they are this connector's words, not
        the upstream's, so they are not wrapped as untrusted content.
        """
        identity, execution_id = request_run(context)
        if not identity and not execution_id and self.server.mode != "local":
            # In platform or hosted mode several callers reach one connector, so
            # without either a verified identity or a run header every anonymous
            # caller would land in one shared "default" browser: same cookies,
            # same logins. Refuse rather than silently co-mingle runs. `local`
            # mode is a single user on loopback, where one default session is fine.
            raise ToolError(
                "This browser tool needs a run identity so your calls get their own "
                "isolated browser session. Send an X-Execution-Id header (any id unique "
                "to your run), or run this connector behind a token verifier. Without "
                "one, different callers would share a single browser, its cookies and "
                "its open logins."
            )
        self._ensure_sweeper()
        logger.info("tool_relayed", tool=name, argument_keys=sorted(arguments))
        key = session_key(identity, execution_id)
        self.scope.open(key)
        started_ns = time.time_ns()
        try:
            result = await self.pool.call_tool(key, name, arguments)
        except UpstreamError as exc:
            logger.warning("upstream_call_failed", tool=name, reason=str(exc)[:200])
            raise ToolError(str(exc)) from None
        if not isinstance(result.get("content"), list):
            raise ToolError("The browser service returned no content.")
        try:
            shaped = CallToolResult.model_validate(result)
        except ValidationError:
            raise ToolError(
                "The browser service returned a result this server cannot read."
            ) from None
        self._record_artifacts(key, name, shaped, started_ns - MTIME_ALLOWANCE_NS)
        return cap_media(shaped, config.max_bytes())

    def _record_artifacts(
        self, key: SessionKey, tool: str, result: CallToolResult, since_ns: int
    ) -> None:
        """Record the files this call reported writing, so only this run may fetch them.

        Authoritative signals only: a path counts when Playwright's own report
        lines name it (``reported_paths``: the ``### Result`` / ``### Snapshot``
        links, never page text, and never from a tool whose reply echoes the
        page, such as console messages), the file exists, and it was written during this
        call. A name a page puts in its URL, title or text is never recorded, and
        neither is a guessed name of a file someone else wrote earlier.
        """
        root = config.artifacts_root()
        paths = [
            path
            for block in result.content
            if isinstance(block, TextContent) and block.text
            for path in reported_paths(block.text, root, tool)
        ]
        produced = produced_files(paths, since_ns) if paths else []
        if len(produced) < len(paths):
            logger.info("artifact_report_unverified", reported=len(paths), kept=len(produced))
        if produced:
            self.scope.record(key, produced)

    def allowed_paths(self, context: Any) -> set[str] | None:
        """The artifact paths the caller may read/list/delete, or None for no restriction.

        None in ``local`` mode (a single caller on loopback). Otherwise the files
        this caller's run produced that are still as recorded: a run that has
        produced nothing, or an anonymous caller with no run, gets an empty set.
        """
        if self.server.mode == "local":
            return None
        key = request_session_key(context)
        self.scope.touch(key)
        return self.scope.allowed(key)

    def require_known_run(self, context: Any) -> None:
        """Refuse, with the reason, a run this process holds no record for.

        Outside ``local`` mode only. Without this, a run whose record a restart,
        a different replica or idle expiry lost would see an empty listing and
        "not produced by this run", which reads as if its files never existed.
        """
        if self.server.mode == "local":
            return
        if not self.scope.known(request_session_key(context)):
            raise ArtifactError(RUN_UNKNOWN)

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
                self.cleanup_expired_runs()
            except Exception as exc:  # noqa: BLE001 - the sweeper must outlive one bad tick
                logger.error("idle_sweep_failed", error_type=type(exc).__name__)

    def cleanup_expired_runs(self) -> int:
        """Forget runs idle past the limit and delete the files they never fetched.

        Once a run's record is gone nobody can fetch its files, so they would
        only fill the shared volume. Kept when ``ARTIFACTS_DELETE_AFTER_PUBLISH``
        is off (the operator wants files left on the volume) and in ``local``
        mode (a laptop user's own output directory). A file rewritten since it
        was recorded is not this run's any more and is left alone.
        """
        left = self.scope.expire()
        if not left or self.server.mode == "local" or not config.delete_after_publish():
            return 0
        removed = 0
        for item in left:
            if not item.still_current():
                continue
            try:
                os.unlink(item.real_path)
                removed += 1
            except OSError as exc:
                logger.warning("expired_artifact_delete_failed", error=type(exc).__name__)
        if removed:
            logger.info("expired_artifacts_deleted", count=removed)
        return removed

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        await self.pool.close_all()


def install(
    server: ConnectorServer,
    transport: httpx.AsyncBaseTransport | None = None,
    options: dict | None = None,
) -> Gateway:
    """Attach a gateway to ``server``: lazy manifest, call checks, shutdown.

    Uses the framework's public relayed-tool seams (``before_list_tools``,
    ``before_call_tool`` and ``register_relayed_tool``), so a relayed call gets
    the same read-only, selection and budget guarantees as a local tool and the
    connector reaches into no SDK or framework internals of its own.
    """
    gateway = Gateway(server, options or settings(), transport)
    server.before_list_tools(gateway.refresh)
    server.before_call_tool(gateway.before_call)
    server.on_shutdown(gateway.close)
    return gateway
