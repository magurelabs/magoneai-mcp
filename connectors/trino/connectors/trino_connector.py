"""Trino connector using the trino Python client (sync, wrapped in asyncio.to_thread).

The client is DB-API and therefore blocking, so every statement runs on a worker
thread. Connections are cached per credential set: the underlying HTTP session
pools its sockets, so re-creating one per tool call means a fresh TCP (and TLS)
handshake for every query. A cached connection is guarded by its own lock, so
one thread uses it at a time while different credential sets stay concurrent.
"""

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .._shared.sql_safety import quote_identifier, quote_qualified_name, serialize_row

try:
    import trino
    from trino.dbapi import connect as trino_connect
except ImportError:
    trino = None
    trino_connect = None

DEFAULT_QUERY_TIMEOUT_SECONDS = 60
MAX_QUERY_TIMEOUT_SECONDS = 300

# Connection cache bounds, mirroring connectors/database/connectors/pool.py.
_MAX_CACHED_CONNECTIONS = 20
_CONNECTION_TTL_SECONDS = 300

# key -> (connection, lock guarding it, last-used monotonic timestamp)
_connections: Dict[Tuple, Tuple[Any, threading.Lock, float]] = {}
_cache_lock = threading.Lock()


class TrinoQueryTimeout(Exception):
    """Raised when a statement exceeds its timeout and is cancelled server-side."""


def _dedupe_columns(names: List[str]) -> Tuple[List[str], List[str]]:
    """Make column names unique, preserving order.

    ``SELECT c.id, o.id`` yields two columns both named ``id``. Zipping those
    into a dict drops the first silently, which loses a column from every
    multi-table join — the common case for a federated query engine. Repeats get
    a ``_1``, ``_2`` … suffix instead, and the renamed ones are reported back so
    the caller can say what happened.
    """
    seen: Dict[str, int] = {}
    out: List[str] = []
    renamed: List[str] = []
    for name in names:
        if name not in seen:
            seen[name] = 0
            out.append(name)
            continue
        seen[name] += 1
        candidate = f"{name}_{seen[name]}"
        while candidate in seen:
            seen[name] += 1
            candidate = f"{name}_{seen[name]}"
        seen[candidate] = 0
        out.append(candidate)
        renamed.append(candidate)
    return out, renamed


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - a failed close must not mask the real error
        pass


