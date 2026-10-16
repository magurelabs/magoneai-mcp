"""The csv-to-sql connector.

Recovered from a built v1 image (`/app/src/mcp/servers/csv_to_sql/`), whose source
survived nowhere else.

7 tools, 5 of them proposed read-only. Tool names and input schemas are
carried across from v1 verbatim via `declare_schema`, with a description added
to every property v1 left undescribed.

## Whose database

The caller's. v1 wrote every import into one database configured on the
server, so every caller of a shared deployment created tables in, and could
list and read, the same database. Here the target arrives with each call in
the same headers `connectors/database` uses (see `_connection.py`), a
connection is opened for that call and closed before it returns, and a call
that names no database is refused.

The import bookkeeping table v1 kept (`_user_data_tables`, which backs
`get_import` and `list_imports`) is kept, in the caller's database, under the
same name and shape so v1 data still reads. Reads no longer create it: v1 ran
`CREATE TABLE IF NOT EXISTS` from `list_imports`, a write from a read tool.

## Writes

`create_table` is destructive (`drop_existing` drops a table) and
`import_data` writes rows; both are refused under `MCP_READ_ONLY=1`. The
schema name and every column name are identifiers, validated and quoted; v1
interpolated both into SQL unescaped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema

from ._connection import POSTGRES_CREDENTIAL_HEADERS, ConnectionProblem, connect, describe_error
from ._csv import (
    ColumnInfo,
    columns_from_arguments,
    convert_value,
    generate_import_id,
    preview,
    quote_ident,
    read_rows,
    sanitize_table_name,
    validate_schema_name,
)

SERVER_NAME = "csv-to-sql"
SERVER_VERSION = "2.0.0"

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="Preview a CSV, create a matching PostgreSQL table and import the rows.",
)

CREDENTIAL_HEADERS = POSTGRES_CREDENTIAL_HEADERS

TRACKING_TABLE = "_user_data_tables"
BATCH_SIZE = 1000
MAX_ERRORS_REPORTED = 100
MAX_PREVIEW_ROWS = 1000
MAX_LIST_IMPORTS = 1000

_TRACKING_DDL = f"""
CREATE TABLE IF NOT EXISTS {quote_ident(TRACKING_TABLE)} (
    id SERIAL PRIMARY KEY,
    import_id VARCHAR(50) NOT NULL UNIQUE,
    table_name VARCHAR(255) NOT NULL,
    schema_name VARCHAR(255) NOT NULL DEFAULT 'public',
    source_file VARCHAR(500),
    row_count INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(schema_name, table_name)
)
"""

_REGISTER_SQL = f"""
INSERT INTO {quote_ident(TRACKING_TABLE)}
    (import_id, table_name, schema_name, source_file, row_count)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (schema_name, table_name) DO UPDATE
SET import_id = EXCLUDED.import_id,
    source_file = COALESCE(EXCLUDED.source_file, {quote_ident(TRACKING_TABLE)}.source_file),
    row_count = EXCLUDED.row_count,
    created_at = NOW()
