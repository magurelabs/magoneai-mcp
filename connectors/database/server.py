"""The database connector.

Ported from the v1 mixin form by a codemod.

8 tools, 3 of them read-only. Every input schema is carried across verbatim
via `declare_schema` rather than derived from type hints, so the wire
contract is byte-identical to v1 and no live agent sees a changed parameter
schema.

The drivers live in the `connectors/` package beside this file, unchanged
from v1 but for import paths. An earlier form of this module carried a
flattened copy of every driver above the tools, about 2,400 lines, and the
copy was what the tools actually resolved against. It shadowed `time` with
`datetime.time` and replaced the structlog logger with a stdlib one, so all
eight tools failed on every call for every driver. The tools now import the
package, which is also what v1 did.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

from .connectors import create_connector
from .connectors.errors import sanitize_db_error
from .connectors.pool import close_all_pools
from .connectors.sql_common import is_destructive_sql
from .tools.connection import ALL_DB_CREDENTIAL_HEADERS, resolve_connection_string

SERVER_NAME = "database"
SERVER_VERSION = "2.0.0"

logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)

# v1 closed cached pools in `@app.on_event("shutdown")`.
server.on_shutdown(close_all_pools)

# From v1 `tools/query.py`: whether a query already bounds its own row count.
_HAS_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
_HAS_FETCH_RE = re.compile(r"\bFETCH\s+(NEXT|FIRST)\b", re.IGNORECASE)
_HAS_TOP_RE = re.compile(r"\bSELECT\s+TOP\s*[\s(]", re.IGNORECASE)

DEFAULT_ROW_LIMIT = 100
ABSOLUTE_MAX_ROWS = 10000

CREDENTIAL_HEADERS = ALL_DB_CREDENTIAL_HEADERS


def _get_row_limit(credentials: Dict[str, str]) -> int:
    """Extract the configured row limit from credentials, clamped to safe bounds."""
    raw = credentials.get("db_query_row_limit", "")
    try:
        limit = int(raw)
    except (ValueError, TypeError):
        return DEFAULT_ROW_LIMIT
    return min(max(1, limit), ABSOLUTE_MAX_ROWS)


# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: connector.create_table()
@server.tool(
    name='db_create_table',
    description="Create a new table in the database. Provide the table name and a 'columns' array of objects. Each object must have 'name' (string) and 'type' (string), and optionally 'primary_key' (boolean), 'nullable' (boolean), 'default' (string). Example: columns=[{\"name\": \"id\", \"type\": \"SERIAL\", \"primary_key\": true}, {\"name\": \"email\", \"type\": \"VARCHAR(255)\", \"nullable\": false}]. Do NOT pass columns as plain text or comma-separated strings.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'table': {'type': 'string',
                              'description': 'Name of the table to create'},
                    'columns': {'type': 'array',
                                'description': 'Column definitions',
                                'items': {'type': 'object',
                                          'properties': {'name': {'type': 'string',
                                                                  'description': 'Column '
                                                                                 'name'},
                                                         'type': {'type': 'string',
                                                                  'description': 'Column '
                                                                                 'type: '
                                                                                 'TEXT, '
                                                                                 'INTEGER, '
                                                                                 'BIGINT, '
                                                                                 'SERIAL, '
                                                                                 'VARCHAR(n), '
                                                                                 'BOOLEAN, '
                                                                                 'TIMESTAMP, '
                                                                                 'NUMERIC, '
                                                                                 'JSONB'},
                                                         'primary_key': {'type': 'boolean',
                                                                         'description': 'Is '
                                                                                        'this '
                                                                                        'the '
                                                                                        'primary '
                                                                                        'key?',
                                                                         'default': False},
                                                         'nullable': {'type': 'boolean',
                                                                      'description': 'Allow '
                                                                                     'NULL '
                                                                                     'values?',
                                                                      'default': True},
                                                         'default': {'type': 'string',
                                                                     'description': 'Default '
                                                                                    'value '
                                                                                    'for '
                                                                                    'the '
                                                                                    'column'}},
                                          'required': ['name', 'type']}}},
     'required': ['table', 'columns']})
async def db_create_table(credentials: Dict[str, str],
    table: str,
    columns: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Create a new table with the given column definitions."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    if not columns:
        return {"success": False, "error": "Column definitions are required"}

    try:
        connector = await create_connector(conn_str or "")
        result = await connector.create_table(table, columns)
        return {"success": True, **result}
    except Exception as e:
        logger.exception("db_create_table_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}



# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: connector.drop_table()
@server.tool(
    name='db_drop_table',
    description="Drop (delete) a table and all its data. This action is irreversible. Use with caution.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'table': {'type': 'string', 'description': 'Name of the table to drop'},
                    'confirm': {'type': 'boolean',
                                'description': 'Must be true to confirm the drop'}},
     'required': ['table', 'confirm']})
async def db_drop_table(credentials: Dict[str, str],
    table: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Drop a table. Requires confirm=true as safety check."""
    if not confirm:
        return {"success": False, "error": "Set confirm=true to drop the table"}

    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    try:
        connector = await create_connector(conn_str or "")
        result = await connector.drop_table(table)
        return {"success": True, **result}
    except Exception as e:
        logger.exception("db_drop_table_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}



# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: connector.insert()
@server.tool(
    name='db_insert',
    description="Insert one or more records into a database table/collection. Use the 'data' parameter (NOT 'records') — pass a single object or an array of objects. Returns the count and IDs of inserted records.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'table': {'type': 'string', 'description': 'Table or collection name'},
                    'data': {'type': 'array',
                             'description': 'Records to insert. Each item is an object '
                                            'with column names as keys. IMPORTANT: use '
                                            "'data', not 'records'.",
                             'items': {'type': 'object',
                                       'description': 'A record with column-name keys and '
                                                      'their values'}},
                    'records': {'type': 'array',
                                'description': "Alias for 'data'. Prefer using 'data'.",
                                'items': {'type': 'object'}}},
     'required': ['table']})