class TrinoConnector:
    """Connection and query surface for a single Trino cluster."""

    def __init__(self) -> None:
        self._conn = None
        self._lock: Optional[threading.Lock] = None
        self._key: Optional[Tuple] = None
        self._active_cursor = None
        self._catalog: Optional[str] = None
        self._schema: Optional[str] = None

    # -- connection ---------------------------------------------------------

    def connect(self, credentials: Dict[str, str]) -> None:
        if trino_connect is None:
            raise ImportError("trino package is required. Install with: pip install trino")

        host = (credentials.get("trino_host") or "").strip()
        if not host:
            raise ValueError("trino_host is required")

        raw_port = (credentials.get("trino_port") or "8080").strip()
        try:
            port = int(raw_port)
        except ValueError:
            raise ValueError(f"trino_port must be a number, got {raw_port!r}") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"trino_port must be between 1 and 65535, got {port}")

        user = (credentials.get("trino_user") or "admin").strip()
        password = (credentials.get("trino_password") or "").strip() or None
        self._catalog = (credentials.get("trino_catalog") or "").strip() or None
        self._schema = (credentials.get("trino_schema") or "").strip() or None
        http_scheme = (credentials.get("trino_http_scheme") or "http").strip()

        self._key = (host, port, user, password, self._catalog, self._schema, http_scheme)
        self._conn, self._lock = _get_or_create(
            self._key,
            lambda: _new_connection(
                host, port, user, password, self._catalog, self._schema, http_scheme
            ),
        )

    def close(self) -> None:
        """Release the connection back to the cache.

        Deliberately not a teardown: the connection is shared, and the cache
        evicts by TTL and capacity. Callers keep their ``finally: close()``.
        """
        self._conn = None
        self._lock = None
        self._key = None
        self._active_cursor = None

    # -- execution ----------------------------------------------------------

    def _run_query(self, sql: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        assert self._conn is not None and self._lock is not None
        with self._lock:
            cursor = self._conn.cursor()
            self._active_cursor = cursor
            try:
                cursor.execute(sql)
                raw_columns = [d[0] for d in cursor.description] if cursor.description else []
                columns, renamed = _dedupe_columns(raw_columns)
                rows = cursor.fetchall()
            finally:
                self._active_cursor = None
        return [serialize_row(dict(zip(columns, row))) for row in rows], renamed

    def _cancel_active(self) -> None:
        cursor = self._active_cursor
        if cursor is None:
            return
        try:
            cursor.cancel()
        except Exception:  # noqa: BLE001 - best-effort; the query may have finished
            pass

    async def _execute(self, sql: str, timeout: float) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Run ``sql`` with a wall-clock timeout, cancelling it server-side on expiry.

        ``asyncio.wait_for`` abandons the worker thread but cannot stop it, and
        Trino would keep computing a query nobody is waiting for. Cancelling the
        cursor tells the cluster to stop and unblocks the thread.
        """
        task = asyncio.create_task(asyncio.to_thread(self._run_query, sql))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            self._cancel_active()
            task.cancel()
            raise TrinoQueryTimeout(
                f"Query exceeded the {int(timeout)}s timeout and was cancelled. "
                "Narrow the query with a WHERE clause or fewer joins."
            ) from None

    async def query(
        self, sql: str, timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        return await self._execute(sql, timeout)

    async def _scalar_column(self, sql: str, timeout: float) -> List[str]:
        rows, _ = await self._execute(sql, timeout)
        return [next(iter(r.values())) for r in rows if r]

    # -- metadata -----------------------------------------------------------
    #
    # Identifiers are quoted rather than interpolated raw: a catalog or table
    # name reaches these from a tool argument, and quoting is what stops one
    # from closing the statement and starting another.

    async def list_catalogs(self, timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS) -> List[str]:
        return await self._scalar_column("SHOW CATALOGS", timeout)

    async def list_schemas(
        self, catalog: str, timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS
    ) -> List[str]:
        return await self._scalar_column(f"SHOW SCHEMAS FROM {quote_identifier(catalog)}", timeout)

    async def list_tables(
        self, catalog: str, schema: str, timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS
    ) -> List[str]:
        return await self._scalar_column(
            f"SHOW TABLES FROM {quote_qualified_name(catalog, schema)}", timeout
        )

    async def get_schema(
        self,
        catalog: str,
        schema: str,
        table: str,
        timeout: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    ) -> List[Dict[str, Any]]:
        rows, _ = await self._execute(
            f"DESCRIBE {quote_qualified_name(catalog, schema, table)}", timeout
        )
        return rows


def _new_connection(
    host: str,
    port: int,
    user: str,
    password: Optional[str],
    catalog: Optional[str],
    schema: Optional[str],
    http_scheme: str,
) -> Any:
    auth = trino.auth.BasicAuthentication(user, password) if password else None
    return trino_connect(
        host=host,
        port=port,
        user=user,
        auth=auth,
        catalog=catalog,
        schema=schema,
        http_scheme=http_scheme,
        request_timeout=30,
    )


def _get_or_create(key: Tuple, factory: Any) -> Tuple[Any, threading.Lock]:
    """Return a cached connection for ``key``, evicting stale and surplus entries."""
    now = time.monotonic()
    with _cache_lock:
        for stale in [
            k
            for k, (_, _, ts) in _connections.items()
            if now - ts > _CONNECTION_TTL_SECONDS and k != key
        ]:
            conn, _, _ = _connections.pop(stale)
            _close_quietly(conn)

        cached = _connections.get(key)
        if cached is not None:
            conn, lock, _ = cached
            _connections[key] = (conn, lock, now)
            return conn, lock

        if len(_connections) >= _MAX_CACHED_CONNECTIONS:
            oldest = min(_connections, key=lambda k: _connections[k][2])
            conn, _, _ = _connections.pop(oldest)
            _close_quietly(conn)

        conn = factory()
        lock = threading.Lock()
        _connections[key] = (conn, lock, now)
        return conn, lock
