"""The vanna connector: ask a PostgreSQL database questions in plain language.

Recovered from a built v1 image (`/app/src/mcp/servers/vanna/`), whose source
survived nowhere else.

13 tools, 7 of them proposed read-only. Tool names are v1's. Input schemas are
v1's verbatim via `declare_schema`, except `connect_database`, which no longer
takes a host and password as arguments (see below).

## What changed from v1, and why it had to

v1 was a single-tenant service wearing a connector's name:

- one model key from the server's environment, spent on every caller;
- one database from the server's environment, and `connect_database`
  repointed **the whole process** at whatever host the last caller named, so
  one caller's connection became every caller's;
- one training directory on the server's disk shared by everyone, with
  contexts keyed only by name.

Here every call carries its own database (the headers `connectors/database`
uses, PostgreSQL only, see `_connection.py`) and `ask_database` carries its own
model key (`X-Llm-Api-Key`, optional `X-Llm-Base-Url` and `X-Llm-Model`, see
`_engine.py`). Training is stored in the caller's own database (see
`_store.py`), so it is isolated by the same credentials that guard the data it
describes, survives restarts, and works on any replica. No credential outlives
the call that carried it.

`connect_database` is kept by name, for agents that call it first. With no
process-wide connection to set, it verifies the connection this call carries
and reports the server version.

`discover_*` and `train_from_schema` see every base table in the schema, not
only tables registered by the csv-to-sql connector: in the caller's own
database every table is the caller's. The three bookkeeping tables the two
connectors keep are left out.

`ask_database` executes only a single SELECT, inside a read-only transaction,
fetching at most `limit` rows. v1 checked only that the text began with
`select`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anyio
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema

from . import _store
from ._connection import POSTGRES_CREDENTIAL_HEADERS, ConnectionProblem, connect, describe_error
from ._engine import LLM_CREDENTIAL_HEADERS, NOT_ALLOWED_TO_SEE_DATA, LlmProblem, llm_settings

SERVER_NAME = "vanna"
SERVER_VERSION = "2.0.0"

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="Ask a PostgreSQL database questions in plain language, with your own model.",
)

DB_HEADERS = POSTGRES_CREDENTIAL_HEADERS
ASK_HEADERS = POSTGRES_CREDENTIAL_HEADERS + LLM_CREDENTIAL_HEADERS

DEFAULT_LIMIT = 100
MAX_LIMIT = 10000
QUERY_TIMEOUT_S = 30
NOT_CONNECTED_HINT = (
    "Send the database connection with this call (X-Db-Connection-String, or "
    "X-Db-Host / X-Db-Name / X-Db-Username / X-Db-Password)"
)

_EXCLUDED_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def _fail(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def _training_result(
    success: bool,
    items: int,
    kind: str,
    details: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    result = {
        "success": success,
        "items_trained": items,
        "training_type": kind,
        "details": details,
        "errors": errors,
    }
    if not success and errors:
        # v1 reported failure only in `errors`, which no client reads as one.
        # `error` makes the call a failure on the wire; see results.py.
        result["error"] = "; ".join(errors[:3])
    return result


async def _user_tables(conn: Any, schema: str, only: list[str] | None = None) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = $1
          AND table_type = 'BASE TABLE'
          AND NOT (table_name = ANY($2::text[]))
          AND ($3::text[] IS NULL OR table_name = ANY($3::text[]))
        ORDER BY table_name
        """,
        schema,
        list(_store.BOOKKEEPING_TABLES),
        only,
    )
    return [r["table_name"] for r in rows]


# -- context management ------------------------------------------------------


