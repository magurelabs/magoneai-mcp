"""The trino connector.

Ported from the v1 mixin form by a codemod.

5 tools, 4 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

# Restored after the port: an optional dependency whose try/except the port dropped.

try:
    import trino
    from trino.dbapi import connect as trino_connect
except ImportError:
    trino = None
    trino_connect = None

SERVER_NAME = "trino"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

from .connectors.trino_connector import TrinoConnector, TrinoQueryTimeout
import asyncio
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from .connectors.trino_connector import (
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    MAX_QUERY_TIMEOUT_SECONDS,
)
import sys
from pathlib import Path
from typing import Any, Dict
from .tools.common import (
    ABSOLUTE_MAX_ROWS,
    CREDENTIAL_HEADERS,
    DEFAULT_ROW_LIMIT,
    METADATA_KEYWORDS,
)
from typing import Optional
from typing import Any

__all__ = ["TrinoConnector", "TrinoQueryTimeout"]

DEFAULT_QUERY_TIMEOUT_SECONDS = 60

MAX_QUERY_TIMEOUT_SECONDS = 300

_MAX_CACHED_CONNECTIONS = 20

_CONNECTION_TTL_SECONDS = 300

_connections: Dict[Tuple, Tuple[Any, threading.Lock, float]] = {}

_cache_lock = threading.Lock()


















CREDENTIAL_HEADERS = [
    "X-Trino-Host",
    "X-Trino-Port",
    "X-Trino-User",
    "X-Trino-Password",
    "X-Trino-Catalog",
    "X-Trino-Schema",
    "X-Trino-Http-Scheme",
    "X-Trino-Allowed-Catalogs",
    "X-Trino-Query-Timeout",
]

DEFAULT_ROW_LIMIT = 100

ABSOLUTE_MAX_ROWS = 5000

MAX_RESPONSE_BYTES = 256_000

METADATA_KEYWORDS = frozenset({"SHOW", "DESCRIBE", "DESC"})

_METADATA_START_RE = re.compile(r"^\s*\(*\s*(SHOW|DESCRIBE|DESC)\b", re.IGNORECASE)

_HAS_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\s*$", re.IGNORECASE)

_HAS_FETCH_RE = re.compile(r"\bFETCH\s+(NEXT|FIRST)\b", re.IGNORECASE)

_PART = r'(?:"(?:[^"]|"")*"|[A-Za-z_]\w*)'

_QUALIFIED_RE = re.compile(rf"({_PART})\s*\.\s*({_PART})\s*\.\s*({_PART})")

def is_metadata_statement(sql: str) -> bool:
    """True for SHOW/DESCRIBE, which take no LIMIT and must not be wrapped."""
    return bool(_METADATA_START_RE.match(sql))

def clamp_row_limit(row_limit: Optional[int]) -> int:
    if row_limit is None:
        return DEFAULT_ROW_LIMIT
    try:
        value = int(row_limit)
    except (TypeError, ValueError):
        return DEFAULT_ROW_LIMIT
    return min(max(1, value), ABSOLUTE_MAX_ROWS)

def query_timeout(credentials: Dict[str, str]) -> float:
    raw = (credentials.get("trino_query_timeout") or "").strip()
    if not raw:
        return float(DEFAULT_QUERY_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_QUERY_TIMEOUT_SECONDS)
    return min(max(1.0, value), float(MAX_QUERY_TIMEOUT_SECONDS))

def apply_row_limit(sql: str, row_limit: int) -> str:
    """Append ``LIMIT n+1`` to a statement that does not already bound itself.

    Appending rather than wrapping in ``SELECT * FROM (...)`` is what keeps a
    top-level ORDER BY meaningful: Trino does not guarantee the ordering of a
    subquery, so a wrapper silently returns an arbitrary n rows for any sorted
    query. Metadata statements take no LIMIT at all and are truncated in Python.
    """
    body = sql.rstrip().rstrip(";").rstrip()
    if is_metadata_statement(body):
        return body
    if _HAS_LIMIT_RE.search(body) or _HAS_FETCH_RE.search(body):
        return body
    return f"{body} LIMIT {row_limit + 1}"

def _unquote(part: str) -> str:
    if part.startswith('"') and part.endswith('"') and len(part) >= 2:
        return part[1:-1].replace('""', '"')
    return part

def allowed_catalogs(credentials: Dict[str, str]) -> Optional[Set[str]]:
    """Catalogs this connection may touch, or None when unrestricted."""
    raw = (credentials.get("trino_allowed_catalogs") or "").strip()
    if not raw:
        return None
    names = {n.strip() for n in raw.split(",") if n.strip()}
    return names or None

def catalog_error(credentials: Dict[str, str], catalog: str) -> Optional[str]:
    """Error message when ``catalog`` is outside the allowlist, else None."""
    allowed = allowed_catalogs(credentials)
    if allowed is None or catalog in allowed:
        return None
    return (
        f"Catalog '{catalog}' is not available on this connection. "
        f"Allowed: {', '.join(sorted(allowed))}."
    )

def sql_catalog_error(credentials: Dict[str, str], sql: str) -> Optional[str]:
    """Best-effort allowlist check for the catalogs a statement references.

    Matches ``catalog.schema.table`` after literals are stripped, so a name
    inside a string cannot trigger it. This is defence in depth, not a security
    boundary — a query can reach data by other routes (connector pass-through
    functions in particular). Restricting the Trino user server-side is the real
    control; see the README.
    """
    allowed = allowed_catalogs(credentials)
    if allowed is None:
        return None

    body = strip_sql_literals(sql)
    referenced = {_unquote(m.group(1)) for m in _QUALIFIED_RE.finditer(body)}
    default_catalog = (credentials.get("trino_catalog") or "").strip()
    if default_catalog:
        referenced.add(default_catalog)

    denied = sorted(c for c in referenced if c not in allowed)
    if denied:
        return (
            f"Query references catalog(s) not available on this connection: "
            f"{', '.join(denied)}. Allowed: {', '.join(sorted(allowed))}."
        )
    return None

def cap_response_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Drop trailing rows until the serialized payload fits MAX_RESPONSE_BYTES."""
    if not rows:
        return rows, False
    if len(json.dumps(rows, default=str)) <= MAX_RESPONSE_BYTES:
        return rows, False

    low, high = 0, len(rows)
    while low < high:
        mid = (low + high + 1) // 2
        if len(json.dumps(rows[:mid], default=str)) <= MAX_RESPONSE_BYTES:
            low = mid
        else:
            high = mid - 1
    return rows[:low], True

