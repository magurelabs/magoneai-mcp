"""Database helper for OTP verification."""

from typing import Any

import asyncpg

_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS otp_verifications (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT        NOT NULL,
    identifier      TEXT        NOT NULL,
    identifier_type TEXT        NOT NULL DEFAULT 'email',
    code            TEXT        NOT NULL,
    purpose         TEXT        NOT NULL DEFAULT 'general',
    status          TEXT        NOT NULL DEFAULT 'pending',
    attempts        INTEGER     NOT NULL DEFAULT 0,
    max_attempts    INTEGER     NOT NULL DEFAULT 3,
    expires_at      TIMESTAMPTZ NOT NULL,
    verified_at     TIMESTAMPTZ,
    metadata        JSONB       DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_otp_identifier_purpose
    ON otp_verifications (identifier, purpose, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_otp_session
    ON otp_verifications (session_id);
"""

# Track which DB URLs have already been migrated in this process lifetime
_migrated_dbs: set[str] = set()


async def get_connection(credentials: dict[str, str]) -> asyncpg.Connection:
    """Get a database connection from credentials, running migration on first use."""
    db_url = credentials.get("db_connection_string", "")
    if not db_url:
        raise ValueError("db_connection_string not provided in credentials")
    # URL-decode in case the platform URL-encoded special chars in the password
    from urllib.parse import unquote

    db_url = unquote(db_url)
    conn = await asyncpg.connect(db_url)

    if db_url not in _migrated_dbs:
        await conn.execute(_MIGRATION_SQL)
        _migrated_dbs.add(db_url)

    return conn


async def execute_query(
    conn: asyncpg.Connection,
    query: str,
    *args: Any,
) -> list[dict]:
    """Execute a query and return results as list of dicts."""
    rows = await conn.fetch(query, *args)
    return [dict(row) for row in rows]


async def execute_one(
    conn: asyncpg.Connection,
    query: str,
    *args: Any,
) -> dict | None:
    """Execute a query and return first result as dict."""
    row = await conn.fetchrow(query, *args)
    return dict(row) if row else None


async def execute_command(
    conn: asyncpg.Connection,
    query: str,
    *args: Any,
) -> str:
    """Execute a command (INSERT/UPDATE/DELETE) and return status."""
    return await conn.execute(query, *args)
