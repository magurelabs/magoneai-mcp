"""Shared SQL safety helpers for MCP servers that accept raw SQL.

Read-only enforcement, literal stripping, identifier quoting and JSON-safe row
serialization. Lives in ``shared/`` rather than inside one server because more
than one MCP server hands user- or model-authored SQL to a database, and each
copy of this logic is a chance to get it subtly wrong.

``connectors/database/connectors/sql_common.py`` re-exports these for its own
callers; new servers should import from here.
"""

import json
import re
from typing import Any

_IDENTIFIER_RE = re.compile(r"[^\w.]")

_QUOTE_RE = re.compile(r'"')

# Writes that can nest inside a SELECT/WITH — e.g. PostgreSQL data-modifying
# CTEs: WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x — so checking the
# leading keyword alone is not enough. None of these can appear legitimately
# inside a SELECT, and literals are stripped before this runs.
_NESTED_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|REPLACE|TRUNCATE|DROP|CREATE|ALTER"
    r"|RENAME|GRANT|REVOKE|VACUUM|FLASHBACK|PURGE)\b",
    re.IGNORECASE,
)
# SELECT ... INTO creates a table on SQL Server and is PL/SQL-only elsewhere.
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


def safe_identifier(name: str) -> str:
    """Sanitize a SQL identifier to prevent injection."""
    sanitized = _IDENTIFIER_RE.sub("", name)
    if not sanitized:
        raise ValueError(f"Invalid SQL identifier: '{name}'")
    return sanitized


def quote_identifier(name: str) -> str:
    r"""Return ``name`` as a double-quoted SQL identifier, safe to interpolate.

    ``safe_identifier`` strips anything outside ``[\w.]``, which silently
    rewrites a legitimate identifier containing a space or a hyphen. Quoting
    instead preserves the name and neutralises it: embedded double quotes are
    doubled per the SQL standard, so no value can terminate the quoting and
    start a new clause.

    Use for engines that follow the standard (Trino, PostgreSQL). MySQL
    backticks and SQL Server brackets need their own quoting.
    """
    if not name or not name.strip():
        raise ValueError("Empty SQL identifier")
    if "\x00" in name:
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return '"' + _QUOTE_RE.sub('""', name) + '"'


def quote_qualified_name(*parts: str) -> str:
    """Quote each part of a dotted name: ``("a", "b") -> '"a"."b"'``."""
    return ".".join(quote_identifier(p) for p in parts)


def serialize_row(row: dict) -> dict[str, Any]:
    """Ensure all values in a row dict are JSON-serializable."""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = value if isinstance(value, (bytes, bytearray)) else bytes(value)
            result[key] = raw.hex()
        else:
            try:
                json.dumps(value, default=str)
                result[key] = value
            except (TypeError, ValueError):
                result[key] = str(value)
    return result