"""  # noqa: S608


def _fail(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


async def _record(
    conn: Any,
    import_id: str,
    table: str,
    schema: str,
    source_file: str | None,
    row_count: int | None,
) -> str | None:
    """Record a table in the bookkeeping table; the reason it failed, or None.

    Best effort, and outside the data transaction: a role that may create the
    caller's table but not the bookkeeping one still gets its table and rows.
    """
    try:
        async with conn.transaction():
            await conn.execute(_TRACKING_DDL)
            await conn.execute(_REGISTER_SQL, import_id, table, schema, source_file, row_count)
    except Exception as exc:  # noqa: BLE001
        return describe_error(exc)
    return None


async def _tracking_exists(conn: Any) -> bool:
    return bool(
        await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", quote_ident(TRACKING_TABLE))
    )


def _import_row(row: Any) -> dict[str, Any]:
    return {
        "import_id": row["import_id"],
        "table_name": row["table_name"],
        "schema": row["schema_name"],
        "source_file": row["source_file"],
        "row_count": row["row_count"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@server.tool(
    name="preview_csv",
    description=(
        "Parse a CSV file and preview its contents. Infers column types (INTEGER, TEXT, "
        "BOOLEAN, DATE, etc.) from the data. Returns column definitions and sample rows."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "csv_content": {
                "type": "string",
                "description": "The CSV file content as a string",
            },
            "has_header": {
                "type": "boolean",
                "description": "Whether the first row is a header row",
                "default": True,
            },
            "preview_rows": {
                "type": "integer",
                "description": "Number of rows to include in preview",
                "default": 5,
            },
        },
        "required": ["csv_content"],
    }
)
async def preview_csv(csv_content: str, has_header: bool = True, preview_rows: int = 5) -> dict:
    """Infer column definitions from a CSV. Touches no database."""
    try:
        return preview(
            csv_content,
            has_header=has_header,
            preview_rows=max(0, min(int(preview_rows), MAX_PREVIEW_ROWS)),
        )
    except Exception as exc:  # noqa: BLE001 - csv.Error, ValueError
        return _fail(f"Could not parse CSV: {exc}")


@server.tool(
    name="create_table",
    description=(
        "Create a PostgreSQL table based on the inferred schema from preview_csv. You must "
        "call preview_csv first to get the column definitions. An auto-increment 'id' "
        "primary key is added. drop_existing=true drops an existing table of that name "
        "and all its data first."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="write_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "Name for the new table (will be sanitized)",
            },
            "columns": {
                "type": "array",
                "description": "Column definitions from preview_csv",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Column name, as returned by preview_csv",
                        },
                        "original_name": {
                            "type": "string",
                            "description": "Header text the column came from",
                        },
                        "inferred_type": {
                            "type": "string",
                            "description": (
                                "TEXT, INTEGER, BIGINT, NUMERIC, BOOLEAN, DATE, TIMESTAMP or JSON"
                            ),
                        },
                        "nullable": {
                            "type": "boolean",
                            "description": "Whether the column accepts NULL",
                        },
                    },
                    "required": ["name", "inferred_type", "nullable"],
                },
            },
            "schema": {
                "type": "string",
                "description": "PostgreSQL schema name",
                "default": "public",
            },
            "drop_existing": {
                "type": "boolean",
                "description": "Drop the table if it already exists",
                "default": False,
            },
        },
        "required": ["table_name", "columns"],
    }
)
async def create_table(
    credentials: dict[str, str],
    table_name: str,
    columns: list[dict[str, Any]],
    schema: str = "public",
    drop_existing: bool = False,
) -> dict:
    """CREATE TABLE IF NOT EXISTS from preview_csv's columns, and record it."""
    try:
        schema_name = validate_schema_name(schema)
        column_info = columns_from_arguments(columns)
    except ValueError as exc:
        return _fail(str(exc))
    sanitized = sanitize_table_name(table_name or "")
    full_name = f"{quote_ident(schema_name)}.{quote_ident(sanitized)}"

    col_defs = ['    "id" SERIAL PRIMARY KEY']
    for col in column_info:
        null_clause = "" if col.nullable else " NOT NULL"
        col_defs.append(f"    {quote_ident(col.name)} {col.inferred_type.value}{null_clause}")
    create_sql = f"CREATE TABLE IF NOT EXISTS {full_name} (\n" + ",\n".join(col_defs) + "\n)"

    try:
        async with connect(credentials) as conn:
            async with conn.transaction():
                if drop_existing:
                    await conn.execute(f"DROP TABLE IF EXISTS {full_name}")
                await conn.execute(create_sql)
            bookkeeping_error = await _record(
                conn, generate_import_id(), sanitized, schema_name, table_name, None
            )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to create table: {describe_error(exc)}")

    result: dict[str, Any] = {"success": True, "table_name": table_name, "sql": create_sql}
    if bookkeeping_error:
        result["warning"] = f"Table created, but it was not recorded: {bookkeeping_error}"
    return result


