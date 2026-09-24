"""Microsoft SQL Server connector using pyodbc with FastAPI threadpool.

Requires:
  - ODBC Driver 17 or 18 for SQL Server installed in the runtime image
  - pyodbc>=5.0

Connection string format::

    mssql://user:pass@server:1433/database
    mssql://user:pass@server/database?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes

Architecture:
  - Synchronous pyodbc — never call blocking pyodbc functions in async context
  - Public async methods delegate to sync helpers via run_in_threadpool
    so the FastAPI event loop is never blocked
  - ODBC-level connection pooling (``pyodbc.pooling = True``, the default)
    transparently reuses physical connections across calls

Security:
  - ``_query_db`` and ``_search_db`` roll back their transaction after reads
    so accidental writes via raw SQL cannot be committed
  - Multi-statement queries are rejected to prevent read-only bypass
  - Identifiers are bracket-quoted to prevent SQL injection
"""

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# anyio, not fastapi. The only thing needed here is "run this blocking
# driver call off the event loop", and anyio is already a core dependency
# via the SDK. Importing fastapi for it made a DATABASE connector depend on
# a web framework that nothing declared, so the module would not import at
# all on a clean install. Every call site is positional, so the signatures
# match exactly.
from anyio.to_thread import run_sync as run_in_threadpool

from .base import DatabaseConnector
from .sql_common import build_where_clause, validate_column_type, validate_filter_columns
from .sql_common import safe_default as _safe_default
from .sql_common import safe_identifier as _safe_id
from .sql_common import serialize_row as _serialize_row

_logger = logging.getLogger(__name__)

try:
    import pyodbc
except ImportError:
    pyodbc = None  # type: ignore[assignment]

_DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


# ---------------------------------------------------------------------------
# Module-level helpers (pure sync, no class state)
# ---------------------------------------------------------------------------


def _reject_multi_statement(sql: str) -> None:
    """Reject queries containing multiple SQL statements."""
    stripped = sql.rstrip().rstrip(";").rstrip()
    if ";" in stripped:
        raise ValueError("Multi-statement queries are not allowed")


def _id(name: str) -> str:
    """Return a bracket-quoted SQL Server identifier."""
    return f"[{_safe_id(name)}]"


def _connect(dsn: str) -> "pyodbc.Connection":
    """Open a pyodbc connection.

    Physical connections are recycled by the ODBC driver pool, so this call
    is cheap after the first open.
    """
    conn = pyodbc.connect(dsn, autocommit=False)
    conn.setdecoding(pyodbc.SQL_CHAR, encoding="utf-8")
    conn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-8")
    conn.setencoding(encoding="utf-8")
    return conn


