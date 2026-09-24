"""Shared plumbing for the Trino tools: credentials, limits and catalog scoping."""

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..connectors.trino_connector import (
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    MAX_QUERY_TIMEOUT_SECONDS,
)

from .._shared.sql_safety import strip_sql_literals

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

# A row cap alone does not bound the payload: 5000 rows of wide columns will
# still swamp the context of whatever model reads the result.
MAX_RESPONSE_BYTES = 256_000

# Read-only statements Trino exposes as their own verbs rather than as SELECT.
METADATA_KEYWORDS = frozenset({"SHOW", "DESCRIBE", "DESC"})
_METADATA_START_RE = re.compile(r"^\s*\(*\s*(SHOW|DESCRIBE|DESC)\b", re.IGNORECASE)

_HAS_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\s*$", re.IGNORECASE)
_HAS_FETCH_RE = re.compile(r"\bFETCH\s+(NEXT|FIRST)\b", re.IGNORECASE)

# catalog.schema.table, with each part either bare or double-quoted.
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