@server.tool(
    name="create_context",
    description=(
        "Create a new Vanna context (namespace) for isolated training. Each context has its "
        "own training data, allowing different workflows to use different knowledge bases."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="write_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Unique context name (alphanumeric with underscores, e.g., "
                    "'real_estate_analytics')"
                ),
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of what this context is for",
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of table names to associate with this context",
            },
        },
        "required": ["name"],
    }
)
async def create_context(
    credentials: dict[str, str],
    name: str,
    description: str = "",
    tables: list[str] | None = None,
) -> dict:
    """Record a named context in the caller's database."""
    if not name or not name.replace("_", "").isalnum():
        return _fail("Context name must be alphanumeric with underscores only")
    tables = tables or []
    try:
        async with connect(credentials) as conn:
            await _store.ensure_tables(conn)
            row = await conn.fetchrow(
                f"""
                INSERT INTO "{_store.CONTEXTS_TABLE}" (name, description, tables_json)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (name) DO NOTHING
                RETURNING created_at
                """,  # noqa: S608 - constant table name
                name,
                description or "",
                json.dumps(tables),
            )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to create context: {describe_error(exc)}")
    if row is None:
        return _fail(f"Context '{name}' already exists")
    return {
        "success": True,
        "context": {
            "name": name,
            "description": description or "",
            "tables": tables,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        },
    }


@server.tool(
    name="list_contexts",
    description="List all Vanna contexts (namespaces) with their training status.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="read_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {},
    }
)
async def list_contexts(credentials: dict[str, str]) -> dict:
    """Contexts recorded in the caller's database, newest first."""
    try:
        async with connect(credentials) as conn:
            rows = []
            if await _store.table_exists(conn, _store.CONTEXTS_TABLE):
                rows = await conn.fetch(
                    f"""
                    SELECT name, description, tables_json, trained_items, created_at
                    FROM "{_store.CONTEXTS_TABLE}" ORDER BY created_at DESC
                    """  # noqa: S608 - constant table name
                )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to list contexts: {describe_error(exc)}")
    contexts = [_store.context_row(r) for r in rows]
    return {"success": True, "contexts": contexts, "count": len(contexts)}


@server.tool(
    name="get_context",
    description="Get details of a specific Vanna context.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="read_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Context name",
            },
        },
        "required": ["name"],
    }
)
async def get_context(credentials: dict[str, str], name: str) -> dict:
    """One context from the caller's database."""
    try:
        async with connect(credentials) as conn:
            row = None
            if await _store.table_exists(conn, _store.CONTEXTS_TABLE):
                row = await conn.fetchrow(
                    f"""
                    SELECT name, description, tables_json, trained_items, created_at
                    FROM "{_store.CONTEXTS_TABLE}" WHERE name = $1
                    """,  # noqa: S608 - constant table name
                    name,
                )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to read context: {describe_error(exc)}")
    if row is None:
        return _fail(f"Context '{name}' not found")
    return {"success": True, "context": _store.context_row(row)}


@server.tool(
    name="delete_context",
    description="Delete a Vanna context and all its training data.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=DB_HEADERS,
    permission_group="write_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Context name to delete",
            },
        },
        "required": ["name"],
    }
)
async def delete_context(credentials: dict[str, str], name: str) -> dict:
    """Remove a context and every item trained into it.

    v1 removed the training only if the context happened to be cached in the
    process, so after a restart the record went and the training stayed.
    """
    try:
        key = _store.context_key(name)
    except _store.ContextNameError as exc:
        return _fail(str(exc))
    if key == _store.DEFAULT_CONTEXT:
        return _fail("Context name is required")
    try:
        async with connect(credentials) as conn:
            if not await _store.table_exists(conn, _store.CONTEXTS_TABLE):
                return _fail(f"Context '{name}' not found")
            async with conn.transaction():
                status = await conn.execute(
                    f'DELETE FROM "{_store.CONTEXTS_TABLE}" WHERE name = $1',  # noqa: S608
                    key,
                )
                if status == "DELETE 0":
                    return _fail(f"Context '{name}' not found")
                await _store.clear_items(conn, key)
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to delete context: {describe_error(exc)}")
    return {"success": True, "message": f"Context '{name}' deleted"}


# -- database ----------------------------------------------------------------