_METADATA_GROUP = "read_trino_metadata"

def _failure(logger: Any, event: str, exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, TrinoQueryTimeout):
        logger.warning(f"{event}_timeout", error=str(exc))
        return {"success": False, "error": str(exc)}
    logger.exception(event, error=str(exc))
    return {"success": False, "error": sanitize_trino_error(exc)}

_URI_CREDS_RE = re.compile(r"://[^@/\s]+@")

_FILE_PATH_RE = re.compile(r"(/[^\s:]+/[\w.\-]+\.py\b[:\d]*)")

_DRIVER_PREFIX_RE = re.compile(r"^[\w.]+Error:\s*")

_TRINO_MESSAGE_RE = re.compile(r'message="((?:[^"\\]|\\.)*)"')

_TRINO_QUERY_ID_RE = re.compile(r",?\s*query_id=[\w-]+\)?\s*$")

_TRINO_WRAPPER_RE = re.compile(r"^\w*Trino\w*Error\((?:type|name)=")

def redact(msg: str) -> str:
    """Remove credential URIs and absolute file paths from a message."""
    msg = _URI_CREDS_RE.sub("://***@", msg)
    return _FILE_PATH_RE.sub("<path>", msg)


def sanitize_trino_error(exc: BaseException) -> str:
    """Safe message for Trino client exceptions.

    Prefers the driver's own ``message`` attribute over its ``repr``; falls back
    to extracting it from the repr, then to the generic sanitizer. The internal
    ``query_id`` is dropped either way — it means nothing to the caller and
    identifies the cluster's execution history.
    """
    message: Optional[str] = getattr(exc, "message", None)
    if not isinstance(message, str) or not message.strip():
        match = _TRINO_MESSAGE_RE.search(str(exc))
        if match:
            message = match.group(1).replace('\\"', '"')

    if isinstance(message, str) and message.strip():
        name = getattr(exc, "error_name", None)
        prefix = f"{name}: " if isinstance(name, str) and name else ""
        return redact(f"{prefix}{message}").strip()

    text = _TRINO_QUERY_ID_RE.sub("", str(exc)).strip()
    if _TRINO_WRAPPER_RE.match(text):
        text = text[text.index("(") + 1 :].rstrip(")")
    cleaned = _DRIVER_PREFIX_RE.sub("", redact(text)).strip()
    return cleaned or "Unknown Trino error"

