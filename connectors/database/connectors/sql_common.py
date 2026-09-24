"""Shared SQL utilities for database connectors.

DRY: safe_identifier, safe_default, serialize_row, validate_column_type,
and build_where_clause are used by both PostgreSQL and MySQL connectors.

The engine-agnostic halves — read-only enforcement, literal stripping,
identifier sanitizing and row serialization — now live in
``shared/sql_safety.py`` so other MCP servers that accept raw SQL use the
same implementation. They are re-exported here so this server's callers and
tests keep importing them from the same place.
"""

import json
import re
from typing import Any

from .._shared.sql_safety import (
    safe_identifier,
    select_only_error,
    serialize_row,
    strip_sql_literals,
)

from .base import parse_filter_key

_DANGEROUS_DEFAULT_RE = re.compile(r"(;|--\s|/\*|\*/)")
_DESTRUCTIVE_KEYWORD_RE = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|ALTER|INSERT|UPDATE|CREATE|GRANT|REVOKE"
    r"|COPY|EXECUTE|CALL|PREPARE)\b",
    re.IGNORECASE,
)

# --- Read-only (SELECT-only) enforcement for the raw-query tools -------------

ALLOWED_COLUMN_TYPES = {
    "TEXT",
    "VARCHAR",
    "CHAR",
    "INTEGER",
    "INT",
    "BIGINT",
    "SMALLINT",
    "SERIAL",
    "BIGSERIAL",
    "BOOLEAN",
    "BOOL",
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "DATE",
    "TIME",
    "NUMERIC",
    "DECIMAL",
    "FLOAT",
    "DOUBLE PRECISION",
    "REAL",
    "UUID",
    "JSONB",
    "JSON",
    "BYTEA",
    "INET",
    "TINYINT",
    "MEDIUMINT",
    "DATETIME",
    "ENUM",
    "BLOB",
    "MEDIUMTEXT",
    "LONGTEXT",
    # Oracle-specific types
    "NUMBER",
    "VARCHAR2",
    "NVARCHAR2",
    "NCHAR",
    "CLOB",
    "NCLOB",
    "BINARY_FLOAT",
    "BINARY_DOUBLE",
    "RAW",
    "LONG",
    # SQL Server–specific types
    "NVARCHAR",
    "NTEXT",
    "UNIQUEIDENTIFIER",
    "BIT",
    "MONEY",
    "SMALLMONEY",
    "DATETIME2",
    "DATETIMEOFFSET",
    "SMALLDATETIME",
    "IMAGE",
    "VARBINARY",
    "ROWVERSION",
}
_PARAMETERIZED_TYPE_RE = re.compile(
    r"^(VARCHAR|CHAR|NVARCHAR|NCHAR|NUMERIC|DECIMAL|VARCHAR2|NVARCHAR2|NUMBER|RAW|VARBINARY)"
    r"\((?:\d+|MAX)(?:,\s*\d+)?\)$",
    re.IGNORECASE,
)


def safe_default(value: str) -> str:
    """Sanitize a DEFAULT value for use in CREATE TABLE."""
    if _DANGEROUS_DEFAULT_RE.search(value):
        raise ValueError(f"Unsafe DEFAULT value rejected (contains prohibited pattern): '{value}'")
    if _DESTRUCTIVE_KEYWORD_RE.search(value):
        raise ValueError(f"Unsafe DEFAULT value rejected (contains destructive keyword): '{value}'")
    return value.replace("'", "''")


def validate_column_type(col_type: str) -> str:
    """Whitelist-based column type validation."""
    normalized = col_type.strip().upper()
    if normalized in ALLOWED_COLUMN_TYPES:
        return col_type
    if _PARAMETERIZED_TYPE_RE.match(normalized):
        return col_type
    raise ValueError(
        f"Unknown column type: '{col_type}'. Allowed: {', '.join(sorted(ALLOWED_COLUMN_TYPES))}"
    )