# v1 took host, port, database, user and password as ARGUMENTS and repointed
# the whole server at them. The connection now arrives in this call's headers,
# so the arguments are gone: a password is not something a model should hold.
@server.tool(
    name="connect_database",
    description=(
        "Verify the PostgreSQL connection this call carries and report the server version. "
        "The connection is configured on the connector (headers), not passed as arguments, "
        "and every other tool uses it directly, so calling this first is optional."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {},
    }
)
async def connect_database(credentials: dict[str, str]) -> dict:
    """Open the call's connection, read the version, close it."""
    try:
        async with connect(credentials) as conn:
            row = await conn.fetchrow(
                "SELECT version() AS version, current_database() AS database, "
                "host(inet_server_addr()) AS host"
            )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Connection failed: {describe_error(exc)}")
    return {
        "success": True,
        "message": f"Connected to {row['database']}",
        "database": row["database"],
        "host": row["host"],
        "version": row["version"],
    }


@server.tool(
    name="discover_schemas",
    description="List all available schemas in the connected database.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {},
    }
)
async def discover_schemas(credentials: dict[str, str]) -> dict:
    """Schemas with the number of base tables in each."""
    try:
        async with connect(credentials) as conn:
            rows = await conn.fetch(
                """
                SELECT s.schema_name,
                       (SELECT count(*) FROM information_schema.tables t
                        WHERE t.table_schema = s.schema_name
                          AND t.table_type = 'BASE TABLE'
                          AND NOT (t.table_name = ANY($2::text[]))) AS table_count
                FROM information_schema.schemata s
                WHERE NOT (s.schema_name = ANY($1::text[]))
                  AND s.schema_name NOT LIKE 'pg_temp%'
                  AND s.schema_name NOT LIKE 'pg_toast_temp%'
                ORDER BY s.schema_name
                """,
                list(_EXCLUDED_SCHEMAS),
                list(_store.BOOKKEEPING_TABLES),
            )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to discover schemas: {describe_error(exc)}")
    return {
        "success": True,
        "schemas": [{"name": r["schema_name"], "table_count": r["table_count"]} for r in rows],
    }


@server.tool(
    name="discover_tables",
    description="List all tables in a schema with their columns and types.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "schema": {
                "type": "string",
                "description": "Schema name (default: public)",
                "default": "public",
            },
        },
    }
)
async def discover_tables(credentials: dict[str, str], schema: str = "public") -> dict:
    """Base tables in a schema, each with its columns and an estimated row count."""
    schema = schema or "public"
    try:
        async with connect(credentials) as conn:
            result_tables = []
            for table_name in await _user_tables(conn, schema):
                columns = await conn.fetch(
                    """
                    SELECT column_name, data_type, is_nullable,
                           (SELECT count(*) FROM information_schema.key_column_usage k
                            WHERE k.table_schema = c.table_schema
                              AND k.table_name = c.table_name
                              AND k.column_name = c.column_name) > 0 as is_key
                    FROM information_schema.columns c
                    WHERE table_schema = $1 AND table_name = $2
                    ORDER BY ordinal_position
                    """,
                    schema,
                    table_name,
                )
                row_count = await conn.fetchval(
                    """
                    SELECT c.reltuples::bigint FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relname = $1 AND n.nspname = $2
                    """,
                    table_name,
                    schema,
                )
                result_tables.append(
                    {
                        "name": table_name,
                        "row_count": max(row_count or 0, 0),
                        "columns": [
                            {
                                "name": c["column_name"],
                                "type": c["data_type"],
                                "nullable": c["is_nullable"] == "YES",
                                "is_key": c["is_key"],
                            }
                            for c in columns
                        ],
                    }
                )
    except ConnectionProblem as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"Failed to discover tables: {describe_error(exc)}")
    return {"success": True, "schema": schema, "tables": result_tables}


# -- query -------------------------------------------------------------------


