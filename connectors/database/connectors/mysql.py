"""MySQL connector using aiomysql connection pool.

Security:
- query() and search() use SET TRANSACTION READ ONLY + ROLLBACK
- Multi-statement queries rejected to prevent bypass of read-only mode
"""

from typing import Any
from urllib.parse import urlparse

try:
    import aiomysql
except ImportError:
    aiomysql = None

from .base import DatabaseConnector
from .sql_common import (
    build_where_clause,
    validate_column_type,
    validate_filter_columns,
)
from .sql_common import (
    safe_default as _safe_default,
)
from .sql_common import (
    safe_identifier as _safe_id,
)
from .sql_common import (
    serialize_row as _serialize_row,
)

QUERY_TIMEOUT_MS = 30000
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5


def _reject_multi_statement(sql: str) -> None:
    """Reject queries containing multiple SQL statements."""
    stripped = sql.rstrip().rstrip(";").rstrip()
    if ";" in stripped:
        raise ValueError("Multi-statement queries are not allowed")


class MySQLConnector(DatabaseConnector):
    """MySQL database connector backed by aiomysql pool."""

    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self, connection_string: str) -> None:
        if aiomysql is None:
            raise ImportError(
                "aiomysql is required for MySQL connections. Install it with: pip install aiomysql"
            )
        parsed = urlparse(connection_string)
        self._pool = await aiomysql.create_pool(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username or "root",
            password=parsed.password or "",
            db=parsed.path.lstrip("/") if parsed.path else "",
            connect_timeout=10,
            autocommit=True,
            minsize=POOL_MIN_SIZE,
            maxsize=POOL_MAX_SIZE,
        )

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        _reject_multi_statement(sql)
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute("START TRANSACTION")
                try:
                    await cur.execute(sql, params or [])
                    rows = await cur.fetchall()
                    return [_serialize_row(r) for r in rows]
                finally:
                    await cur.execute("ROLLBACK")

    async def insert(
        self, table: str, data: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        records = data if isinstance(data, list) else [data]
        if not records:
            return {"inserted_count": 0, "inserted_ids": []}

        columns = list(records[0].keys())
        col_str = ", ".join(f"`{_safe_id(c)}`" for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO `{_safe_id(table)}` ({col_str}) VALUES ({placeholders})"

        inserted_ids: list[Any] = []
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                for record in records:
                    values = [record.get(c) for c in columns]
                    await cur.execute(sql, values)
                    inserted_ids.append(cur.lastrowid)

        return {"inserted_count": len(records), "inserted_ids": inserted_ids}

    async def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        if not filters:
            return {"updated_count": 0, "error": "Filters required for update"}

        set_parts: list[str] = []
        params: list[Any] = []
        for col, val in data.items():
            set_parts.append(f"`{_safe_id(col)}` = %s")
            params.append(val)

        where_clause, where_params, _ = build_where_clause(
            filters,
            dialect="mysql",
        )
        params.extend(where_params)

        sql = f"UPDATE `{_safe_id(table)}` SET {', '.join(set_parts)} WHERE {where_clause}"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return {"updated_count": cur.rowcount}

    async def _get_column_names(self, table: str) -> set[str]:
        """Return lowercase column names for a table."""
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"DESCRIBE `{_safe_id(table)}`")
                rows = await cur.fetchall()
        return {r["Field"].lower() for r in rows}

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

        if filters:
            known_cols = await self._get_column_names(table)
            validate_filter_columns(filters, known_cols)
            where_clause, where_params, _ = build_where_clause(
                filters,
                dialect="mysql",
            )
            params.extend(where_params)
            where_sql = f"WHERE {where_clause}"
        else:
            where_sql = ""

        order_sql = f"ORDER BY `{_safe_id(order_by)}`" if order_by else ""
        sql = f"SELECT * FROM `{_safe_id(table)}` {where_sql} {order_sql} LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute("START TRANSACTION")
                try:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    return [_serialize_row(r) for r in rows]
                finally:
                    await cur.execute("ROLLBACK")

    async def get_tables(self) -> list[str]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES")
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def get_schema(self, table: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"DESCRIBE `{_safe_id(table)}`")
                rows = await cur.fetchall()
        return [
            {
                "name": r["Field"],
                "type": r["Type"],
                "nullable": r["Null"] == "YES",
                "default": r["Default"],
                "primary_key": r["Key"] == "PRI",
            }
            for r in rows
        ]

    async def create_table(self, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        col_defs: list[str] = []
        for col in columns:
            col_type = validate_column_type(col.get("type", "TEXT"))
            parts = [f"`{_safe_id(col['name'])}`", col_type]
            if col.get("primary_key"):
                parts.append("PRIMARY KEY")
            if not col.get("nullable", True) and not col.get("primary_key"):
                parts.append("NOT NULL")
            if col.get("default") is not None:
                safe_val = _safe_default(str(col["default"]))
                parts.append(f"DEFAULT '{safe_val}'")
            col_defs.append(" ".join(parts))

        sql = f"CREATE TABLE IF NOT EXISTS `{_safe_id(table)}` ({', '.join(col_defs)})"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
        return {"created": True, "table": table}

    async def drop_table(self, table: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS `{_safe_id(table)}`")
        return {"dropped": True, "table": table}

    async def close(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
