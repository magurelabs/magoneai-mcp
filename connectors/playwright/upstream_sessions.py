"""One upstream browser session per caller run.

Keyed by ``(identity, run)``: ``identity`` is the verified caller (the access
token's client id when the server runs with a token verifier, else empty) and
``run`` is the ``X-Execution-Id`` header the caller sets per run. So one run's
calls all land in the same browser (cookies, logins, open pages), and two
verified callers never share one even when they pick the same run id. A call
with no run id shares the caller's ``"default"`` session.

The map is bounded (least recently used goes first) and idle sessions are
ended, which makes the upstream close their browsers. Calls for one key run
one at a time; different keys run concurrently.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from .upstream_rpc import SessionGone, UpstreamError, UpstreamRpc

logger = structlog.get_logger("mcp.playwright.sessions")

DEFAULT_EXECUTION = "default"
CLOSE_TOOL = "browser_close"

SessionKey = tuple[str, str]


def session_key(identity: str | None, execution_id: str | None) -> SessionKey:
    return (identity or "", (execution_id or "").strip() or DEFAULT_EXECUTION)


@dataclass
class _Session:
    session_id: str | None
    protocol: str
    last_used: float


class SessionPool:
    def __init__(
        self,
        rpc: UpstreamRpc,
        idle_seconds: float,
        max_sessions: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rpc, self.idle_seconds, self.max_sessions, self.clock = (
            rpc,
            idle_seconds,
            max_sessions,
            clock,
        )
        self._sessions: OrderedDict[SessionKey, _Session] = OrderedDict()
        self._locks: dict[SessionKey, asyncio.Lock] = {}

    def _lock(self, key: SessionKey) -> asyncio.Lock:
        # No await between lookup and insert, so this is atomic on the event loop.
        return self._locks.setdefault(key, asyncio.Lock())

    def active_keys(self) -> list[SessionKey]:
        return list(self._sessions)

    async def _session(self, key: SessionKey) -> _Session:
        session = self._sessions.get(key)
        if session is not None:
            self._sessions.move_to_end(key)
            return session
        session_id, protocol = await self.rpc.initialize()
        session = _Session(session_id, protocol, self.clock())
        self._sessions[key] = session
        logger.info("upstream_session_opened", sessions=len(self._sessions))
        while len(self._sessions) > self.max_sessions:
            oldest, stale = self._sessions.popitem(last=False)
            logger.info("upstream_session_evicted", reason="capacity")
            await self.rpc.delete(stale.session_id, stale.protocol)
        return session

    async def call_tool(self, key: SessionKey, name: str, arguments: dict) -> dict:
        """Relay one tools/call in this key's session, re-opening it once if it is gone."""
        params = {"name": name, "arguments": arguments}
        async with self._lock(key):
            session = await self._session(key)
            session.last_used = self.clock()
            try:
                result = await self.rpc.request(
                    "tools/call", params, session.session_id, session.protocol
                )
            except SessionGone:
                self._sessions.pop(key, None)
                session = await self._session(key)
                try:
                    result = await self.rpc.request(
                        "tools/call", params, session.session_id, session.protocol
                    )
                except SessionGone as exc:
                    raise UpstreamError(str(exc)) from None
            session.last_used = self.clock()
        if name == CLOSE_TOOL and not result.get("isError"):
            await self.evict(key, reason="browser_close")
        return result

    async def evict(self, key: SessionKey, reason: str = "explicit") -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            logger.info("upstream_session_evicted", reason=reason)
            await self.rpc.delete(session.session_id, session.protocol)
        self._drop_lock(key)

    async def evict_idle(self) -> int:
        """End sessions idle longer than the limit; a key with a call in flight is skipped."""
        now = self.clock()
        stale = [k for k, s in self._sessions.items() if now - s.last_used > self.idle_seconds]
        evicted = 0
        for key in stale:
            lock = self._lock(key)
            if lock.locked():
                continue
            async with lock:
                await self.evict(key, reason="idle")
                evicted += 1
            self._drop_lock(key)
        return evicted

    def _drop_lock(self, key: SessionKey) -> None:
        lock = self._locks.get(key)
        if lock is not None and not lock.locked() and key not in self._sessions:
            self._locks.pop(key, None)

    async def close_all(self) -> None:
        for key in list(self._sessions):
            await self.evict(key, reason="shutdown")

    async def list_tools(self) -> list[dict]:
        """The upstream manifest, through a short-lived session of its own."""
        session_id, protocol = await self.rpc.initialize()
        tools: list[dict] = []
        try:
            cursor = None
            for _page in range(20):
                params = {"cursor": cursor} if cursor else {}
                result = await self.rpc.request("tools/list", params, session_id, protocol)
                tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        finally:
            await self.rpc.delete(session_id, protocol)
        return tools