@server.tool(
    name="ask_database",
    description=(
        "Ask a natural language question about your data. Converts the question to SQL, "
        "executes it, and returns the results. Examples: 'What is the total sales by "
        "region?', 'Show me the top 10 customers', 'What is the average order value?' "
        "Uses your own model (X-Llm-Api-Key). Only a single SELECT is executed, read-only."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    credential_headers=ASK_HEADERS,
    permission_group="read_database",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Natural language question about the data",
            },
            "context": {
                "type": "string",
                "description": (
                    "Context name to use for this query. Uses training data from this context only."
                ),
            },
            "execute": {
                "type": "boolean",
                "description": "Whether to execute the SQL and return results (default: true)",
                "default": True,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return (default: 100)",
                "default": 100,
            },
        },
        "required": ["question"],
    }
)
async def ask_database(
    credentials: dict[str, str],
    question: str,
    context: str | None = None,
    execute: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Generate SQL with the caller's model from their training, optionally run it."""

    def answer(sql: str = "", results: list | None = None, error: str | None = None) -> dict:
        return {
            "success": error is None,
            "question": question,
            "sql": sql,
            "results": results or [],
            "row_count": len(results or []),
            "error": error,
        }

    try:
        key = _store.context_key(context)
        settings = llm_settings(credentials)
    except (_store.ContextNameError, LlmProblem) as exc:
        return answer(error=str(exc))
    bounded = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    try:
        async with connect(credentials) as conn:
            items = await _store.load_items(conn, key)
            sql = await _generate_sql(settings, items, question)
            if not sql:
                return answer(error="Could not generate SQL for this question")
            if sql.startswith(NOT_ALLOWED_TO_SEE_DATA):
                return answer(
                    error=(
                        "The model needs to look at the data to answer this. Train the "
                        "context with documentation or example question/SQL pairs instead."
                    )
                )
            sql = sql.strip().rstrip(";").strip()
            if not execute:
                return answer(sql=sql)
            if not sql.lower().startswith("select"):
                return answer(sql=sql, error="Only SELECT queries are allowed")
            if not _HAS_LIMIT.search(sql):
                sql = f"{sql} LIMIT {bounded}"
            async with conn.transaction(readonly=True):
                cursor = await conn.cursor(sql, timeout=QUERY_TIMEOUT_S)
                rows = await cursor.fetch(bounded, timeout=QUERY_TIMEOUT_S)
            return answer(sql=sql, results=[dict(r) for r in rows])
    except ConnectionProblem as exc:
        return answer(error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return answer(error=describe_error(exc))


async def _generate_sql(settings: dict, items: list[dict[str, Any]], question: str) -> str:
    from ._engine import build_vanna

    vanna = build_vanna(settings, items)
    return await anyio.to_thread.run_sync(vanna.generate_sql, question) or ""


_HAS_LIMIT = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


# -- training ----------------------------------------------------------------


@server.tool(
    name="train_from_schema",
    description=(
        "Automatically train Vanna from the database schema. Extracts table structures (DDL) "
        "and column information. Run this after importing new data to enable natural "
        "language queries."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="write_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "schema": {
                "type": "string",
                "description": "PostgreSQL schema to extract from (default: public)",
                "default": "public",
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of specific table names to train on. If not provided, "
                    "trains on all user-imported tables."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Context name to train. Training data will be isolated to this context."
                ),
            },
        },
    }
)
async def train_from_schema(
    credentials: dict[str, str],
    schema: str = "public",
    tables: list[str] | None = None,
    context: str | None = None,
) -> dict:
    """Train on each table's DDL, foreign keys and comments, from pg_catalog."""
    schema = schema or "public"
    details: dict[str, Any] = {"schema": schema, "tables_processed": 0, "context": context}
    try:
        key = _store.context_key(context)
    except _store.ContextNameError as exc:
        return _training_result(False, 0, "schema", details, [str(exc)])

    errors: list[str] = []
    items_trained = 0
    table_names: list[str] = []
    trained_tables: list[str] = []
    relationships_trained = 0
    comments_trained = 0
    try:
        async with connect(credentials) as conn:
            table_names = await _user_tables(conn, schema, tables or None)
            if not table_names:
                return _training_result(
                    False,
                    0,
                    "schema",
                    details,
                    [f"No tables found in schema '{schema}'."],
                )
            await _store.ensure_tables(conn)
            for table_name in table_names:
                try:
                    ddl = await _get_table_ddl(conn, schema, table_name)
                    await _store.add_item(conn, key, "ddl", ddl)
                    items_trained += 1
                    for rel in await _get_table_relationships(conn, schema, table_name):
                        doc = (
                            f"The column '{table_name}.{rel['from_column']}' references "
                            f"'{rel['to_table']}.{rel['to_column']}'. "
                            f"Use JOIN {rel['to_table']} ON {table_name}.{rel['from_column']} "
                            f"= {rel['to_table']}.{rel['to_column']} to connect these tables."
                        )
                        await _store.add_item(conn, key, "documentation", doc)
                        items_trained += 1
                        relationships_trained += 1
                    comments = await _get_table_comments(conn, schema, table_name)
                    if comments["table"]:
                        await _store.add_item(
                            conn, key, "documentation", f"Table '{table_name}': {comments['table']}"
                        )
                        items_trained += 1
                        comments_trained += 1
                    for col_name, col_comment in comments["columns"].items():
                        await _store.add_item(
                            conn,
                            key,
                            "documentation",
                            f"Column '{table_name}.{col_name}': {col_comment}",
                        )
                        items_trained += 1
                        comments_trained += 1
                    trained_tables.append(table_name)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Error training on {table_name}: {describe_error(exc)}")
            if key != _store.DEFAULT_CONTEXT:
                await conn.execute(
                    f"""
                    UPDATE "{_store.CONTEXTS_TABLE}"
                    SET tables_json = $2::jsonb, trained_items = trained_items + $3,
                        updated_at = NOW()
                    WHERE name = $1
                    """,  # noqa: S608 - constant table name
                    key,
                    json.dumps(trained_tables),
                    items_trained,
                )
    except ConnectionProblem as exc:
        return _training_result(False, 0, "schema", details, [str(exc)])
    except Exception as exc:  # noqa: BLE001
        errors.append(describe_error(exc))

    details.update(
        {
            "tables_processed": len(table_names),
            "tables": trained_tables,
            "relationships_trained": relationships_trained,
            "comments_trained": comments_trained,
        }
    )
    return _training_result(not errors, items_trained, "schema", details, errors)