_IDENTIFIER_RE = re.compile(r"[^\w.]")

_QUOTE_RE = re.compile(r'"')

_NESTED_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|REPLACE|TRUNCATE|DROP|CREATE|ALTER"
    r"|RENAME|GRANT|REVOKE|VACUUM|FLASHBACK|PURGE)\b",
    re.IGNORECASE,
)

_SELECT_INTO_RE = re.compile(r"\bINTO\b", re.IGNORECASE)

_FIRST_WORD_RE = re.compile(r"[(\s]*(\w+)")

def strip_sql_literals(sql: str) -> str:
    """Blank out comments and quoted text so keyword checks see only SQL structure.

    A single left-to-right pass, not sequential regex passes: quotes and comments
    can each contain the other's delimiters (``'-- '`` inside a literal, a quote
    inside a comment), so whichever opens first must win. Stripping comments
    first would let a ``--`` inside a string swallow its closing quote.
    """
    out: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        if char in "'\"`":  # string literal or quoted identifier
            i += 1
            while i < length:
                if sql[i] == char:
                    if i + 1 < length and sql[i + 1] == char:  # doubled = escaped quote
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif sql.startswith("--", i):
            while i < length and sql[i] != "\n":
                i += 1
            out.append(" ")
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = length if end == -1 else end + 2
            out.append(" ")
        else:
            out.append(char)
            i += 1
    return "".join(out)

def select_only_error(
    sql: str,
    metadata_keywords: frozenset[str] = frozenset(),
) -> str | None:
    """Return an error message unless ``sql`` is a single read-only SELECT.

    ``metadata_keywords`` additionally permits read-only metadata statements
    that an engine exposes as their own verbs (Trino's SHOW/DESCRIBE). They
    cannot execute DML, so the nested-write scan is skipped for them —
    ``SHOW CREATE TABLE t`` is read-only despite containing "CREATE". Callers
    that leave it empty get exactly the SELECT/WITH behaviour as before.

    An allowlist rather than a denylist: the statement must *start* with SELECT
    or WITH, be a single statement, and contain no nested write. Comments and
    quoted text are stripped first, so a value like 'DELETE ME' or a column
    named "delete" cannot trip the check. Returns None when the query is safe.
    """
    body = strip_sql_literals(sql).strip().rstrip(";").rstrip()
    if not body:
        return "Query is empty."
    if ";" in body:
        return "Only a single SELECT statement is allowed (remove the ';')."
    match = _FIRST_WORD_RE.match(body)
    first = (match.group(1) if match else "").upper()
    if first in metadata_keywords:
        return None
    if first not in ("SELECT", "WITH"):
        allowed = ", ".join(sorted(("SELECT", "WITH") + tuple(metadata_keywords)))
        return f"Only read-only queries are allowed ({allowed}) — '{first}' is not permitted."
    nested = _NESTED_WRITE_RE.search(body)
    if nested:
        return f"Only SELECT queries are allowed — '{nested.group(1).upper()}' is not permitted."
    if _SELECT_INTO_RE.search(body):
        return "Only SELECT queries are allowed — 'SELECT ... INTO' is not permitted."
    return None







