"""The caller's PostgreSQL connection, resolved from this request's headers.

The header names and the two connection forms are the ones `connectors/database`
uses, so a platform that already collects a database connection can send the
same fields here:

- `X-Db-Type`: `PostgreSQL` (the only engine this connector speaks; omitted
  means PostgreSQL)
- `X-Connection-Type`: `URI` or `Basic` (omitted: `Basic` when host and
  database name are present, `URI` otherwise)
- URI form: `X-Db-Connection-String`, e.g. `postgresql://user:pass@host:5432/db`
- Basic form: `X-Db-Host`, `X-Db-Port` (default 5432), `X-Db-Name`,
  `X-Db-Username`, `X-Db-Password`

A connection is opened for one tool call and closed before the call returns.
Nothing about it, the password included, outlives the request: no pool keyed by
connection string, no module-level cache. There is no server-wide default
database, so a call that names none is refused rather than quietly pointed at
whatever the server was configured with.

Like `connectors/database`, the host is not checked against private ranges:
the databases this is meant for are usually on private networks, and refusing
them would refuse the use case. The core SSRF check (`validate_url`) is for
HTTP fetches and is applied where this connector makes one.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlparse

import asyncpg

from magone_mcp_core.redact import scrub_text

POSTGRES_CREDENTIAL_HEADERS = [
    "X-Db-Type",
    "X-Connection-Type",
    "X-Db-Connection-String",
    "X-Db-Host",
    "X-Db-Port",
    "X-Db-Name",
    "X-Db-Username",
    "X-Db-Password",
]

DEFAULT_PORT = 5432
CONNECT_TIMEOUT_S = 10
COMMAND_TIMEOUT_S = 60

_DB_TYPE_ALIASES = {"postgres": "postgresql", "postgresql": "postgresql", "pg": "postgresql"}
_CONNECTION_TYPE_ALIASES = {
    "uri": "uri",
    "url": "uri",
    "connection_string": "uri",
    "basic": "basic",
}
_URI_SCHEMES = {"postgresql", "postgres", "postgresql+asyncpg"}


class ConnectionProblem(ValueError):
    """The request does not describe a PostgreSQL connection this connector can open."""


def _clean(credentials: dict[str, str], key: str) -> str:
    return (credentials.get(key) or "").strip()


def resolve_dsn(credentials: dict[str, str]) -> str:
    """A PostgreSQL DSN from the request's credentials, or ConnectionProblem."""
    raw_type = _clean(credentials, "db_type").lower()
    if raw_type and _DB_TYPE_ALIASES.get(raw_type) != "postgresql":
        raise ConnectionProblem(
            f"Unsupported database type: {raw_type}. This connector supports PostgreSQL only"
        )

    raw_connection = _clean(credentials, "connection_type").lower()
    if raw_connection:
        connection_type = _CONNECTION_TYPE_ALIASES.get(raw_connection)
        if connection_type is None:
            raise ConnectionProblem(
                f"Connection type '{raw_connection}' is not supported. Use: basic, uri"
            )
    elif _clean(credentials, "db_host") and _clean(credentials, "db_name"):
        connection_type = "basic"
    else:
        connection_type = "uri"

    if connection_type == "uri":
        uri = _clean(credentials, "db_connection_string")
        if not uri:
            raise ConnectionProblem(
                "No database connection: send X-Db-Connection-String, or X-Db-Host and "
                "X-Db-Name with X-Connection-Type: Basic"
            )
        scheme = urlparse(uri).scheme.lower()
        if scheme not in _URI_SCHEMES:
            raise ConnectionProblem(
                f"Connection URI scheme '{scheme}' is not PostgreSQL (use postgresql://)"
            )
        if scheme == "postgresql+asyncpg":
            uri = "postgresql" + uri[len("postgresql+asyncpg") :]
        return uri

    host = _clean(credentials, "db_host")
    name = _clean(credentials, "db_name")
    username = _clean(credentials, "db_username")
    password = _clean(credentials, "db_password")
    for value, label in ((host, "Host"), (name, "Database Name"), (username, "Username")):
        if not value:
            raise ConnectionProblem(f"Missing required field: {label}")
    raw_port = _clean(credentials, "db_port")
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError:
        raise ConnectionProblem("Port must be a valid number") from None
    if not 0 < port < 65536:
        raise ConnectionProblem("Port must be between 1 and 65535")
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return f"postgresql://{auth}@{host}:{port}/{quote(name, safe='')}"


def describe_error(exc: BaseException) -> str:
    """A driver error as a caller may see it: no credential-shaped text."""
    message = str(exc) or type(exc).__name__
    return scrub_text(message)[:500]


@asynccontextmanager
async def connect(credentials: dict[str, str]) -> Any:
    """One connection, for one call, closed on the way out."""
    dsn = resolve_dsn(credentials)
    try:
        conn = await asyncpg.connect(
            dsn,
            timeout=CONNECT_TIMEOUT_S,
            command_timeout=COMMAND_TIMEOUT_S,
            statement_cache_size=0,
        )
    except Exception as exc:  # noqa: BLE001 - every driver failure is reported, none raised
        raise ConnectionProblem(f"Connection failed: {describe_error(exc)}") from None
    try:
        yield conn
    finally:
        await conn.close(timeout=5)