def serialize_bind_value(value: Any) -> Any:
    """Prepare a Python value for binding as a SQL parameter.

    DB drivers (e.g. python-oracledb) only accept scalar bind values
    (str/int/float/datetime/bytes/None). Nested ``dict``/``list`` values —
    typically destined for a JSON/CLOB/TEXT column — are encoded to a JSON
    string so they bind cleanly instead of raising (e.g. DPY-3002). All other
    values pass through unchanged.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def is_destructive_sql(sql: str) -> bool:
    """Check if SQL contains destructive keywords (defense-in-depth)."""
    return bool(_DESTRUCTIVE_KEYWORD_RE.search(sql))


def validate_filter_columns(filters: dict[str, Any], known_columns: set[str]) -> None:
    """Raise ValueError if any filter references a column not in known_columns."""
    unknown = []
    for key in filters:
        col, _ = parse_filter_key(key)
        # MySQL/MSSQL return lowercase column names; check both raw and lowercased so
        # mixed-case filter keys (e.g. "UserID") still match on case-insensitive DBs.
        if col not in known_columns and col.lower() not in known_columns:
            unknown.append(col)
    if unknown:
        available = ", ".join(sorted(known_columns))
        raise ValueError(
            f"Unknown filter column(s): {', '.join(unknown)}. Available columns: {available}"
        )


def build_where_clause(
    filters: dict[str, Any],
    dialect: str = "postgresql",
    start_idx: int = 1,
) -> tuple[str, list[Any], int]:
    """Unified WHERE clause builder for PostgreSQL, MySQL, and Oracle."""
    parts: list[str] = []
    params: list[Any] = []
    idx = start_idx

    operators = {
        "__eq": "=",
        "__ne": "!=",
        "__gt": ">",
        "__lt": "<",
        "__gte": ">=",
        "__lte": "<=",
    }

    def _ph() -> str:
        nonlocal idx
        if dialect == "postgresql":
            p = f"${idx}"
        elif dialect == "oracle":
            p = f":{idx}"
        elif dialect == "mssql":
            p = "?"  # pyodbc uses positional ? placeholders
        else:
            p = "%s"
        idx += 1
        return p

    for key, value in filters.items():
        col, op_suffix = parse_filter_key(key)
        # Auto-convert list values to IN / NOT IN; reject lists for comparison operators.
        if isinstance(value, list):
            if op_suffix == "__eq":
                op_suffix = "__in"
            elif op_suffix == "__ne":
                op_suffix = "__not_in"
            elif op_suffix in ("__gt", "__lt", "__gte", "__lte"):
                raise ValueError(
                    f"Filter '{key}': operator '{op_suffix}' does not support list values. "
                    "Use __eq (IN) or __ne (NOT IN) for list filters."
                )
        if dialect == "postgresql":
            safe_col = f'"{safe_identifier(col)}"'
        elif dialect == "oracle":
            safe_col = f'"{safe_identifier(col).upper()}"'
        elif dialect == "mssql":
            safe_col = f"[{safe_identifier(col)}]"
        else:
            safe_col = f"`{safe_identifier(col)}`"

        if op_suffix == "__in":
            if not isinstance(value, list):
                value = [value]
            placeholders = ", ".join(_ph() for _ in value)
            parts.append(f"{safe_col} IN ({placeholders})")
            params.extend(value)
        elif op_suffix == "__not_in":
            if not isinstance(value, list):
                value = [value]
            placeholders = ", ".join(_ph() for _ in value)
            parts.append(f"{safe_col} NOT IN ({placeholders})")
            params.extend(value)
        elif op_suffix == "__contains":
            like_kw = "ILIKE" if dialect == "postgresql" else "LIKE"
            parts.append(f"{safe_col} {like_kw} {_ph()}")
            params.append(f"%{value}%")
        elif op_suffix == "__starts_with":
            like_kw = "ILIKE" if dialect == "postgresql" else "LIKE"
            parts.append(f"{safe_col} {like_kw} {_ph()}")
            params.append(f"{value}%")
        elif op_suffix == "__ends_with":
            like_kw = "ILIKE" if dialect == "postgresql" else "LIKE"
            parts.append(f"{safe_col} {like_kw} {_ph()}")
            params.append(f"%{value}")
        elif op_suffix == "__is_null":
            is_null = "IS NULL" if value else "IS NOT NULL"
            parts.append(f"{safe_col} {is_null}")
        elif op_suffix in operators:
            parts.append(f"{safe_col} {operators[op_suffix]} {_ph()}")
            params.append(value)
        else:
            parts.append(f"{safe_col} = {_ph()}")
            params.append(value)

    fallback = "TRUE" if dialect == "postgresql" else "1=1"  # mssql / mysql both accept 1=1
    where = " AND ".join(parts) if parts else fallback
    return where, params, idx


__all__ = [
    "strip_sql_literals",
    "select_only_error",
    "safe_identifier",
    "serialize_row",
]