# credential_headers: ['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout']
# permission_group: 'read_trino_metadata'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: SHOW CATALOGS (via list_catalogs())
@server.tool(
    name='trino_list_catalogs',
    description="List all available catalogs in Trino. Each catalog represents a connected data source (e.g. postgresql, hive, s3). Use this first to discover what data sources are available.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout'],
    permission_group='read_trino_metadata',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'catalogs': {'items': {'type': 'string'}, 'type': 'array'},
                                       'count': {'type': 'integer'},
                                       'error': {'type': 'string'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def trino_list_catalogs(credentials: Dict[str, str]) -> Dict[str, Any]:
    connector = TrinoConnector()
    try:
        connector.connect(credentials)
        catalogs = await connector.list_catalogs(query_timeout(credentials))
        # Hide what this connection may not query, so the model does not
        # plan around a catalog it will be refused.
        allowed = allowed_catalogs(credentials)
        if allowed is not None:
            catalogs = [c for c in catalogs if c in allowed]
        return {"success": True, "catalogs": catalogs, "count": len(catalogs)}
    except Exception as e:
        return _failure(logger, "trino_list_catalogs_error", e)
    finally:
        connector.close()



# credential_headers: ['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout']
# permission_group: 'read_trino_metadata'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: SHOW SCHEMAS FROM (via list_schemas())
@server.tool(
    name='trino_list_schemas',
    description="List all schemas inside a Trino catalog. A schema is a logical grouping of tables within a catalog. Example: catalog='postgresql' → schemas like 'public', 'sales'",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout'],
    permission_group='read_trino_metadata',
)
@declare_schema({'type': 'object',
     'properties': {'catalog': {'type': 'string',
                                'description': 'Catalog name to list schemas from (e.g. '
                                               'hive, postgresql)'}},
     'required': ['catalog']})
@declare_output_schema({'properties': {'catalog': {'type': 'string'},
                                       'count': {'type': 'integer'},
                                       'error': {'type': 'string'},
                                       'schemas': {'items': {'type': 'string'}, 'type': 'array'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def trino_list_schemas(credentials: Dict[str, str], catalog: str) -> Dict[str, Any]:
    denied = catalog_error(credentials, catalog)
    if denied:
        return {"success": False, "error": denied}

    connector = TrinoConnector()
    try:
        connector.connect(credentials)
        schemas = await connector.list_schemas(catalog, query_timeout(credentials))
        return {"success": True, "catalog": catalog, "schemas": schemas, "count": len(schemas)}
    except Exception as e:
        return _failure(logger, "trino_list_schemas_error", e)
    finally:
        connector.close()



# credential_headers: ['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout']
# permission_group: 'read_trino_metadata'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: SHOW TABLES FROM (via list_tables())
@server.tool(
    name='trino_list_tables',
    description="List all tables inside a Trino catalog and schema. Use trino_list_catalogs and trino_list_schemas first to find the right catalog/schema. Example: catalog='hive', schema='sales' → tables like 'orders', 'customers'",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout'],
    permission_group='read_trino_metadata',
)
@declare_schema({'type': 'object',
     'properties': {'catalog': {'type': 'string',
                                'description': 'Catalog name (e.g. hive, postgresql)'},
                    'schema': {'type': 'string',
                               'description': 'Schema name (e.g. public, sales)'}},
     'required': ['catalog', 'schema']})
@declare_output_schema({'properties': {'catalog': {'type': 'string'},
                                       'count': {'type': 'integer'},
                                       'error': {'type': 'string'},
                                       'schema': {'type': 'string'},
                                       'success': {'type': 'boolean'},
                                       'tables': {'items': {'type': 'string'}, 'type': 'array'}},
                        'type': 'object'})
async def trino_list_tables(credentials: Dict[str, str], catalog: str, schema: str
) -> Dict[str, Any]:
    denied = catalog_error(credentials, catalog)
    if denied:
        return {"success": False, "error": denied}

    connector = TrinoConnector()
    try:
        connector.connect(credentials)
        tables = await connector.list_tables(catalog, schema, query_timeout(credentials))
        return {
            "success": True,
            "catalog": catalog,
            "schema": schema,
            "tables": tables,
            "count": len(tables),
        }
    except Exception as e:
        return _failure(logger, "trino_list_tables_error", e)
    finally:
        connector.close()



# credential_headers: ['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout']
# permission_group: 'read_trino_data'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: CREATE TABLE (via select_only_error())
@server.tool(
    name='trino_query',
    description="Execute a read-only SQL query on Trino. Trino can query data across multiple sources (PostgreSQL, S3, Hive, etc.) in a single SQL statement. Always qualify table names as catalog.schema.table. Example: SELECT * FROM hive.sales.orders WHERE region = 'APAC'. ORDER BY is honoured, so ask for the ordering you want rather than relying on the order rows come back in. Do NOT add LIMIT clauses — row limits are applied automatically.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout'],
    permission_group='read_trino_data',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': 'SQL query to run on Trino. Use fully '
                                             'qualified names: catalog.schema.table'},
                    'row_limit': {'type': 'integer',
                                  'description': 'Max rows to return (1–5000, default 100)',
                                  'default': 100}},
     'required': ['query']})
@declare_output_schema({'properties': {'error': {'type': 'string'},
                                       'renamed_columns': {'description': 'Columns given a _1, _2 … suffix because '
                                                                          'the query returned the same name more than '
                                                                          'once',
                                                           'items': {'type': 'string'},
                                                           'type': 'array'},
                                       'row_count': {'description': 'Number of rows returned', 'type': 'integer'},
                                       'rows': {'description': 'Query result rows',
                                                'items': {'type': 'object'},
                                                'type': 'array'},
                                       'success': {'type': 'boolean'},
                                       'truncated': {'description': 'True if results were cut short by the row or '
                                                                    'size limit',
                                                     'type': 'boolean'}},
                        'type': 'object'})
async def trino_query(credentials: Dict[str, str],
    query: str,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> Dict[str, Any]:
    row_limit = clamp_row_limit(row_limit)

    guard_error = select_only_error(query, METADATA_KEYWORDS)
    if guard_error:
        return {"success": False, "error": guard_error}

    scope_error = sql_catalog_error(credentials, query)
    if scope_error:
        return {"success": False, "error": scope_error}

    # A SELECT is fetched with LIMIT n+1 so one surplus row reveals
    # truncation; a metadata statement takes no LIMIT and comes back whole.
    # Either way more rows than the limit means there were more to have.
    sql = apply_row_limit(query, row_limit)

    connector = TrinoConnector()
    try:
        connector.connect(credentials)
        rows, renamed = await connector.query(sql, query_timeout(credentials))

        truncated = len(rows) > row_limit
        rows = rows[:row_limit]

        rows, size_capped = cap_response_rows(rows)

        result: Dict[str, Any] = {
            "success": True,
            "rows": rows,
            "row_count": len(rows),
        }
        if truncated or size_capped:
            result["truncated"] = True
        if size_capped:
            result["truncation_reason"] = "response size limit"
        if renamed:
            result["renamed_columns"] = renamed
        return result
    except TrinoQueryTimeout as e:
        logger.warning("trino_query_timeout", error=str(e))
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("trino_query_error", error=str(e))
        return {"success": False, "error": sanitize_trino_error(e)}
    finally:
        connector.close()



# credential_headers: ['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout']
# permission_group: 'read_trino_metadata'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False.
# get_schema() runs DESCRIBE with the verb fixed in the f-string and only
# quote_qualified_name() output interpolated. trino_query stays a write:
# it forwards caller SQL, and an allowlist is still not proof.
@server.tool(
    name='trino_table_schema',
    description="Get the column definitions (name, type, nullable) for a specific Trino table. Use this before writing queries to understand the table structure. Example: catalog='hive', schema='sales', table='orders'",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Trino-Host', 'X-Trino-Port', 'X-Trino-User', 'X-Trino-Password', 'X-Trino-Catalog', 'X-Trino-Schema', 'X-Trino-Http-Scheme', 'X-Trino-Allowed-Catalogs', 'X-Trino-Query-Timeout'],
    permission_group='read_trino_metadata',
)
@declare_schema({'type': 'object',
     'properties': {'catalog': {'type': 'string', 'description': 'Catalog name'},
                    'schema': {'type': 'string', 'description': 'Schema name'},
                    'table': {'type': 'string', 'description': 'Table name to inspect'}},
     'required': ['catalog', 'schema', 'table']})
@declare_output_schema({'properties': {'catalog': {'type': 'string'},
                                       'columns': {'items': {'type': 'object'}, 'type': 'array'},
                                       'error': {'type': 'string'},
                                       'schema': {'type': 'string'},
                                       'success': {'type': 'boolean'},
                                       'table': {'type': 'string'}},
                        'type': 'object'})
async def trino_table_schema(credentials: Dict[str, str], catalog: str, schema: str, table: str
) -> Dict[str, Any]:
    denied = catalog_error(credentials, catalog)
    if denied:
        return {"success": False, "error": denied}

    connector = TrinoConnector()
    try:
        connector.connect(credentials)
        columns = await connector.get_schema(catalog, schema, table, query_timeout(credentials))
        return {
            "success": True,
            "catalog": catalog,
            "schema": schema,
            "table": table,
            "columns": columns,
        }
    except Exception as e:
        return _failure(logger, "trino_table_schema_error", e)
    finally:
        connector.close()
