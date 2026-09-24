"""The zoom connector.

Ported from the v1 mixin form by a codemod.

3 tools, 1 of them proposed read-only. Every input schema
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

SERVER_NAME = "zoom"
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

import asyncio
import base64
import hashlib
import time
from typing import Any, Dict, Optional, Tuple
import httpx
import sys
from pathlib import Path
from typing import Any


ZOOM_API_BASE = "https://api.zoom.us/v2"

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"

_TOKEN_REFRESH_LEEWAY = 300  # seconds

_token_cache: Dict[Tuple[str, str, str], Tuple[str, float]] = {}

_token_cache_lock = asyncio.Lock()

_MAX_CACHED_TOKENS = 256

def _zoom_error_detail(response: httpx.Response) -> str:
    """Pull Zoom's own explanation out of an error response body."""
    try:
        body = response.json()
    except Exception:
        return response.text.strip() or f"HTTP {response.status_code}"
    if not isinstance(body, dict):
        return str(body)
    message = body.get("message") or body.get("reason") or response.text.strip()
    code = body.get("code")
    return f"{message} (code {code})" if code is not None else str(message)

def raise_for_zoom_error(response: httpx.Response, context: str) -> None:
    """Raise ValueError carrying Zoom's message when a response is an error."""
    if response.status_code < 400:
        return
    raise ValueError(f"{context} ({response.status_code}): {_zoom_error_detail(response)}")

def normalize_meeting_id(meeting_id: Any) -> str:
    """Accept a meeting id as int or str and return it as a path-safe string.

    Zoom meeting ids are numeric but 11 digits long, and every hop that carries
    one (a workflow variable, a stored record, an LLM argument) is free to hand
    it back as a string. Both are accepted rather than rejected by schema
    validation before the tool ever runs.
    """
    text = str(meeting_id).strip()
    if not text:
        raise ValueError("meeting_id is required.")
    # Zoom ids are digits, optionally spaced for display ("823 4567 8901").
    compact = text.replace(" ", "").replace("-", "")
    if not compact.isdigit():
        raise ValueError(f"meeting_id must be a numeric Zoom meeting id, got {text!r}.")
    return compact

def _prune_expired(now: float) -> None:
    """Drop expired entries; if still oversized, drop the soonest to expire."""
    for key, (_, expires_at) in list(_token_cache.items()):
        if now >= expires_at:
            _token_cache.pop(key, None)
    if len(_token_cache) >= _MAX_CACHED_TOKENS:
        oldest = min(_token_cache, key=lambda k: _token_cache[k][1])
        _token_cache.pop(oldest, None)

async def get_access_token(credentials: Dict[str, str]) -> str:
    """Mint (or reuse) a Zoom access token via Server-to-Server OAuth."""
    account_id = (credentials.get("zoom_account_id") or "").strip()
    client_id = (credentials.get("zoom_client_id") or "").strip()
    client_secret = (credentials.get("zoom_client_secret") or "").strip()

    missing = [
        name
        for name, value in (
            ("zoom_account_id", account_id),
            ("zoom_client_id", client_id),
            ("zoom_client_secret", client_secret),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"Missing Zoom credentials: {', '.join(missing)}. Add them to the Zoom "
            "connection (Zoom Marketplace → Develop → Build App → Server-to-Server OAuth)."
        )

    fingerprint = hashlib.sha256(client_secret.encode()).hexdigest()[:16]
    cache_key = (account_id, client_id, fingerprint)

    now = time.time()
    cached = _token_cache.get(cache_key)
    if cached and now < cached[1] - _TOKEN_REFRESH_LEEWAY:
        return cached[0]

    async with _token_cache_lock:
        # Re-check: another coroutine may have minted one while we waited.
        now = time.time()
        cached = _token_cache.get(cache_key)
        if cached and now < cached[1] - _TOKEN_REFRESH_LEEWAY:
            return cached[0]

        auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                ZOOM_OAUTH_URL,
                headers={
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "account_credentials", "account_id": account_id},
            )
        # An auth failure here is the single most common setup problem (wrong
        # account id, secret not yet activated), so say what Zoom said.
        raise_for_zoom_error(resp, "Zoom authentication failed")

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise ValueError("Zoom returned no access_token for these credentials.")

        expires_in = data.get("expires_in", 3600)
        _prune_expired(time.time())
        _token_cache[cache_key] = (token, time.time() + expires_in)
        return token

async def zoom_request(
    credentials: Dict[str, str],
    method: str,
    path: str,
    json_body: Optional[Dict[str, Any]] = None,
    context: str = "Zoom API error",
) -> Dict[str, Any]:
    """Call the Zoom API and return the decoded body ({} when there is none)."""
    token = await get_access_token(credentials)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            f"{ZOOM_API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
        )
    raise_for_zoom_error(resp, context)

    # DELETE answers 204 with an empty body.
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()

CREDENTIAL_HEADERS = [
    "X-Zoom-Account-Id",
    "X-Zoom-Client-Id",
    "X-Zoom-Client-Secret",
]

_MEETING_ID_SCHEMA = {
    "type": ["integer", "string"],
    "description": "The Zoom meeting ID (e.g. 82345678901), as a number or string",
}

def _meeting_error(event: str, error: Exception) -> dict[str, Any]:
    """Log a failed call and return it in the tools' error shape.

    ValueError carries a message meant for the caller (a missing credential,
    or whatever Zoom said) — logged as a warning, since it is an expected
    outcome rather than a bug. Anything else is unexpected: log the traceback.
    """
    if isinstance(error, ValueError):
        logger.warning(event, error=str(error))
        return {"success": False, "error": str(error)}
    logger.exception(event, error=str(error))
    return {"success": False, "error": f"Zoom request failed: {error}"}