async def _get_table_ddl(conn: Any, schema: str, table_name: str) -> str:
    """DDL from pg_catalog: types, NOT NULL, defaults and every constraint. v1's query."""
    columns = await conn.fetch(
        """
        SELECT
            a.attname AS column_name,
            pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
            CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END AS not_null,
            CASE
                WHEN a.atthasdef THEN ' DEFAULT ' || pg_catalog.pg_get_expr(d.adbin, d.adrelid)
                ELSE ''
            END AS default_value
        FROM pg_catalog.pg_attribute a
        LEFT JOIN pg_catalog.pg_attrdef d ON a.attrelid = d.adrelid AND a.attnum = d.adnum
        WHERE a.attrelid = (
            SELECT c.oid FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = $1 AND n.nspname = $2
        )
        AND a.attnum > 0
        AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        table_name,
        schema,
    )
    col_defs = [
        f"    {c['column_name']} {c['data_type']}{c['not_null']}{c['default_value']}"
        for c in columns
    ]
    constraints = await conn.fetch(
        """
        SELECT
            con.conname AS constraint_name,
            pg_catalog.pg_get_constraintdef(con.oid, true) AS constraint_def
        FROM pg_catalog.pg_constraint con
        JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid
        JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE rel.relname = $1 AND nsp.nspname = $2
        ORDER BY
            CASE con.contype
                WHEN 'p' THEN 1
                WHEN 'u' THEN 2
                WHEN 'f' THEN 3
                WHEN 'c' THEN 4
            END
        """,
        table_name,
        schema,
    )
    for con in constraints:
        col_defs.append(f"    CONSTRAINT {con['constraint_name']} {con['constraint_def']}")
    return f"CREATE TABLE {schema}.{table_name} (\n" + ",\n".join(col_defs) + "\n);"


async def _get_table_relationships(conn: Any, schema: str, table_name: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT
            kcu.column_name AS from_column,
            ccu.table_schema AS to_schema,
            ccu.table_name AS to_table,
            ccu.column_name AS to_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = $1
            AND tc.table_name = $2
        """,
        schema,
        table_name,
    )
    return [dict(r) for r in rows]