@server.tool(
    name="import_data",
    description=(
        "Import CSV data into an existing PostgreSQL table. The table must exist (use "
        "create_table first). Rows are appended; a row that fails to convert or insert is "
        "reported and skipped."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="write_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "csv_content": {
                "type": "string",
                "description": "The CSV file content as a string",
            },
            "table_name": {
                "type": "string",
                "description": "Target table name",
            },
            "columns": {
                "type": "array",
                "description": "Column definitions (for type conversion)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Column name, as returned by preview_csv",
                        },
                        "inferred_type": {
                            "type": "string",
                            "description": (
                                "TEXT, INTEGER, BIGINT, NUMERIC, BOOLEAN, DATE, TIMESTAMP or JSON"
                            ),
                        },
                        "nullable": {
                            "type": "boolean",
                            "description": "Whether the column accepts NULL",
                        },
                    },
                    "required": ["name", "inferred_type"],
                },
            },
            "has_header": {
                "type": "boolean",
                "description": "Whether the first row is a header row",
                "default": True,
            },
            "schema": {
                "type": "string",
                "description": "PostgreSQL schema name",
                "default": "public",
            },
        },
        "required": ["csv_content", "table_name", "columns"],
    }
)
async def import_data(
    credentials: dict[str, str],
    csv_content: str,
    table_name: str,
    columns: list[dict[str, Any]],
    has_header: bool = True,
    schema: str = "public",
) -> dict:
    """Append the CSV's rows to the table, batch by batch, reporting rows that fail."""
    try:
        schema_name = validate_schema_name(schema)
        column_info = columns_from_arguments(columns)
        rows = read_rows(csv_content or "")
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - csv.Error
        return _fail(f"Could not parse CSV: {exc}")

    import_id = generate_import_id()
    sanitized = sanitize_table_name(table_name or "")
    full_name = f"{quote_ident(schema_name)}.{quote_ident(sanitized)}"
    if has_header:
        rows = rows[1:]

    col_names = ", ".join(quote_ident(c.name) for c in column_info)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(column_info)))
    insert_sql = f"INSERT INTO {full_name} ({col_names}) VALUES ({placeholders})"  # noqa: S608

    errors: list[str] = []
    rows_imported = 0
    bookkeeping_error = None
    offset = 2 if has_header else 1

    try:
        async with connect(credentials) as conn:
            exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"{full_name}")
            if not exists:
                return _fail(
                    f"Table {schema_name}.{sanitized} does not exist; call create_table first"
                )
            for start in range(0, len(rows), BATCH_SIZE):
                converted: list[tuple[int, list[Any]]] = []
                for index, row in enumerate(rows[start : start + BATCH_SIZE], start=start):
                    try:
                        converted.append((index, _convert_row(row, column_info)))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"Row {index + offset}: {exc}")
                if not converted:
                    continue
                try:
                    async with conn.transaction():
                        await conn.executemany(insert_sql, [values for _, values in converted])
                    rows_imported += len(converted)
                except Exception:  # noqa: BLE001 - retried row by row to name the bad rows
                    for index, values in converted:
                        try:
                            async with conn.transaction():
                                await conn.execute(insert_sql, *values)
                            rows_imported += 1
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"Row {index + offset}: {describe_error(exc)}")
            bookkeeping_error = await _record(
                conn, import_id, sanitized, schema_name, None, rows_imported
            )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to import data: {describe_error(exc)}")

    result: dict[str, Any] = {
        "success": True,
        "import_id": import_id,
        "table_name": sanitized,
        "rows_imported": rows_imported,
        "columns_created": [c.name for c in column_info],
        "errors": errors[:MAX_ERRORS_REPORTED],
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }
    if bookkeeping_error:
        result["warning"] = f"Rows imported, but the import was not recorded: {bookkeeping_error}"
    return result


