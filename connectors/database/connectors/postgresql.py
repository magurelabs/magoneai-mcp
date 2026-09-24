"""PostgreSQL connector using asyncpg connection pool.

Uses asyncpg.Pool so concurrent tool calls each get their own connection.
Security: query() and search() use read-only transactions enforced at DB level.
"""

import json as _json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

try:
    import asyncpg
except ImportError:
    asyncpg = None

from .base import DatabaseConnector, parse_filter_key
from .sql_common import (
    build_where_clause,
    serialize_row,
    validate_column_type,
    validate_filter_columns,
)
from .sql_common import (
    safe_default as _safe_default,
)
from .sql_common import (
    safe_identifier as _safe_identifier,
)

QUERY_TIMEOUT_S = 30
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5


class PostgreSQLConnector(DatabaseConnector):
    """PostgreSQL database connector backed by asyncpg pool."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self, connection_string: str) -> None:
        if asyncpg is None:
            raise ImportError(
                "asyncpg is required for PostgreSQL connections. "
                "Install it with: pip install asyncpg"
            )
        self._pool = await asyncpg.create_pool(
            connection_string,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            timeout=10,
            command_timeout=QUERY_TIMEOUT_S,
            statement_cache_size=0,
            init=_init_connection,
        )

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(sql, *(params or []), timeout=QUERY_TIMEOUT_S)
                    return [_row_to_dict(r) for r in rows]
            except asyncpg.InvalidCachedStatementError:
                await conn.reset(timeout=5)
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(sql, *(params or []), timeout=QUERY_TIMEOUT_S)
                    return [_row_to_dict(r) for r in rows]

    async def insert(
        self, table: str, data: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        records = data if isinstance(data, list) else [data]
        if not records:
            return {"inserted_count": 0, "inserted_ids": []}

        columns = list(records[0].keys())
        col_str = ", ".join(f'"{_safe_identifier(c)}"' for c in columns)
        inserted_ids: list[Any] = []

        async with self._pool.acquire() as conn:
            col_types = await _get_column_types(conn, table)
            for record in records:
                placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
                values = [_coerce_value(record.get(c), col_types.get(c)) for c in columns]
                sql = (
                    f'INSERT INTO "{_safe_identifier(table)}" ({col_str}) '
                    f"VALUES ({placeholders}) RETURNING *"
                )
                row = await conn.fetchrow(sql, *values, timeout=QUERY_TIMEOUT_S)
                if row:
                    inserted_ids.append(row[0])

        return {"inserted_count": len(records), "inserted_ids": inserted_ids}

    async def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        if not filters:
            return {"updated_count": 0, "error": "Filters required for update"}

        set_parts: list[str] = []
        params: list[Any] = []
        idx = 1

        async with self._pool.acquire() as conn:
            col_types = await _get_column_types(conn, table)

            for col, val in data.items():
                set_parts.append(f'"{_safe_identifier(col)}" = ${idx}')
                params.append(_coerce_value(val, col_types.get(col)))
                idx += 1

            where_clause, where_params, idx = build_where_clause(
                filters,
                dialect="postgresql",
                start_idx=idx,
            )
            params.extend(where_params)

            sql = (
                f'UPDATE "{_safe_identifier(table)}" '
                f"SET {', '.join(set_parts)} WHERE {where_clause}"
            )
            result = await conn.execute(sql, *params, timeout=QUERY_TIMEOUT_S)
        count = int(result.split()[-1]) if result else 0
        return {"updated_count": count}

    async def search(
        self,
        table: str,
        filters: dict[str, Any],
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, limit)
        params: list[Any] = []
        idx = 1

        async with self._pool.acquire() as conn:
            if filters:
                col_types = await _get_column_types(conn, table)
                if not col_types:
                    raise ValueError(f"Table '{table}' not found or has no columns")
                validate_filter_columns(filters, set(col_types.keys()))
                coerced = {k: _coerce_filter_value(k, v, col_types) for k, v in filters.items()}
                where_clause, where_params, idx = build_where_clause(
                    coerced,
                    dialect="postgresql",
                    start_idx=idx,
                )
                params.extend(where_params)
                where_sql = f"WHERE {where_clause}"
            else:
                where_sql = ""

            order_sql = f'ORDER BY "{_safe_identifier(order_by)}"' if order_by else ""
            sql = (
                f'SELECT * FROM "{_safe_identifier(table)}" {where_sql} '
                f"{order_sql} LIMIT ${idx} OFFSET ${idx + 1}"
            )
            params.extend([limit, offset])

            try:
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(sql, *params, timeout=QUERY_TIMEOUT_S)
                    return [_row_to_dict(r) for r in rows]
            except asyncpg.InvalidCachedStatementError:
                await conn.reset(timeout=5)
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(sql, *params, timeout=QUERY_TIMEOUT_S)
                    return [_row_to_dict(r) for r in rows]

    async def get_tables(self) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name",
                timeout=QUERY_TIMEOUT_S,
            )
        return [r["table_name"] for r in rows]

    async def get_schema(self, table: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.column_name, c.data_type, c.is_nullable, "
                "c.column_default, "
                "CASE WHEN pk.column_name IS NOT NULL THEN true "
                "ELSE false END as is_primary_key "
                "FROM information_schema.columns c "
                "LEFT JOIN ("
                "  SELECT ku.column_name FROM information_schema.table_constraints tc "
                "  JOIN information_schema.key_column_usage ku "
                "    ON tc.constraint_name = ku.constraint_name "
                "  WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = $1"
                ") pk ON c.column_name = pk.column_name "
                "WHERE c.table_name = $1 AND c.table_schema = 'public' "
                "ORDER BY c.ordinal_position",
                table,
                timeout=QUERY_TIMEOUT_S,
            )
        return [
            {
                "name": r["column_name"],
                "type": r["data_type"],
                "nullable": r["is_nullable"] == "YES",
                "default": r["column_default"],
                "primary_key": r["is_primary_key"],
            }
            for r in rows
        ]

    async def create_table(self, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        col_defs: list[str] = []
        for col in columns:
            col_type = validate_column_type(col.get("type", "TEXT"))
            parts = [f'"{_safe_identifier(col["name"])}"', col_type]
            if col.get("primary_key"):
                parts.append("PRIMARY KEY")
            if not col.get("nullable", True) and not col.get("primary_key"):
                parts.append("NOT NULL")
            if col.get("default") is not None:
                safe_val = _safe_default(str(col["default"]))
                parts.append(f"DEFAULT '{safe_val}'")
            col_defs.append(" ".join(parts))

        sql = f'CREATE TABLE IF NOT EXISTS "{_safe_identifier(table)}" ({", ".join(col_defs)})'
        async with self._pool.acquire() as conn:
            await conn.execute(sql, timeout=QUERY_TIMEOUT_S)
        return {"created": True, "table": table}

    async def drop_table(self, table: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            await conn.execute(
                f'DROP TABLE IF EXISTS "{_safe_identifier(table)}"',
                timeout=QUERY_TIMEOUT_S,
            )
        return {"dropped": True, "table": table}

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None


async def _init_connection(conn: "asyncpg.Connection") -> None:
    """Decode json/jsonb into real Python objects.

    Without this, asyncpg hands back the raw JSON *string*, and serialize_row
    passes it through untouched (a string is already JSON-serializable). Callers
    then receive `'[{"a": 1}]'` where they expect `[{"a": 1}]` — so a query that
    builds structured output with jsonb_agg / jsonb_build_array is unusable
    downstream without the caller re-parsing it by hand.
    """
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name,
            encoder=_json.dumps,
            decoder=_json.loads,
            schema="pg_catalog",
        )


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert asyncpg Record to a JSON-serializable dict."""
    return serialize_row(dict(row))