async def _get_table_comments(conn: Any, schema: str, table_name: str) -> dict[str, Any]:
    oid = await conn.fetchval(
        """
        SELECT c.oid FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = $1 AND n.nspname = $2
        """,
        table_name,
        schema,
    )
    if oid is None:
        return {"table": None, "columns": {}}
    table_comment = await conn.fetchval("SELECT obj_description($1::oid, 'pg_class')", oid)
    column_comments = await conn.fetch(
        """
        SELECT a.attname AS column_name, col_description(a.attrelid, a.attnum) AS comment
        FROM pg_catalog.pg_attribute a
        WHERE a.attrelid = $1::oid AND a.attnum > 0 AND NOT a.attisdropped
          AND col_description(a.attrelid, a.attnum) IS NOT NULL
        """,
        oid,
    )
    return {
        "table": table_comment,
        "columns": {c["column_name"]: c["comment"] for c in column_comments},
    }


@server.tool(
    name="upload_training",
    description="""Upload a training file to improve query accuracy. Supports CSV and JSON formats.

CSV format:
type,key,value,sql
question,What is the cancellation rate?,,"SELECT..."
column,status,Booking status: active or cancelled,
term,ATS,Average Ticket Size,

JSON format:
{
  "questions": [{"question": "...", "sql": "..."}],
  "columns": {"column_name": "description"},
  "terminology": {"term": "definition"}
}""",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="write_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "file_content": {
                "type": "string",
                "description": "Training file content (CSV or JSON)",
            },
            "file_type": {
                "type": "string",
                "enum": ["csv", "json"],
                "description": "File format",
                "default": "csv",
            },
            "context": {
                "type": "string",
                "description": (
                    "Context name to train. Training data will be isolated to this context."
                ),
            },
        },
        "required": ["file_content"],
    }
)
async def upload_training(
    credentials: dict[str, str],
    file_content: str,
    file_type: str = "csv",
    context: str | None = None,
) -> dict:
    """Train from a CSV or JSON file of pairs, column notes, terms, DDL and documentation."""
    details: dict[str, Any] = {
        "questions": 0,
        "columns": 0,
        "terminology": 0,
        "ddl": 0,
        "documentation": 0,
        "context": context,
    }
    try:
        key = _store.context_key(context)
    except _store.ContextNameError as exc:
        return _training_result(False, 0, "file", details, [str(exc)])

    try:
        parsed = _parse_training_file(file_content or "", file_type or "csv")
    except Exception as exc:  # noqa: BLE001 - json/csv parse errors
        return _training_result(False, 0, "file", details, [f"Parse error: {exc}"])
    items, errors = parsed

    trained = 0
    try:
        async with connect(credentials) as conn:
            await _store.ensure_tables(conn)
            for counter, kind, content, question in items:
                try:
                    await _store.add_item(conn, key, kind, content, question)
                    trained += 1
                    details[counter] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{counter} error: {describe_error(exc)}")
    except ConnectionProblem as exc:
        return _training_result(False, 0, "file", details, [str(exc)])
    return _training_result(not errors, trained, "file", details, errors)


def _parse_training_file(
    file_content: str, file_type: str
) -> tuple[list[tuple[str, str, str, str | None]], list[str]]:
    """(counter, kind, content, question) items in v1's formats, plus per-row errors."""
    import csv
    import io

    items: list[tuple[str, str, str, str | None]] = []
    errors: list[str] = []
    if file_type.lower() == "json":
        data = json.loads(file_content)
        if not isinstance(data, dict):
            raise ValueError("a JSON training file is an object")
        for qa in data.get("questions", []) or data.get("question_sql_pairs", []):
            if isinstance(qa, dict) and qa.get("question") and qa.get("sql"):
                items.append(("questions", "sql", str(qa["sql"]), str(qa["question"])))
        for entry in data.get("ddl", []):
            content = entry if isinstance(entry, str) else (entry or {}).get("content", "")
            if content:
                items.append(("ddl", "ddl", str(content), None))
        for entry in data.get("documentation", []):
            content = entry if isinstance(entry, str) else (entry or {}).get("content", "")
            if content:
                items.append(("documentation", "documentation", str(content), None))
        for col_name, description in (data.get("columns") or {}).items():
            items.append(("columns", "documentation", f"Column '{col_name}': {description}", None))
        for term, definition in (data.get("terminology") or {}).items():
            items.append(
                (
                    "terminology",
                    "documentation",
                    f"Business term '{term}' means: {definition}",
                    None,
                )
            )
        return items, errors

    for row in csv.DictReader(io.StringIO(file_content)):
        row_type = (row.get("type") or "").lower()
        key = row.get("key") or ""
        if row_type == "question":
            sql = row.get("sql") or ""
            if key and sql:
                items.append(("questions", "sql", sql, key))
        elif row_type == "column":
            value = row.get("value") or ""
            if key and value:
                items.append(("columns", "documentation", f"Column '{key}': {value}", None))
        elif row_type == "term":
            value = row.get("value") or ""
            if key and value:
                items.append(
                    ("terminology", "documentation", f"Business term '{key}' means: {value}", None)
                )
    return items, errors