@declare_output_schema({'properties': {'ids': {'description': 'IDs of inserted records',
                                               'items': {'type': 'string'},
                                               'type': 'array'},
                                       'inserted_count': {'description': 'Number of records inserted',
                                                          'type': 'integer'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def db_insert(credentials: Dict[str, str],
    table: str,
    data: Dict[str, Any] | List[Dict[str, Any]] | None = None,
    records: Dict[str, Any] | List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Insert records into a table or collection."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    payload = data or records
    if not payload:
        return {"success": False, "error": "No data provided to insert (use 'data' parameter)"}

    try:
        connector = await create_connector(conn_str or "")
        result = await connector.insert(table, payload)
        return {"success": True, **result}
    except Exception as e:
        logger.exception("db_insert_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}



# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: connector.get_tables()
@server.tool(
    name='db_list_tables',
    description="List all tables (SQL) or collections (MongoDB) in the database. Use this to discover what data is available before querying.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
async def db_list_tables(credentials: Dict[str, str]) -> Dict[str, Any]:
    """List all tables or collections."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    try:
        connector = await create_connector(conn_str or "")
        tables = await connector.get_tables()
        return {"success": True, "tables": tables, "count": len(tables)}
    except Exception as e:
        logger.exception("db_list_tables_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}



# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: connector.query() with caller-supplied SQL, guarded by
# is_destructive_sql(). Deliberately NOT read-only: the guard is a
# denylist, and a denylist is not proof. The bar for marking a tool
# read-only is proof; doubt leaves it a write.
@server.tool(
    name='db_query',
    description="Execute a read-only database query. For SQL databases, provide a parameterized SQL query. PostgreSQL uses $1, $2 placeholders; MySQL/Oracle use %s or positional params. For MongoDB, provide a JSON string with collection and filter. Do NOT add LIMIT or FETCH clauses — row limits are applied automatically. Returns rows/documents.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': "SQL query (e.g. 'SELECT * FROM leads WHERE "
                                             "phone = $1') or MongoDB JSON (e.g. "
                                             '\'{"collection": "leads", "filter": '
                                             '{"phone": "123"}}\')'},
                    'params': {'type': 'array',
                               'items': {},
                               'description': 'Query parameters for parameterized queries '
                                              '(SQL only)',
                               'default': []}},
     'required': ['query']})
@declare_output_schema({'properties': {'row_count': {'description': 'Number of rows returned', 'type': 'integer'},
                                       'rows': {'description': 'Query result rows',
                                                'items': {'type': 'object'},
                                                'type': 'array'},
                                       'success': {'type': 'boolean'},
                                       'truncated': {'description': 'True if results were truncated by row limit',
                                                     'type': 'boolean'}},
                        'type': 'object'})
async def db_query(credentials: Dict[str, str],
    query: str,
    params: list[Any] | None = None,
) -> Dict[str, Any]:
    """Execute a read query against the database."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    if not query.strip().startswith("{") and is_destructive_sql(query):
        return {
            "success": False,
            "error": "db_query is read-only. Use db_insert or db_update for write operations.",
        }

    row_limit = _get_row_limit(credentials)
    effective_query = query
    is_sql = not query.strip().startswith("{")
    if is_sql:
        scheme = (conn_str or "").split("://")[0].lower()
        is_oracle = scheme in ("oracle", "oracle+thin", "oracle+thick")
        is_mssql = scheme == "mssql"
        if is_oracle:
            if not _HAS_FETCH_RE.search(query) and not _HAS_LIMIT_RE.search(query):
                effective_query = (
                    f"{query.rstrip().rstrip(';')} FETCH FIRST {row_limit + 1} ROWS ONLY"
                )
        elif is_mssql:
            if not _HAS_TOP_RE.search(query) and not _HAS_FETCH_RE.search(query):
                effective_query = re.sub(
                    r"\bSELECT\b",
                    f"SELECT TOP ({row_limit + 1})",
                    query,
                    count=1,
                    flags=re.IGNORECASE,
                )
        elif not _HAS_LIMIT_RE.search(query):
            effective_query = f"{query.rstrip().rstrip(';')} LIMIT {row_limit + 1}"

    try:
        connector = await create_connector(conn_str or "")
        rows = await connector.query(effective_query, params)
        truncated = len(rows) > row_limit
        if truncated:
            rows = rows[:row_limit]
        result: Dict[str, Any] = {
            "success": True,
            "rows": rows,
            "row_count": len(rows),
        }
        if truncated:
            result["truncated"] = True
        return result
    except Exception as e:
        logger.exception("db_query_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}






# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False.
# connector.get_schema(table) takes no caller SQL. Each backend runs a
# fixed metadata read: information_schema.columns (Postgres), DESCRIBE
# (MySQL), INFORMATION_SCHEMA plus an explicit rollback (MSSQL),
# user_tab_columns (Oracle), find_one (Mongo).
@server.tool(
    name='db_schema',
    description="Get the schema (columns/fields) of a specific table or collection. Returns column names, types, nullable status, and primary key info.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'table': {'type': 'string',
                              'description': 'Table or collection name to inspect'}},
     'required': ['table']})
async def db_schema(credentials: Dict[str, str], table: str) -> Dict[str, Any]:
    """Get column/field definitions for a table or collection."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    try:
        connector = await create_connector(conn_str or "")
        columns = await connector.get_schema(table)
        return {"success": True, "table": table, "columns": columns}
    except Exception as e:
        logger.exception("db_schema_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}



# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False.
# connector.search(table, filters, ...) builds a parameterised SELECT.
# The caller supplies a table and filter values, never a SQL verb, so
# unlike db_query there is no statement to guard. db_query stays a write:
# it forwards caller SQL behind a denylist, and a denylist is not proof.
@server.tool(
    name='db_search',
    description="Search database records using filters (no raw SQL needed). Supports operators: exact match (default), __contains, __gt, __lt, __gte, __lte, __in, __starts_with, __ends_with, __is_null, __ne. Example: {\"status\": \"active\", \"amount__gt\": 1000}. Pass a list for any field: {\"status\": [\"active\", \"pending\"]} → IN, {\"status__ne\": [\"a\", \"b\"]} → NOT IN.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'table': {'type': 'string',
                              'description': 'Table or collection name to search'},
                    'filters': {'type': 'object',
                                'description': 'Filter conditions as key-value pairs. Use '
                                               'operator suffixes for advanced filters: '
                                               "phone='123' (exact), "
                                               "name__contains='sharma', amount__gt=1000, "
                                               "status__in=['active','pending']. Pass a "
                                               'list as a value to match multiple values: '
                                               '{"status": ["active", "pending"]} works '
                                               'like __in; {"status__ne": ["a", "b"]} '
                                               'works like NOT IN. Invalid column names or '
                                               'type mismatches raise an error.',
                                'default': {}},
                    'limit': {'type': 'integer',
                              'description': 'Max rows to return (default: configured '
                                             'db_query_row_limit)'},
                    'offset': {'type': 'integer',
                               'description': 'Number of rows to skip for pagination',
                               'default': 0},
                    'order_by': {'type': 'string',
                                 'description': "Column to sort by. Prefix with '-' for "
                                                'descending (MongoDB only)'}},
     'required': ['table']})
@declare_output_schema({'properties': {'row_count': {'description': 'Number of matching records', 'type': 'integer'},
                                       'rows': {'description': 'Matching records',
                                                'items': {'type': 'object'},
                                                'type': 'array'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def db_search(credentials: Dict[str, str],
    table: str,
    filters: Dict[str, Any] | None = None,
    limit: int | None = None,
    offset: int = 0,
    order_by: str | None = None,
) -> Dict[str, Any]:
    """Search records using filters with operator support."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    row_limit = _get_row_limit(credentials)
    effective_limit = min(limit, row_limit) if limit is not None else row_limit

    try:
        connector = await create_connector(conn_str or "")
        rows = await connector.search(
            table=table,
            filters=filters or {},
            limit=effective_limit,
            offset=offset,
            order_by=order_by,
        )
        return {"success": True, "rows": rows, "row_count": len(rows)}
    except Exception as e:
        logger.exception("db_search_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}



# credential_headers: ['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: UPDATE "t" SET ... on every SQL backend, update_many with
# $set on Mongo. Overwriting prior values is the tool's purpose.
# Evidence: connector.update()
@server.tool(
    name='db_update',
    description="Update records in a database table/collection that match the given filters. Filters are required to prevent accidental full-table updates. Supports the same filter operators as db_search.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Db-Display-Name', 'X-Db-Type', 'X-Connection-Type', 'X-Db-Connection-String', 'X-Db-Host', 'X-Db-Port', 'X-Db-Name', 'X-Db-Username', 'X-Db-Password', 'X-Db-Trust-Server-Certificate', 'X-Oracle-Tns-Alias', 'X-Oracle-Tns-User', 'X-Oracle-Tns-Password', 'X-Oracle-Wallet-Alias', 'X-Oracle-Wallet-User', 'X-Oracle-Wallet-Db-Password', 'X-Oracle-Wallet', 'X-Oracle-Wallet-Password', 'X-Db-Query-Row-Limit'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'table': {'type': 'string', 'description': 'Table or collection name'},
                    'filters': {'type': 'object',
                                'description': 'Filter to identify records to update. Same '
                                               'syntax as db_search filters. Example: '
                                               '{"id": 42} or {"phone": "9876543210"}'},
                    'data': {'type': 'object',
                             'description': 'Fields to update with new values. Example: '
                                            '{"status": "contacted", "last_contact_date": '
                                            '"2026-02-14"}'}},
     'required': ['table', 'filters', 'data']})
@declare_output_schema({'properties': {'success': {'type': 'boolean'},
                                       'updated_count': {'description': 'Number of records updated',
                                                         'type': 'integer'}},
                        'type': 'object'})
async def db_update(credentials: Dict[str, str],
    table: str,
    filters: Dict[str, Any] | None = None,
    data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Update records matching filters."""
    conn_str, conn_error = resolve_connection_string(credentials)
    if conn_error:
        return {"success": False, "error": conn_error}

    if not filters:
        return {"success": False, "error": "Filters are required for update operations"}

    if not data:
        return {"success": False, "error": "No data provided to update"}

    try:
        connector = await create_connector(conn_str or "")
        result = await connector.update(table, filters, data)
        return {"success": True, **result}
    except Exception as e:
        logger.exception("db_update_error", error=str(e))
        return {"success": False, "error": sanitize_db_error(e)}
