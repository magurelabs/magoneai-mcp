"""Connection pool cache for database connectors.

Module-level pool keyed by connection string. Evicts stale entries by TTL
and uses LRU eviction when at capacity. Each connector wraps a DB-level
connection pool (asyncpg.Pool, aiomysql.Pool, Motor client) that handles
concurrent access safely.
"""

import asyncio
import time
from typing import Any

MAX_POOLS = 50
POOL_TTL_SECONDS = 300  # 5 minutes - evict idle connectors

_pools: dict[str, tuple[Any, float]] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create lock in the current event loop context."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def get_or_create_connection(
    connection_string: str,
    connector_factory: Any,
) -> Any:
    """Get a cached connector or create a new one.

    Evicts stale connectors (older than POOL_TTL_SECONDS) and uses
    LRU eviction when at capacity.

    Args:
        connection_string: Database URI
        connector_factory: Async callable that returns a connected connector

    Returns:
        Connected DatabaseConnector instance (may be cached).
    """
    lock = _get_lock()
    now = time.monotonic()

    async with lock:
        # Evict stale entries first
        stale_keys = [k for k, (_, ts) in _pools.items() if now - ts > POOL_TTL_SECONDS]
        for key in stale_keys:
            old_connector, _ = _pools.pop(key)
            try:
                await old_connector.close()
            except Exception:
                pass

        # Return cached connector if still alive
        if connection_string in _pools:
            connector, _ = _pools[connection_string]
            _pools[connection_string] = (connector, now)
            return connector

        # Evict LRU if at capacity
        if len(_pools) >= MAX_POOLS:
            oldest_key = min(_pools, key=lambda k: _pools[k][1])
            old_connector, _ = _pools.pop(oldest_key)
            try:
                await old_connector.close()
            except Exception:
                pass

        connector = await connector_factory(connection_string)
        _pools[connection_string] = (connector, now)
        return connector


async def close_all_pools() -> None:
    """Close all cached connections. Called on server shutdown."""
    lock = _get_lock()
    async with lock:
        for _, (connector, _) in list(_pools.items()):
            try:
                await connector.close()
            except Exception:
                pass
        _pools.clear()