@server.tool(
    name="add_training_pair",
    description=(
        "Add a single question/SQL training pair. Use this when a generated query is "
        "confirmed as correct to improve future accuracy."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="write_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Natural language question",
            },
            "sql": {
                "type": "string",
                "description": "Corresponding SQL query",
            },
            "context": {
                "type": "string",
                "description": "Context name to add training to.",
            },
        },
        "required": ["question", "sql"],
    }
)
async def add_training_pair(
    credentials: dict[str, str], question: str, sql: str, context: str | None = None
) -> dict:
    """Store one confirmed question/SQL pair."""
    details = {"question": question, "context": context}
    try:
        key = _store.context_key(context)
        if not (question or "").strip() or not (sql or "").strip():
            raise ValueError("question and sql are both required")
    except ValueError as exc:
        return _pair_failure(details, str(exc))
    try:
        async with connect(credentials) as conn:
            await _store.ensure_tables(conn)
            await _store.add_item(conn, key, "sql", sql, question)
    except ConnectionProblem as exc:
        return _pair_failure(details, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _pair_failure(details, describe_error(exc))
    return {"success": True, "items_trained": 1, "details": details, "errors": []}


def _pair_failure(details: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "success": False,
        "items_trained": 0,
        "details": details,
        "errors": [message],
        "error": message,
    }


@server.tool(
    name="get_training_status",
    description="Get the current training data status and sample entries.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=DB_HEADERS,
    permission_group="read_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": (
                    "Context name to get status for. If not provided, uses default context."
                ),
            },
        },
    }
)
async def get_training_status(credentials: dict[str, str], context: str | None = None) -> dict:
    """How many items a context holds, and the ten newest."""
    try:
        key = _store.context_key(context)
        async with connect(credentials) as conn:
            count = await _store.count_items(conn, key)
            sample = (await _store.load_items(conn, key))[:10]
    except (ConnectionProblem, _store.ContextNameError) as exc:
        return {"success": False, "count": 0, "error": str(exc), "context": context}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "count": 0, "error": describe_error(exc), "context": context}
    return {
        "success": True,
        "count": count,
        "sample": [_store.as_training_data(i) for i in sample],
        "context": context,
    }


@server.tool(
    name="clear_training",
    description=("Clear all training data. Use with caution - this removes all learned patterns."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=DB_HEADERS,
    permission_group="write_training",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": ("Context name to clear. If not provided, clears default context."),
            },
        },
    }
)
async def clear_training(credentials: dict[str, str], context: str | None = None) -> dict:
    """Delete every item trained into one context."""
    try:
        key = _store.context_key(context)
        async with connect(credentials) as conn:
            removed = await _store.clear_items(conn, key)
            if key != _store.DEFAULT_CONTEXT and await _store.table_exists(
                conn, _store.CONTEXTS_TABLE
            ):
                await conn.execute(
                    f"""
                    UPDATE "{_store.CONTEXTS_TABLE}" SET trained_items = 0, updated_at = NOW()
                    WHERE name = $1
                    """,  # noqa: S608 - constant table name
                    key,
                )
    except (ConnectionProblem, _store.ContextNameError) as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(describe_error(exc))
    return {
        "success": True,
        "message": f"Training data cleared for context: {context or 'default'}",
        "removed": removed,
    }
