"""Error sanitization for MCP servers that talk to databases.

Strips credentials, file paths and driver internals from exception messages so
they are safe to hand back to a caller (and to a model, which will quote them).

Two sanitizers because two shapes of exception exist. DB-API drivers raise
``asyncpg.InterfaceError: relation does not exist`` — a class prefix and a colon.
Trino raises ``TrinoUserError(type=USER_ERROR, name=SYNTAX_ERROR, message="…",
query_id=…)`` — a repr with no colon, so a prefix regex written for the first
shape silently matches nothing and passes the whole repr through.
"""

import re
from typing import Optional

_URI_CREDS_RE = re.compile(r"://[^@/\s]+@")
_FILE_PATH_RE = re.compile(r"(/[^\s:]+/[\w.\-]+\.py\b[:\d]*)")
_DRIVER_PREFIX_RE = re.compile(r"^[\w.]+Error:\s*")

# Fallback for when the driver exception carries no `.message` attribute.
_TRINO_MESSAGE_RE = re.compile(r'message="((?:[^"\\]|\\.)*)"')
_TRINO_QUERY_ID_RE = re.compile(r",?\s*query_id=[\w-]+\)?\s*$")
_TRINO_WRAPPER_RE = re.compile(r"^\w*Trino\w*Error\((?:type|name)=")


def redact(msg: str) -> str:
    """Remove credential URIs and absolute file paths from a message."""
    msg = _URI_CREDS_RE.sub("://***@", msg)
    return _FILE_PATH_RE.sub("<path>", msg)


def sanitize_db_error(exc: BaseException) -> str:
    """Safe message for DB-API drivers that raise ``SomeError: detail``.

    Keeps the useful diagnostic part (e.g. "relation does not exist") while
    stripping credential patterns, traceback paths and the driver class prefix.
    """
    msg = _DRIVER_PREFIX_RE.sub("", redact(str(exc)))
    return msg.strip() or "Unknown database error"


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
