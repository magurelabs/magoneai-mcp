"""Shared Zoom client: Server-to-Server OAuth token cache and REST helper.

Tools talk to the Zoom API (https://api.zoom.us/v2) with an access token minted
from Server-to-Server OAuth credentials (account id, client id, client secret).
Zoom tokens live an hour, so they are cached per connection and reused until
shortly before expiry.

Errors are raised as ValueError on purpose. protocol.py's _SAFE_EXCEPTIONS is
(ValueError, TypeError, KeyError) — only those messages reach the caller, and
anything else is replaced with an opaque "Internal error". Zoom puts the useful
part in the body ({"code": 3001, "message": "Meeting does not exist"}), so that
body is what gets raised: a bare status code tells the agent nothing it can act
on. Same reasoning as shared/docusign_auth.py's _raise_for_docusign_error.
"""

import asyncio
import base64
import hashlib
import time
from typing import Any, Dict, Optional, Tuple

import httpx

ZOOM_API_BASE = "https://api.zoom.us/v2"
ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"

# Refresh this many seconds before Zoom's reported expiry, so a cached token is
# never handed to the API already expired.
_TOKEN_REFRESH_LEEWAY = 300  # seconds

# Process-wide cache: (account_id, client_id, secret fingerprint) -> (token, expires_at).
# The secret is part of the key (hashed, never stored raw) so that rotating a
# connection's client secret misses the cache instead of serving a token minted
# from the old one for up to an hour.
_token_cache: Dict[Tuple[str, str, str], Tuple[str, float]] = {}

# Guards the cache so a burst of tool calls on a cold cache mints one token
# rather than one per call. Created at import time — needs no running loop.
_token_cache_lock = asyncio.Lock()

# Bound the cache so a long-lived server serving many connections can't grow it
# without limit; expired entries are dropped first.
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
