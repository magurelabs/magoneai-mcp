"""Oracle Database connector using python-oracledb (sync thick mode) with FastAPI threadpool.

Requires Oracle Database 12c Release 2 (12.2) or later (for OFFSET/FETCH NEXT).
Thick mode uses Oracle Instant Client (LD_LIBRARY_PATH / ORACLE_HOME must be set).

Architecture:
- Sync oracledb connection pool (create_pool) — never create_pool_async
- All blocking DB work done in private sync _* helpers
- Public async methods delegate to sync helpers via run_in_threadpool
  so the FastAPI event loop is never blocked

Security:
- _query_db() and _search_db() call conn.rollback() after reads to prevent accidental commits
- Multi-statement queries rejected to prevent injection
"""

import base64
import binascii
import io
import logging
import os
import re
import shutil
import tempfile
import zipfile
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
from .sql_common import (
    build_where_clause,
    serialize_bind_value,
    validate_column_type,
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

_logger = logging.getLogger(__name__)

try:
    import oracledb
except ImportError:
    oracledb = None
else:
    # Fetch CLOB/BLOB values directly as str/bytes instead of LOB locator objects,
    # so read paths return JSON-serialisable values (a raw LOB is not serialisable
    # and would slip through serialize_row unchanged).
    oracledb.defaults.fetch_lobs = False
    try:
        oracledb.init_oracle_client()  # uses LD_LIBRARY_PATH / ORACLE_HOME; no-op if already initialised
    except Exception as _thick_err:
        # Log clearly — hiding this produces a cryptic create_pool error later.
        # Common causes: missing libaio, wrong architecture, bad LD_LIBRARY_PATH.
        _logger.warning(
            "Oracle thick-client (Instant Client) init failed: %s. "
            "Falling back to thin mode — set LD_LIBRARY_PATH/ORACLE_HOME to enable thick mode.",
            _thick_err,
        )

POOL_ACQUIRE_TIMEOUT_S = 30

# ---------------------------------------------------------------------------
# Module-level helpers (pure sync, no class state)
# ---------------------------------------------------------------------------


def _materialize_wallet(wallet_b64: str) -> str:
    """Decode a base64 Oracle wallet zip and extract it to a private temp dir.

    Returns the directory path (0700, containing tnsnames.ora / sqlnet.ora /
    ewallet.pem) suitable for oracledb's ``config_dir`` + ``wallet_location``.
    The caller owns the directory and must remove it via ``_cleanup_wallet_dir``.
    """
    try:
        raw = base64.b64decode(wallet_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid Oracle wallet: value is not valid base64") from exc

    wallet_dir = tempfile.mkdtemp(prefix="oracle_wallet_")
    os.chmod(wallet_dir, 0o700)
    root = os.path.realpath(wallet_dir)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for member in zf.namelist():
                # Reject absolute paths and zip-slip (../) traversal.
                dest = os.path.realpath(os.path.join(wallet_dir, member))
                if dest != root and not dest.startswith(root + os.sep):
                    raise ValueError("Invalid Oracle wallet: unsafe path in archive")
            zf.extractall(wallet_dir)
    except zipfile.BadZipFile as exc:
        _cleanup_wallet_dir(wallet_dir)
        raise ValueError("Invalid Oracle wallet: not a valid zip archive") from exc
    except Exception:
        _cleanup_wallet_dir(wallet_dir)
        raise
    return wallet_dir


def _cleanup_wallet_dir(path: str) -> None:
    """Best-effort removal of an extracted wallet directory."""
    shutil.rmtree(path, ignore_errors=True)


def _parse_tnsnames(text: str) -> dict[str, str]:
    """Map each alias in a tnsnames.ora to its full connect descriptor.

    Aliases are matched only at the top level: once an entry's descriptor is
    consumed (by balancing parentheses) the scan resumes after it, so inner
    ``key=(...)`` pairs are never mistaken for aliases.
    """
    entries: dict[str, str] = {}
    content = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    alias_re = re.compile(r"([A-Za-z0-9_.\-]+(?:\s*,\s*[A-Za-z0-9_.\-]+)*)\s*=\s*\(")
    pos = 0
    while True:
        match = alias_re.search(content, pos)
        if not match:
            return entries
        start = match.end() - 1  # the descriptor's opening '('
        depth = 0
        i = start
        while i < len(content):
            if content[i] == "(":
                depth += 1
            elif content[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            return entries  # unbalanced/truncated file — keep what we parsed
        descriptor = content[start : i + 1]
        for name in match.group(1).split(","):
            entries[name.strip().upper()] = descriptor
        pos = i + 1


def _inject_wallet_dir(descriptor: str, wallet_dir: str) -> str:
    """Bind a wallet to a connect descriptor via my_wallet_directory."""
    setting = f"(my_wallet_directory={wallet_dir})"
    match = re.search(r"\(\s*security\s*=", descriptor, re.IGNORECASE)
    if match:
        return descriptor[: match.end()] + setting + descriptor[match.end() :]
    closing = descriptor.rfind(")")  # no security clause — add one inside the descriptor
    return descriptor[:closing] + f"(security={setting})" + descriptor[closing:]


def _wallet_dsn(wallet_dir: str, alias: str) -> str:
    """Resolve a wallet alias to a self-contained connect descriptor.

    Thick mode ignores create_pool's ``config_dir``/``wallet_location``, so an
    alias from the wallet's tnsnames.ora would never resolve there (ORA-12154).
    Inlining the descriptor and pointing at the wallet with my_wallet_directory
    works in both modes, needs neither TNS_ADMIN nor an edited sqlnet.ora, and
    keeps connections independent (no process-global state to race over).
    """
    tns_path = os.path.join(wallet_dir, "tnsnames.ora")
    if not os.path.exists(tns_path):
        raise ValueError("Invalid Oracle wallet: tnsnames.ora is missing from the archive")
    with open(tns_path, encoding="utf-8", errors="replace") as handle:
        entries = _parse_tnsnames(handle.read())
    descriptor = entries.get(alias.strip().upper())
    if descriptor is None:
        available = ", ".join(sorted(entries)) or "none"
        raise ValueError(
            f"Service name '{alias}' is not in the wallet's tnsnames.ora. Available: {available}"
        )
    return _inject_wallet_dir(descriptor, wallet_dir)


def _reject_multi_statement(sql: str) -> None:
    """Reject queries containing multiple SQL statements."""
    stripped = sql.rstrip().rstrip(";").rstrip()
    if ";" in stripped:
        raise ValueError("Multi-statement queries are not allowed")


def _oid(name: str) -> str:
    """Return a safely-quoted Oracle identifier uppercased to match Oracle defaults.

    Uppercasing before quoting means "USERS" == users (case-insensitive), so tables
    created via create_table can be queried without double-quoting.
    """
    return _safe_id(name).upper()


def _rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    """Convert an executed cursor's rows to a list of serialisable dicts."""
    if cursor.description is None:
        return []
    columns = [desc[0].lower() for desc in cursor.description]
    rows = cursor.fetchall()
    return [_serialize_row(dict(zip(columns, row))) for row in rows]


# ---------------------------------------------------------------------------
# Sync DB helpers — called from threadpool, never directly in async context
# ---------------------------------------------------------------------------


def _query_db(pool: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    _reject_multi_statement(sql)
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
            rows = _rows_to_dicts(cur)
        conn.rollback()
    return rows


def _insert_db(pool: Any, table: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    columns = list(records[0].keys())
    col_str = ", ".join(f'"{_oid(c)}"' for c in columns)
    placeholders = ", ".join(f":{i + 1}" for i in range(len(columns)))
    returning_idx = len(columns) + 1
    sql = (
        f'INSERT INTO "{_oid(table)}" ({col_str}) VALUES ({placeholders}) '
        f"RETURNING ROWID INTO :{returning_idx}"
    )

    inserted_ids: list[Any] = []
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            for record in records:
                rowid_var = cur.var(oracledb.DB_TYPE_ROWID)
                values = [serialize_bind_value(record.get(c)) for c in columns] + [rowid_var]
                # Strings > 32767 bytes bind as VARCHAR by default and fail against
                # CLOB/LONG columns; declare those positions as CLOB explicitly. Reset
                # every iteration so sizes don't leak across records of differing shape.
                sizes = [
                    oracledb.DB_TYPE_CLOB
                    if isinstance(v, str) and len(v.encode("utf-8")) > 32767
                    else None
                    for v in values[:-1]
                ]
                if any(sizes):
                    cur.setinputsizes(*sizes)
                else:
                    cur.setinputsizes()
                cur.execute(sql, values)
                inserted_ids.append(rowid_var.getvalue())
        conn.commit()

    return {"inserted_count": len(records), "inserted_ids": inserted_ids}


def _update_db(
    pool: Any, table: str, filters: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    set_parts: list[str] = []
    params: list[Any] = []
    idx = 1

    for col, val in data.items():
        set_parts.append(f'"{_oid(col)}" = :{idx}')
        params.append(serialize_bind_value(val))
        idx += 1

    where_clause, where_params, _ = build_where_clause(
        filters,
        dialect="oracle",
        start_idx=idx,
    )
    params.extend(where_params)

    sql = f'UPDATE "{_oid(table)}" SET {", ".join(set_parts)} WHERE {where_clause}'
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            updated = cur.rowcount
        conn.commit()
    return {"updated_count": updated}


def _search_db(
    pool: Any,
    table: str,
    filters: dict[str, Any],
    limit: int,
    offset: int,
    order_by: str | None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    idx = 1

    if filters:
        where_clause, where_params, idx = build_where_clause(
            filters,
            dialect="oracle",
            start_idx=idx,
        )
        params.extend(where_params)
        where_sql = f"WHERE {where_clause}"
    else:
        where_sql = ""

    order_sql = f'ORDER BY "{_oid(order_by)}"' if order_by else ""
    params.extend([offset, limit])
    sql = (
        f'SELECT * FROM "{_oid(table)}" {where_sql} {order_sql} '
        f"OFFSET :{idx} ROWS FETCH NEXT :{idx + 1} ROWS ONLY"
    )

    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = _rows_to_dicts(cur)
        conn.rollback()
    return rows


def _get_tables_db(pool: Any) -> list[str]:
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
            rows = cur.fetchall()
    return [row[0].upper() for row in rows]


def _get_schema_db(pool: Any, table: str) -> list[dict[str, Any]]:
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.column_name, c.data_type, c.nullable, c.data_default, "
                "CASE WHEN p.column_name IS NOT NULL THEN 1 ELSE 0 END AS is_pk "
                "FROM user_tab_columns c "
                "LEFT JOIN ("
                "  SELECT cc.column_name FROM user_constraints uc "
                "  JOIN user_cons_columns cc ON uc.constraint_name = cc.constraint_name "
                "  WHERE uc.table_name = :1 AND uc.constraint_type = 'P'"
                ") p ON c.column_name = p.column_name "
                "WHERE c.table_name = :2 "
                "ORDER BY c.column_id",
                [table.upper(), table.upper()],
            )
            rows = cur.fetchall()
    return [
        {
            "name": row[0].lower(),
            "type": row[1],
            "nullable": row[2] == "Y",
            "default": row[3],
            "primary_key": bool(row[4]),
        }
        for row in rows
    ]


def _create_table_db(pool: Any, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    col_defs: list[str] = []
    for col in columns:
        col_type = validate_column_type(col.get("type", "VARCHAR2(255)"))
        parts = [f'"{_oid(col["name"])}"', col_type]
        if col.get("primary_key"):
            parts.append("PRIMARY KEY")
        if not col.get("nullable", True) and not col.get("primary_key"):
            parts.append("NOT NULL")
        if col.get("default") is not None:
            safe_val = _safe_default(str(col["default"]))
            parts.append(f"DEFAULT '{safe_val}'")
        col_defs.append(" ".join(parts))

    sql = f'CREATE TABLE "{_oid(table)}" ({", ".join(col_defs)})'
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(sql)
            except oracledb.DatabaseError as e:
                (error,) = e.args
                if error.code == 955:
                    # ORA-00955: name is already used by an existing object
                    return {"created": False, "table": table, "error": "Table already exists"}
                raise
        conn.commit()
    return {"created": True, "table": table}


def _drop_table_db(pool: Any, table: str) -> dict[str, Any]:
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(f'DROP TABLE "{_oid(table)}"')
            except oracledb.DatabaseError as e:
                (error,) = e.args
                if error.code == 942:
                    # ORA-00942: table or view does not exist
                    return {"dropped": False, "table": table, "error": "Table does not exist"}
                raise
        conn.commit()
    return {"dropped": True, "table": table}


def _close_pool(pool: Any) -> None:
    pool.close()


# ---------------------------------------------------------------------------
# OracleConnector — async facade over the sync pool
# ---------------------------------------------------------------------------


class OracleConnector(DatabaseConnector):
    """Oracle Database connector.

    Uses a synchronous oracledb connection pool.  All blocking operations are
    dispatched to FastAPI's threadpool via run_in_threadpool so the event loop
    is never blocked.
    """

    def __init__(self) -> None:
        self._pool: Any | None = None
        # Populated when connecting via an Autonomous Database wallet; the
        # extracted wallet dir lives for the life of the pool and is removed
        # in close(). None for non-wallet connections.
        self._wallet_dir: str | None = None

    async def connect(self, connection_string: str) -> None:
        if oracledb is None:
            raise ImportError(
                "python-oracledb is required for Oracle connections. "
                "Install it with: pip install oracledb"
            )
        parsed = urlparse(connection_string)
        query_params = parse_qs(parsed.query)
        dsn_param = query_params.get("dsn", [""])[0]
        # parse_qs already percent-decodes; the resolver encoded the wallet blob
        # with quote(safe="") so '+', '/', '=' survive round-trip intact here.
        wallet_b64 = query_params.get("wallet_b64", [""])[0]
        wallet_password = query_params.get("wallet_password", [""])[0]

        if dsn_param:
            dsn = unquote(dsn_param)
        else:
            host = parsed.hostname or "localhost"
            port = parsed.port or 1521
            service = parsed.path.lstrip("/") if parsed.path else "ORCL"
            dsn = f"{host}:{port}/{service}"

        # Sync pool — do NOT use create_pool_async
        pool_params = oracledb.PoolParams(
            min=2,
            max=10,
            increment=1,
            wait_timeout=POOL_ACQUIRE_TIMEOUT_S * 1000,
        )
        # urlparse leaves username/password percent-encoded (the resolver quotes
        # them so special characters survive the URI). Decode before use — ADW
        # passwords routinely contain '@', '/', '+', '=' and would otherwise be
        # passed to the driver in their encoded form.
        create_kwargs: dict[str, Any] = {
            "user": unquote(parsed.username) if parsed.username else "",
            "password": unquote(parsed.password) if parsed.password else "",
            "dsn": dsn,
            "params": pool_params,
        }

        # Autonomous Database (ADW/ATP): mTLS via a downloaded wallet zip.
        # Materialize it to a private temp dir; the dir is owned by this
        # connector and removed in close().
        if wallet_b64:
            self._wallet_dir = _materialize_wallet(wallet_b64)

        try:
            if self._wallet_dir:
                # Resolve the alias against the wallet's own tnsnames.ora and
                # bind the wallet into the descriptor — Thick mode ignores the
                # params below, which only Thin mode consumes.
                create_kwargs["dsn"] = _wallet_dsn(self._wallet_dir, dsn)
                create_kwargs["config_dir"] = self._wallet_dir
                create_kwargs["wallet_location"] = self._wallet_dir
                if wallet_password:
                    create_kwargs["wallet_password"] = wallet_password
            self._pool = oracledb.create_pool(**create_kwargs)
        except Exception:
            # Don't leak the extracted wallet if resolution/pool creation fails.
            if self._wallet_dir:
                _cleanup_wallet_dir(self._wallet_dir)
                self._wallet_dir = None
            raise

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return await run_in_threadpool(_query_db, self._pool, sql, params)

    async def insert(
        self, table: str, data: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        records = data if isinstance(data, list) else [data]
        if not records:
            return {"inserted_count": 0, "inserted_ids": []}
        return await run_in_threadpool(_insert_db, self._pool, table, records)

    async def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        if not filters:
            return {"updated_count": 0, "error": "Filters required for update"}
        return await run_in_threadpool(_update_db, self._pool, table, filters, data)

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
            _search_db, self._pool, table, filters, limit, offset, order_by
        )

    async def get_tables(self) -> list[str]:
        return await run_in_threadpool(_get_tables_db, self._pool)

    async def get_schema(self, table: str) -> list[dict[str, Any]]:
        return await run_in_threadpool(_get_schema_db, self._pool, table)

    async def create_table(self, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        return await run_in_threadpool(_create_table_db, self._pool, table, columns)

    async def drop_table(self, table: str) -> dict[str, Any]:
        return await run_in_threadpool(_drop_table_db, self._pool, table)

    async def close(self) -> None:
        if self._pool:
            await run_in_threadpool(_close_pool, self._pool)
            self._pool = None
        if self._wallet_dir:
            _cleanup_wallet_dir(self._wallet_dir)
            self._wallet_dir = None