async def _get_column_types(
    conn: asyncpg.Connection,
    table: str,
) -> dict[str, str]:
    """Fetch column name → data_type map for a table."""
    rows = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = $1 AND table_schema = 'public'",
        _safe_identifier(table),
        timeout=QUERY_TIMEOUT_S,
    )
    return {r["column_name"]: r["data_type"].lower() for r in rows}


_STRICT_STRING_TYPES = {
    "date",
    "timestamp without time zone",
    "timestamp with time zone",
    "time without time zone",
    "integer",
    "smallint",
    "bigint",
    "real",
    "double precision",
    "numeric",
    "decimal",
    "uuid",
}


def _coerce_filter_value(key: str, value: Any, col_types: dict[str, str]) -> Any:
    """Coerce a filter value to the expected column type, handling list values for IN/NOT IN."""
    col, op_suffix = parse_filter_key(key)
    # col_types keys are lowercase (pg folds unquoted identifiers); handle mixed-case filter keys.
    col_type = col_types.get(col) or col_types.get(col.lower())
    # Pattern/null ops use the value as-is — it is not a column value to coerce.
    if op_suffix in ("__is_null", "__contains", "__starts_with", "__ends_with"):
        return value
    # IN / NOT IN (and __eq/__ne with lists) — coerce each element individually.
    if isinstance(value, list):
        return [_coerce_value(v, col_type) for v in value]
    return _coerce_value(value, col_type)


def _coerce_value(value: Any, col_type: str | None) -> Any:
    """Coerce a JSON-origin value to the Python type asyncpg expects."""
    if value is None or col_type is None:
        return value

    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        raise ValueError(
            f"Unresolved template variable {value!r} — the workflow did not substitute this "
            "value before calling the tool (likely the referenced step returned no rows)"
        )

    if col_type in ("json", "jsonb") and not isinstance(value, str):
        return _json.dumps(value, default=str)

    if isinstance(value, str):
        if value == "" and col_type in _STRICT_STRING_TYPES:
            return None
        try:
            if col_type == "date":
                return date.fromisoformat(value)
            if col_type in ("timestamp without time zone", "timestamp with time zone"):
                return datetime.fromisoformat(value)
            if col_type == "time without time zone":
                return time.fromisoformat(value)
            if col_type in ("integer", "smallint", "bigint"):
                return int(value)
            if col_type in ("real", "double precision"):
                return float(value)
            if col_type in ("numeric", "decimal"):
                return Decimal(value)
            if col_type == "boolean":
                lower = value.lower()
                if lower in ("true", "1", "yes", "on", "t"):
                    return True
                if lower in ("false", "0", "no", "off", "f"):
                    return False
                raise ValueError(
                    f"'{value}' is not a valid boolean. Use true/false, yes/no, 1/0, on/off"
                )
            if col_type == "uuid":
                return UUID(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Cannot coerce value {value!r} to column type {col_type!r}: {exc}"
            ) from exc

    return value