def _convert_row(row: list[str], columns: list[ColumnInfo]) -> list[Any]:
    return [
        convert_value(row[i] if i < len(row) else "", c.inferred_type)
        for i, c in enumerate(columns)
    ]


@server.tool(
    name="list_tables",
    description="List all tables in the database schema.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "schema": {
                "type": "string",
                "description": "PostgreSQL schema name",
                "default": "public",
            },
        },
    }
)
async def list_tables(credentials: dict[str, str], schema: str = "public") -> dict:
    """Base tables in a schema, with their column counts."""
    try:
        async with connect(credentials) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    table_name,
                    (SELECT COUNT(*) FROM information_schema.columns
                     WHERE table_schema = $1 AND table_name = t.table_name) as column_count
                FROM information_schema.tables t
                WHERE table_schema = $1 AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                schema or "public",
            )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to list tables: {describe_error(exc)}")
    return {"tables": [dict(row) for row in rows]}


@server.tool(
    name="get_table_info",
    description=(
        "Get detailed information about a specific table including columns and row count."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "Name of the table",
            },
            "schema": {
                "type": "string",
                "description": "PostgreSQL schema name",
                "default": "public",
            },
        },
        "required": ["table_name"],
    }
)
async def get_table_info(
    credentials: dict[str, str], table_name: str, schema: str = "public"
) -> dict:
    """Columns and exact row count of one table."""
    try:
        schema_name = validate_schema_name(schema)
    except ValueError as exc:
        return _fail(str(exc))
    sanitized = sanitize_table_name(table_name or "")
    try:
        async with connect(credentials) as conn:
            columns = await conn.fetch(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                ORDER BY ordinal_position
                """,
                schema_name,
                sanitized,
            )
            if not columns:
                return _fail(f"Table not found: {schema_name}.{sanitized}")
            async with conn.transaction(readonly=True):
                row_count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {quote_ident(schema_name)}.{quote_ident(sanitized)}"  # noqa: S608
                )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to read table info: {describe_error(exc)}")
    return {
        "table_name": sanitized,
        "schema": schema_name,
        "columns": [dict(col) for col in columns],
        "row_count": row_count,
    }


@server.tool(
    name="get_import",
    description="Get information about a specific CSV import by its unique import ID.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "import_id": {
                "type": "string",
                "description": "The unique import identifier (e.g., imp_20250113_a1b2c3d4)",
            },
        },
        "required": ["import_id"],
    }
)
async def get_import(credentials: dict[str, str], import_id: str) -> dict:
    """One import's record from the bookkeeping table."""
    try:
        async with connect(credentials) as conn:
            row = None
            if await _tracking_exists(conn):
                row = await conn.fetchrow(
                    f"""
                    SELECT import_id, table_name, schema_name, source_file, row_count, created_at
                    FROM {quote_ident(TRACKING_TABLE)}
                    WHERE import_id = $1
                    """,  # noqa: S608
                    import_id,
                )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to read import: {describe_error(exc)}")
    if row is None:
        return _fail(f"Import not found: {import_id}")
    return {"success": True, "import": _import_row(row)}


@server.tool(
    name="list_imports",
    description=(
        "List all CSV imports with their metadata, ordered by creation date (newest first)."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of imports to return",
                "default": 100,
            },
        },
    }
)
async def list_imports(credentials: dict[str, str], limit: int = 100) -> dict:
    """Recorded imports, newest first."""
    bounded = max(1, min(int(limit or 100), MAX_LIST_IMPORTS))
    try:
        async with connect(credentials) as conn:
            rows = []
            if await _tracking_exists(conn):
                rows = await conn.fetch(
                    f"""
                    SELECT import_id, table_name, schema_name, source_file, row_count, created_at
                    FROM {quote_ident(TRACKING_TABLE)}
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,  # noqa: S608
                    bounded,
                )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to list imports: {describe_error(exc)}")
    imports = [_import_row(row) for row in rows]
    return {"success": True, "imports": imports, "total": len(imports)}