def _rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    """Convert an executed cursor's rows to a list of serialisable dicts."""
    if cursor.description is None:
        return []
    columns = [desc[0].lower() for desc in cursor.description]
    return [_serialize_row(dict(zip(columns, row))) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Sync DB helpers — dispatched to threadpool, never called in async context
# ---------------------------------------------------------------------------


def _query_db(dsn: str, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    _reject_multi_statement(sql)
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
            result = _rows_to_dicts(cur)
        conn.rollback()
        return result
    finally:
        conn.close()


def _insert_db(dsn: str, table: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    columns = list(records[0].keys())
    col_str = ", ".join(_id(c) for c in columns)
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {_id(table)} ({col_str}) VALUES ({placeholders})"

    inserted_ids: list[Any] = []
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            for record in records:
                values = [record.get(c) for c in columns]
                cur.execute(sql, values)
                cur.execute("SELECT SCOPE_IDENTITY()")
                row = cur.fetchone()
                inserted_ids.append(row[0] if row else None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"inserted_count": len(records), "inserted_ids": inserted_ids}


def _update_db(
    dsn: str, table: str, filters: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    set_parts: list[str] = []
    params: list[Any] = []
    for col, val in data.items():
        set_parts.append(f"{_id(col)} = ?")
        params.append(val)

    where_clause, where_params, _ = build_where_clause(filters, dialect="mssql")
    params.extend(where_params)

    sql = f"UPDATE {_id(table)} SET {', '.join(set_parts)} WHERE {where_clause}"
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            updated = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"updated_count": updated}


def _get_column_names_db(conn: "pyodbc.Connection", table: str) -> set[str]:
    """Return lowercase column names for a table using an existing connection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?",
            [table],
        )
        rows = cur.fetchall()
    return {row[0].lower() for row in rows}


def _search_db(
    dsn: str,
    table: str,
    filters: dict[str, Any],
    limit: int,
    offset: int,
    order_by: str | None,
) -> list[dict[str, Any]]:
    params: list[Any] = []

    conn = _connect(dsn)
    try:
        if filters:
            known_cols = _get_column_names_db(conn, table)
            if not known_cols:
                raise ValueError(f"Table '{table}' not found or has no columns")
            validate_filter_columns(filters, known_cols)
            where_clause, where_params, _ = build_where_clause(filters, dialect="mssql")
            params.extend(where_params)
            where_sql = f"WHERE {where_clause}"
        else:
            where_sql = ""

        # SQL Server requires ORDER BY when using OFFSET/FETCH NEXT
        order_sql = f"ORDER BY {_id(order_by)}" if order_by else "ORDER BY (SELECT NULL)"
        params.extend([offset, limit])
        sql = f"SELECT * FROM {_id(table)} {where_sql} {order_sql} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"

        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = _rows_to_dicts(cur)
        conn.rollback()
        return result
    finally:
        conn.close()


def _get_tables_db(dsn: str) -> list[str]:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
            )
            rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _get_schema_db(dsn: str, table: str) -> list[dict[str, Any]]:
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
                "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS IS_PK "
                "FROM INFORMATION_SCHEMA.COLUMNS c "
                "LEFT JOIN ("
                "  SELECT ku.COLUMN_NAME "
                "  FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "  JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku "
                "    ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME "
                "  WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_NAME = ?"
                ") pk ON c.COLUMN_NAME = pk.COLUMN_NAME "
                "WHERE c.TABLE_NAME = ? "
                "ORDER BY c.ORDINAL_POSITION",
                [table, table],
            )
            rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    return [
        {
            "name": row[0].lower(),
            "type": row[1],
            "nullable": row[2] == "YES",
            "default": row[3],
            "primary_key": bool(row[4]),
        }
        for row in rows
    ]


def _create_table_db(dsn: str, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    col_defs: list[str] = []
    for col in columns:
        col_type = validate_column_type(col.get("type", "NVARCHAR(255)"))
        parts = [_id(col["name"]), col_type]
        if col.get("primary_key"):
            parts.append("PRIMARY KEY")
        if not col.get("nullable", True) and not col.get("primary_key"):
            parts.append("NOT NULL")
        if col.get("default") is not None:
            safe_val = _safe_default(str(col["default"]))
            parts.append(f"DEFAULT '{safe_val}'")
        col_defs.append(" ".join(parts))

    # SQL Server has no CREATE TABLE IF NOT EXISTS; use OBJECT_ID guard instead
    safe_table = _safe_id(table)
    sql = (
        f"IF OBJECT_ID(N'[{safe_table}]', N'U') IS NULL "
        f"CREATE TABLE [{safe_table}] ({', '.join(col_defs)})"
    )
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"created": True, "table": table}


def _drop_table_db(dsn: str, table: str) -> dict[str, Any]:
    safe_table = _safe_id(table)
    sql = f"IF OBJECT_ID(N'[{safe_table}]', N'U') IS NOT NULL DROP TABLE [{safe_table}]"
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"dropped": True, "table": table}


def _test_connect(dsn: str) -> None:
    """Open and immediately close a connection to validate the DSN at connect time."""
    conn = pyodbc.connect(dsn, timeout=10)
    conn.close()


# ---------------------------------------------------------------------------
# MSSQLConnector — async facade over synchronous pyodbc
# ---------------------------------------------------------------------------


class MSSQLConnector(DatabaseConnector):
    """Microsoft SQL Server connector backed by pyodbc.

    Uses synchronous pyodbc with ODBC-level connection pooling.  All blocking
    operations are dispatched to FastAPI's threadpool via ``run_in_threadpool``
    so the event loop is never blocked.

    Connection string format::

        mssql://user:pass@server:1433/database
        mssql://user:pass@server/database?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
    """

    def __init__(self) -> None:
        self._dsn: str | None = None

    async def connect(self, connection_string: str) -> None:
        if pyodbc is None:
            raise ImportError(
                "pyodbc is required for SQL Server connections. Install it with: pip install pyodbc"
            )
        self._dsn = _parse_dsn(connection_string)
        # Validate credentials immediately rather than failing on first tool call
        await run_in_threadpool(_test_connect, self._dsn)

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return await run_in_threadpool(_query_db, self._dsn, sql, params)

    async def insert(
        self, table: str, data: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        records = data if isinstance(data, list) else [data]
        if not records:
            return {"inserted_count": 0, "inserted_ids": []}
        return await run_in_threadpool(_insert_db, self._dsn, table, records)

    async def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        if not filters:
            return {"updated_count": 0, "error": "Filters required for update"}
        return await run_in_threadpool(_update_db, self._dsn, table, filters, data)

    async def search(
        self,
        table: str,
        filters: dict[str, Any],
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, limit)
        return await run_in_threadpool(
            _search_db, self._dsn, table, filters, limit, offset, order_by
        )

    async def get_tables(self) -> list[str]:
        return await run_in_threadpool(_get_tables_db, self._dsn)

    async def get_schema(self, table: str) -> list[dict[str, Any]]:
        return await run_in_threadpool(_get_schema_db, self._dsn, table)

    async def create_table(self, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        return await run_in_threadpool(_create_table_db, self._dsn, table, columns)

    async def drop_table(self, table: str) -> dict[str, Any]:
        return await run_in_threadpool(_drop_table_db, self._dsn, table)

    async def close(self) -> None:
        # ODBC pools the physical connection; clearing the DSN is sufficient.
        self._dsn = None


# ---------------------------------------------------------------------------
# DSN builder
# ---------------------------------------------------------------------------


def _parse_dsn(connection_string: str) -> str:
    """Convert an ``mssql://`` URI to a pyodbc connection string."""
    parsed = urlparse(connection_string)
    query = parse_qs(parsed.query)

    server = parsed.hostname or "localhost"
    port = parsed.port or 1433
    database = parsed.path.lstrip("/") if parsed.path else ""
    user = parsed.username or ""
    password = unquote(parsed.password or "")
    driver = unquote(query.get("driver", [_DEFAULT_DRIVER])[0])
    trust_cert = query.get("TrustServerCertificate", ["no"])[0]
    encrypt = query.get("Encrypt", ["yes"])[0]

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server},{port};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        f"TrustServerCertificate={trust_cert};"
        f"Encrypt={encrypt};"
    )
