"""The otp-verification connector.

Ported from the v1 mixin form by a codemod.

3 tools, 0 of them proposed read-only. Every input schema
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

SERVER_NAME = "otp-verification"
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

from typing import Any
import asyncpg
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from .tools._constants import (
    CREDENTIAL_HEADERS,
    DEFAULT_BLOCK_HOURS,
    DEFAULT_EXPIRY_MINUTES,
    DEFAULT_MAX_RESENDS,
    DEFAULT_OTP_LENGTH,
    DEFAULT_TABLE,
    VALID_IDENTIFIER_TYPES,
)
from typing import Any, Dict
from .tools._constants import CREDENTIAL_HEADERS, DEFAULT_TABLE
from .tools._constants import (
    CREDENTIAL_HEADERS,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_TABLE,
)


DEFAULT_OTP_LENGTH = 6

DEFAULT_EXPIRY_MINUTES = 5

DEFAULT_MAX_ATTEMPTS = 3

DEFAULT_MAX_RESENDS = 3

DEFAULT_BLOCK_HOURS = 2

DEFAULT_COOLDOWN_MINUTES = 15

DEFAULT_TABLE = "otp_verifications"

STATUS_PENDING = "pending"

STATUS_VERIFIED = "verified"

STATUS_EXPIRED = "expired"

STATUS_LOCKED = "locked"

VALID_IDENTIFIER_TYPES = {"email", "phone", "user_id", "session"}

CREDENTIAL_HEADERS = [
    "X-Db-Connection-String",
]

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

TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{0,62}$")



# credential_headers: ['X-Db-Connection-String']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to False, destructiveHint=False.
# The query itself is a SELECT COUNT/MAX. But every tool here calls
# get_connection(), which on the first call per database URL executes
# _MIGRATION_SQL: CREATE TABLE IF NOT EXISTS otp_verifications plus two
# CREATE INDEX. That is DDL against the caller's database, so under
# MCP_READ_ONLY=1 this tool would still create a table. Idempotent is
# not the same as read-only.
# The better fix is to move the migration off the request path so this
# can go back to True; until then the cautious value is the honest one.
# Evidence: SELECT COUNT(*) as verified_count,
@server.tool(
    name='check_verification_status',
    description="Check if an identifier has been verified for a given purpose. Use before initiating a new OTP flow to check if already verified.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Db-Connection-String'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'identifier': {'type': 'string',
                                   'description': 'The identifier to check (email, phone)'},
                    'purpose': {'type': 'string', 'default': 'general'},
                    'table_name': {'type': 'string', 'default': 'otp_verifications'},
                    'days_lookback': {'type': 'integer', 'default': 7}},
     'required': ['identifier']})
@declare_output_schema({'properties': {'error': {'type': 'string'},
                                       'has_verified': {'type': 'boolean'},
                                       'last_verified_at': {'type': 'string'},
                                       'success': {'type': 'boolean'},
                                       'verified_count': {'type': 'integer'}},
                        'type': 'object'})
async def check_verification_status(credentials: Dict[str, str],
    identifier: str,
    purpose: str = "general",
    table_name: str = DEFAULT_TABLE,
    days_lookback: int = 7,
) -> Dict[str, Any]:
    """Check verification status for an identifier."""
    if not identifier:
        return {"success": False, "error": "identifier is required"}
    if not TABLE_NAME_PATTERN.match(table_name):
        return {"success": False, "error": "Invalid table_name"}

    identifier = identifier.strip().lower()
    days_lookback = max(1, min(365, days_lookback))

    conn = await get_connection(credentials)
    try:
        row = await execute_one(
            conn,
            f"SELECT COUNT(*) as verified_count, "
            f"MAX(verified_at) as last_verified_at "
            f"FROM {table_name} "
            f"WHERE identifier = $1 AND purpose = $2 "
            f"AND status = 'verified' "
            f"AND verified_at > NOW() - make_interval(days => $3)",
            identifier,
            purpose,
            days_lookback,
        )

        count = row["verified_count"] if row else 0
        last = row["last_verified_at"] if row else None

        return {
            "success": True,
            "has_verified": count > 0,
            "verified_count": count,
            "last_verified_at": last.isoformat() if last else None,
        }
    except Exception as e:
        logger.exception("check_status_error", error=str(e))
        return {"success": False, "error": f"Status check failed: {e}"}
    finally:
        await conn.close()



# credential_headers: ['X-Db-Connection-String']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: UPDATE {table} SET status='expired' over the caller's
# pending rows before inserting the new code, invalidating live codes.
# Evidence: INSERT INTO {table} of a new code row. Additive, so destructiveHint
# stays False.
@server.tool(
    name='generate_otp',
    description="Generate a verification OTP code for an identifier (email, phone). If a valid code already exists, returns it without generating a new one. Max 3 codes per identifier per 2 hours.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Db-Connection-String'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'identifier': {'type': 'string',
                                   'description': 'What to verify (email address, phone '
                                                  'number)'},
                    'identifier_type': {'type': 'string',
                                        'enum': ['email', 'phone', 'user_id'],
                                        'default': 'email'},
                    'purpose': {'type': 'string', 'default': 'general'},
                    'table_name': {'type': 'string', 'default': 'otp_verifications'},
                    'expiry_minutes': {'type': 'integer', 'default': 5},
                    'metadata': {'type': 'string',
                                 'description': 'Optional JSON string with extra context'}},
     'required': ['identifier']})
@declare_output_schema({'properties': {'attempts_remaining': {'type': 'integer'},
                                       'code': {'type': 'string'},
                                       'error': {'type': 'string'},
                                       'expires_in_minutes': {'type': 'integer'},
                                       'is_resend': {'type': 'boolean'},
                                       'message': {'type': 'string'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def generate_otp(credentials: Dict[str, str],
    identifier: str,
    identifier_type: str = "email",
    purpose: str = "general",
    table_name: str = DEFAULT_TABLE,
    expiry_minutes: int = DEFAULT_EXPIRY_MINUTES,
    metadata: Optional[str | dict] = None,
) -> Dict[str, Any]:
    """Generate and store a new OTP."""
    if not identifier or not identifier.strip():
        return {"success": False, "error": "identifier is required"}
    if identifier_type not in VALID_IDENTIFIER_TYPES:
        return {"success": False, "error": "Invalid identifier_type"}
    if not TABLE_NAME_PATTERN.match(table_name):
        return {"success": False, "error": "Invalid table_name"}

    identifier = identifier.strip().lower()
    expiry_minutes = max(1, min(30, expiry_minutes))

    # The schema says "a JSON string", and a JSON string is what a caller
    # sends. The SDK parses a JSON-looking string argument into an object
    # before validation, so typed `str` alone rejected every well-formed value
    # ("Input should be a valid string"). Accept both, as v1 effectively did.
    meta_dict: dict = {}
    if isinstance(metadata, dict):
        meta_dict = metadata
    elif metadata:
        try:
            meta_dict = json.loads(metadata)
        except json.JSONDecodeError:
            return {"success": False, "error": "metadata must be valid JSON"}
        if not isinstance(meta_dict, dict):
            return {"success": False, "error": "metadata must be a JSON object"}

    conn = await get_connection(credentials)
    try:
        # 1. Check for existing valid pending OTP for this identifier
        existing = await execute_one(
            conn,
            f"SELECT id, code, expires_at "
            f"FROM {table_name} "
            f"WHERE identifier = $1 "
            f"AND purpose = $2 AND status = 'pending' "
            f"AND expires_at > NOW() "
            f"ORDER BY created_at DESC LIMIT 1",
            identifier,
            purpose,
        )

        if existing:
            logger.info(
                "otp_already_exists",
                identifier_type=identifier_type,
                purpose=purpose,
            )
            return {
                "success": True,
                "code": existing["code"],
                "expires_in_minutes": expiry_minutes,
                "is_resend": True,
                "message": "A valid code already exists. Check your email.",
            }

        # 2. Count OTPs generated in the block window
        count_row = await execute_one(
            conn,
            f"SELECT COUNT(*) as cnt FROM {table_name} "
            f"WHERE identifier = $1 AND purpose = $2 "
            f"AND created_at > NOW() - INTERVAL '{DEFAULT_BLOCK_HOURS} hours'",
            identifier,
            purpose,
        )
        total_generated = count_row["cnt"] if count_row else 0

        if total_generated >= DEFAULT_MAX_RESENDS:
            return {
                "success": False,
                "error": (
                    f"Too many verification attempts. "
                    f"Please try again after {DEFAULT_BLOCK_HOURS} hours."
                ),
            }

        # 3. Expire any old pending OTPs for this identifier
        await conn.execute(
            f"UPDATE {table_name} SET status = 'expired' "
            f"WHERE identifier = $1 "
            f"AND purpose = $2 AND status = 'pending'",
            identifier,
            purpose,
        )

        # 4. Generate new OTP
        otp_len = DEFAULT_OTP_LENGTH
        row = await execute_one(
            conn,
            f"INSERT INTO {table_name} "
            f"(session_id, identifier, identifier_type, code, purpose, "
            f"expires_at, metadata) "
            f"VALUES ($1, $2, $3, "
            f"LPAD(FLOOR(RANDOM() * {10**otp_len})::TEXT, {otp_len}, '0'), "
            f"$4, NOW() + INTERVAL '{expiry_minutes} minutes', $5) "
            f"RETURNING code",
            identifier,
            identifier,
            identifier_type,
            purpose,
            json.dumps(meta_dict),
        )

        if not row:
            return {"success": False, "error": "Failed to generate OTP"}

        logger.info(
            "otp_generated",
            identifier_type=identifier_type,
            purpose=purpose,
            attempts_used=total_generated + 1,
        )
        return {
            "success": True,
            "code": row["code"],
            "expires_in_minutes": expiry_minutes,
            "is_resend": False,
            "attempts_remaining": DEFAULT_MAX_RESENDS - total_generated - 1,
        }
    except Exception as e:
        logger.exception("generate_otp_error", error=str(e))
        return {"success": False, "error": f"Failed to generate OTP: {e}"}
    finally:
        await conn.close()



# credential_headers: ['X-Db-Connection-String']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: UPDATE {table} SET status='verified', and on the failure
# path SET attempts and SET status='locked'. Writes on every branch.
# Evidence: Reads the pending code, then UPDATE {table} SET attempts, and SET
# status='locked' once attempts run out. The read is what the name
# suggests; the writes are what make this not read-only.
@server.tool(
    name='verify_otp',
    description="Verify an OTP code for an identifier. Max 3 attempts before lockout (15 min cooldown).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Db-Connection-String'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'identifier': {'type': 'string',
                                   'description': 'The identifier (email, phone) used when '
                                                  'generating'},
                    'code': {'type': 'string', 'description': 'The OTP code to verify'},
                    'purpose': {'type': 'string', 'default': 'general'},
                    'table_name': {'type': 'string', 'default': 'otp_verifications'}},
     'required': ['identifier', 'code']})
@declare_output_schema({'properties': {'attempts_remaining': {'type': 'integer'},
                                       'error': {'type': 'string'},
                                       'metadata': {'type': 'object'},
                                       'success': {'type': 'boolean'},
                                       'verified': {'type': 'boolean'}},
                        'type': 'object'})
async def verify_otp(credentials: Dict[str, str],
    identifier: str,
    code: str,
    purpose: str = "general",
    table_name: str = DEFAULT_TABLE,
) -> Dict[str, Any]:
    """Verify an OTP code."""
    if not identifier or not code:
        return {"success": False, "verified": False, "error": "identifier and code required"}
    if not TABLE_NAME_PATTERN.match(table_name):
        return {"success": False, "verified": False, "error": "Invalid table_name"}

    identifier = identifier.strip().lower()
    code = code.strip()

    conn = await get_connection(credentials)
    try:
        # Check for cooldown lock
        locked = await execute_one(
            conn,
            f"SELECT id FROM {table_name} "
            f"WHERE identifier = $1 "
            f"AND purpose = $2 AND status = 'locked' "
            f"AND created_at > NOW() - INTERVAL '{DEFAULT_COOLDOWN_MINUTES} minutes' "
            f"LIMIT 1",
            identifier,
            purpose,
        )
        if locked:
            return {
                "success": False,
                "verified": False,
                "error": f"Locked. Try again in {DEFAULT_COOLDOWN_MINUTES} minutes.",
            }

        # Find latest pending OTP for this identifier
        otp_row = await execute_one(
            conn,
            f"SELECT id, code, attempts, max_attempts, metadata "
            f"FROM {table_name} "
            f"WHERE identifier = $1 "
            f"AND purpose = $2 AND status = 'pending' "
            f"AND expires_at > NOW() "
            f"ORDER BY created_at DESC LIMIT 1",
            identifier,
            purpose,
        )

        if not otp_row:
            return {
                "success": False,
                "verified": False,
                "error": "No pending verification found. Code may have expired.",
            }

        if otp_row["code"] == code:
            await conn.execute(
                f"UPDATE {table_name} "
                f"SET status = 'verified', verified_at = NOW() "
                f"WHERE id = $1",
                otp_row["id"],
            )
            logger.info("otp_verified", purpose=purpose)

            meta = {}
            if otp_row["metadata"]:
                try:
                    raw = otp_row["metadata"]
                    meta = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    pass

            return {"success": True, "verified": True, "metadata": meta}
        else:
            new_attempts = otp_row["attempts"] + 1
            remaining = otp_row["max_attempts"] - new_attempts

            if new_attempts >= otp_row["max_attempts"]:
                await conn.execute(
                    f"UPDATE {table_name} SET status = 'locked', attempts = $2 WHERE id = $1",
                    otp_row["id"],
                    new_attempts,
                )
                logger.warning("otp_locked", identifier=identifier)
                return {
                    "success": False,
                    "verified": False,
                    "attempts_remaining": 0,
                    "error": f"Too many attempts. Locked for {DEFAULT_COOLDOWN_MINUTES} min.",
                }
            else:
                await conn.execute(
                    f"UPDATE {table_name} SET attempts = $2 WHERE id = $1",
                    otp_row["id"],
                    new_attempts,
                )
                return {
                    "success": False,
                    "verified": False,
                    "attempts_remaining": remaining,
                    "error": f"Invalid code. {remaining} attempt(s) remaining.",
                }
    except Exception as e:
        logger.exception("verify_otp_error", error=str(e))
        return {"success": False, "verified": False, "error": f"Verification failed: {e}"}
    finally:
        await conn.close()