# credential_headers: ['X-Zoom-Account-Id', 'X-Zoom-Client-Id', 'X-Zoom-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /users/me/meetings
@server.tool(
    name='create_meeting',
    description="Create a scheduled Zoom meeting. Returns the join URL, meeting ID, and password. Use for virtual consultations.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Zoom-Account-Id', 'X-Zoom-Client-Id', 'X-Zoom-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'topic': {'type': 'string',
                              'description': "Meeting title (e.g. 'Consultation with John "
                                             "Doe')"},
                    'start_time': {'type': 'string',
                                   'description': 'Start time in ISO 8601 UTC format (e.g. '
                                                  "'2026-04-10T14:00:00Z')"},
                    'duration': {'type': 'integer',
                                 'description': 'Duration in minutes (default 60)',
                                 'default': 60},
                    'agenda': {'type': 'string',
                               'description': 'Meeting description/agenda (optional)'}},
     'required': ['topic', 'start_time']})
@declare_output_schema({'properties': {'duration': {'type': 'integer'},
                                       'error': {'type': 'string'},
                                       'join_url': {'type': 'string'},
                                       'meeting_id': {'type': 'integer'},
                                       'password': {'type': 'string'},
                                       'start_time': {'type': 'string'},
                                       'start_url': {'type': 'string'},
                                       'success': {'type': 'boolean'},
                                       'topic': {'type': 'string'}},
                        'type': 'object'})
async def create_meeting(credentials: dict[str, str],
    topic: str,
    start_time: str,
    duration: int = 60,
    agenda: str = "",
) -> dict[str, Any]:
    """Create a scheduled Zoom meeting."""
    try:
        payload = {
            "topic": topic,
            "type": 2,
            "start_time": start_time,
            "duration": duration,
            "timezone": "UTC",
            "agenda": agenda,
            "settings": {
                "host_video": True,
                "participant_video": True,
                "join_before_host": False,
                "mute_upon_entry": False,
                "watermark": False,
                "use_pmi": False,
                "approval_type": 2,
                "audio": "both",
                "auto_recording": "none",
                "waiting_room": True,
                "meeting_authentication": False,
            },
        }

        data = await zoom_request(
            credentials,
            "POST",
            "/users/me/meetings",
            payload,
            context="Could not create the Zoom meeting",
        )

        return {
            "success": True,
            "meeting_id": data["id"],
            "join_url": data["join_url"],
            "start_url": data.get("start_url", ""),
            "password": data.get("password", ""),
            "topic": data["topic"],
            "start_time": data["start_time"],
            "duration": data["duration"],
        }
    except Exception as e:
        return _meeting_error("create_meeting_error", e)



# credential_headers: ['X-Zoom-Account-Id', 'X-Zoom-Client-Id', 'X-Zoom-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /meetings/{resolved_id}
@server.tool(
    name='delete_meeting',
    description="Delete/cancel an existing Zoom meeting.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Zoom-Account-Id', 'X-Zoom-Client-Id', 'X-Zoom-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'meeting_id': {'type': ['integer', 'string'],
                                   'description': 'The Zoom meeting ID (e.g. 82345678901), '
                                                  'as a number or string'}},
     'required': ['meeting_id']})
@declare_output_schema({'properties': {'error': {'type': 'string'},
                                       'meeting_id': {'type': 'string'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def delete_meeting(credentials: dict[str, str],
    meeting_id: int | str,
) -> dict[str, Any]:
    """Delete a Zoom meeting."""
    try:
        resolved_id = normalize_meeting_id(meeting_id)
        await zoom_request(
            credentials,
            "DELETE",
            f"/meetings/{resolved_id}",
            context=f"Could not delete Zoom meeting {resolved_id}",
        )
        return {"success": True, "meeting_id": resolved_id}
    except Exception as e:
        return _meeting_error("delete_meeting_error", e)



# credential_headers: ['X-Zoom-Account-Id', 'X-Zoom-Client-Id', 'X-Zoom-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /meetings/{resolved_id}
@server.tool(
    name='get_meeting',
    description="Get details of an existing Zoom meeting by its ID.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Zoom-Account-Id', 'X-Zoom-Client-Id', 'X-Zoom-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'meeting_id': {'type': ['integer', 'string'],
                                   'description': 'The Zoom meeting ID (e.g. 82345678901), '
                                                  'as a number or string'}},
     'required': ['meeting_id']})
@declare_output_schema({'properties': {'agenda': {'type': 'string'},
                                       'duration': {'type': 'integer'},
                                       'error': {'type': 'string'},
                                       'join_url': {'type': 'string'},
                                       'meeting_id': {'type': 'integer'},
                                       'password': {'type': 'string'},
                                       'start_time': {'type': 'string'},
                                       'start_url': {'type': 'string'},
                                       'status': {'type': 'string'},
                                       'success': {'type': 'boolean'},
                                       'topic': {'type': 'string'}},
                        'type': 'object'})
async def get_meeting(credentials: dict[str, str],
    meeting_id: int | str,
) -> dict[str, Any]:
    """Get details of a Zoom meeting."""
    try:
        resolved_id = normalize_meeting_id(meeting_id)
        data = await zoom_request(
            credentials,
            "GET",
            f"/meetings/{resolved_id}",
            context=f"Could not read Zoom meeting {resolved_id}",
        )

        return {
            "success": True,
            "meeting_id": data["id"],
            "topic": data.get("topic", ""),
            "join_url": data.get("join_url", ""),
            "start_url": data.get("start_url", ""),
            "password": data.get("password", ""),
            "agenda": data.get("agenda", ""),
            "start_time": data.get("start_time", ""),
            "duration": data.get("duration", 0),
            "status": data.get("status", ""),
        }
    except Exception as e:
        return _meeting_error("get_meeting_error", e)
