"""Shared LinkedIn REST client: auth, request helpers, URN utilities, serializers.

Every tool talks to LinkedIn's versioned REST API (https://api.linkedin.com/rest)
with the `Linkedin-Version` and `X-Restli-Protocol-Version` headers. Auth is an
OAuth access token, either supplied directly (`X-LinkedIn-Access-Token`) or
minted from a refresh token (`X-LinkedIn-Refresh-Token` plus the app's client
id and secret). LinkedIn only issues refresh tokens to apps with programmatic
refresh enabled; everyone else uses a direct token.

Credentials arrive per call, as the `credentials` dict the framework builds
from the tool's declared headers (or from `MCP_CREDENTIALS_PATH` for a server
run standalone). Nothing here reads a credential from the process environment.

Inputs are forgiving (a post as a URN, a share URL or a bare id; an author as
`me`, an organization id or a full URN) and are normalised here. Outputs are
flat dicts, so every tool returns posts, comments and reactions in one shape.

Every helper returns `(value, error)` with exactly one of them set, and the
tools turn an error into `{"error": ...}`, which the framework reports as a
failed call with the message intact.
"""

import hashlib
import os
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

# Overridable for a test double or a proxy. Not credentials.
API_BASE = os.getenv("LINKEDIN_API_BASE", "https://api.linkedin.com")
TOKEN_URL = os.getenv("LINKEDIN_TOKEN_URL", "https://www.linkedin.com/oauth/v2/accessToken")
# Versioned-API month (YYYYMM). LinkedIn supports each version for at least a year.
LINKEDIN_VERSION = os.getenv("LINKEDIN_VERSION", "202606")

_POST_URN_RE = re.compile(r"urn:li:(?:share|ugcPost|activity):\d+")
# Share URLs sometimes embed the id as ".../activity-7123456789012345678-xxxx".
_ACTIVITY_SLUG_RE = re.compile(r"activity-(\d{10,})")

# Little Text Format reserved characters. Unescaped they trigger formatting or
# a 400, so commentary is escaped by default (backslash first).
_LTF_RESERVED = "|{}@[]()<>*_~"

# Access tokens minted from a refresh token, keyed by a digest of the refresh
# token and client id rather than the raw token: (token, expires_at_monotonic).
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_REFRESH_LEEWAY = 300  # seconds
# Bounded, so a long-lived server rotating through refresh tokens cannot grow it.
_TOKEN_CACHE_MAX = 256

_REQUEST_TIMEOUT = 30


def escape_text(text: str) -> str:
    """Escape Little Text Format reserved characters in post commentary."""
    text = text.replace("\\", "\\\\")
    for ch in _LTF_RESERVED:
        text = text.replace(ch, f"\\{ch}")
    return text


def encode_urn(urn: str) -> str:
    """Percent-encode a URN for use inside a REST path (Rest.li 2.0)."""
    return quote(urn, safe="")


def ms_to_iso(ms: Any) -> str | None:
    """Epoch milliseconds to an ISO-8601 UTC string."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def normalize_post_urn(value: str) -> tuple[str | None, str | None]:
    """Accept a post URN, a share URL, or a bare numeric id."""
    value = (value or "").strip()
    if not value:
        return None, "post is required (URN, share URL, or numeric share id)"
    if value.startswith("urn:li:"):
        return value, None
    match = _POST_URN_RE.search(value)
    if match:
        return match.group(0), None
    slug = _ACTIVITY_SLUG_RE.search(value)
    if slug:
        return f"urn:li:activity:{slug.group(1)}", None
    if value.isdigit():
        return f"urn:li:share:{value}", None
    return None, (
        f"'{value}' is not a valid post reference. Use a post URN "
        "(urn:li:share:123 / urn:li:ugcPost:123 / urn:li:activity:123), "
        "a LinkedIn share URL, or a numeric share id."
    )


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"


def activity_urn_hint(urn: str, error: str) -> str:
    """Explain a `/rest/posts` failure on an activity URN.

    `/rest/posts/{urn}` is keyed by share and ugcPost URNs. A LinkedIn browser
    URL carries an `activity-7123...` slug instead, so pasting one resolves to
    `urn:li:activity:...` and the lookup misses. The engagement tools accept
    activity URNs, which makes the failure look arbitrary without this note.
    """
    if not urn.startswith("urn:li:activity:"):
        return error
    return (
        f"{error} That reference resolved to {urn}, an activity URN. Post lookup and "
        "delete are keyed by the share/ugcPost URN instead; run list_posts and use the "
        "`id` it returns."
    )


# -- auth --------------------------------------------------------------------


def _cache_key(refresh_token: str, client_id: str) -> str:
    return hashlib.sha256(f"{client_id}\0{refresh_token}".encode()).hexdigest()


def _cache_token(key: str, token: str, expires_in: int) -> None:
    """Store a fresh access token, dropping expired entries to bound the cache."""
    now = time.monotonic()
    for existing, (_, expires_at) in list(_token_cache.items()):
        if expires_at <= now:
            del _token_cache[existing]
    if len(_token_cache) >= _TOKEN_CACHE_MAX:
        # Still full after the sweep: evict the entry expiring soonest.
        del _token_cache[min(_token_cache, key=lambda k: _token_cache[k][1])]
    _token_cache[key] = (token, now + max(0, expires_in - _TOKEN_REFRESH_LEEWAY))


async def get_access_token(credentials: dict[str, str]) -> tuple[str | None, str | None]:
    """A direct access token if one was supplied, else a refresh-token exchange."""
    token = (credentials.get("linkedin_access_token") or "").strip()
    if token:
        return token, None

    refresh_token = (credentials.get("linkedin_refresh_token") or "").strip()
    if not refresh_token:
        return None, (
            "LinkedIn credentials not configured. Send an access token in the "
            "X-LinkedIn-Access-Token header (generate one in the LinkedIn Developer "
            "Portal under Auth, OAuth 2.0 tools), or a refresh token in "
            "X-LinkedIn-Refresh-Token with X-LinkedIn-Client-Id and X-LinkedIn-Client-Secret."
        )

    client_id = (credentials.get("linkedin_client_id") or "").strip()
    client_secret = (credentials.get("linkedin_client_secret") or "").strip()
    if not client_id or not client_secret:
        return None, "Refresh-token auth needs client_id and client_secret credentials."

    key = _cache_key(refresh_token, client_id)
    cached = _token_cache.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0], None

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
    except httpx.HTTPError:
        return None, "Cannot reach LinkedIn's token endpoint. Check your internet connection."

    if resp.status_code != 200:
        try:
            detail = resp.json().get("error_description", "")
        except ValueError:
            detail = ""
        return None, (
            f"LinkedIn token refresh failed (HTTP {resp.status_code}). {detail} "
            "Note: LinkedIn only issues refresh tokens to apps with programmatic "
            "refresh enabled; otherwise re-authorize to get a new 60-day access token."
        ).strip()

    try:
        body = resp.json()
    except ValueError:
        body = {}
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        return None, "LinkedIn token endpoint returned no access_token."
    try:
        expires_in = int(body.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    _cache_token(key, token, expires_in)
    return token, None


# -- requests ----------------------------------------------------------------


async def rest(
    credentials: dict[str, str],
    method: str,
    path: str,
    json_data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call the LinkedIn API.

    `path` is relative to the API base (e.g. "/rest/posts"). On a 201 the new
    entity's URN, which LinkedIn sends in the `x-restli-id` header, is returned
    as `{"id": ...}`.
    """
    token, error = await get_access_token(credentials)
    if error:
        return None, error

    headers = {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.request(
                method, f"{API_BASE}{path}", headers=headers, json=json_data, params=params
            )
    except httpx.TimeoutException:
        return None, "LinkedIn API request timed out. Try again."
    except httpx.HTTPError:
        return None, "Cannot reach the LinkedIn API. Check your internet connection."

    if resp.status_code == 401:
        return None, (
            "LinkedIn rejected the access token (HTTP 401). Tokens expire after about 60 "
            "days: re-authorize or generate a fresh token in the LinkedIn Developer Portal."
        )
    if resp.status_code == 403:
        return None, (
            "LinkedIn denied access (HTTP 403). The token is missing a required scope or "
            "your app lacks the needed product: posting needs 'Share on LinkedIn' "
            "(w_member_social) or Community Management API (w_organization_social); "
            "reading engagement/analytics needs the Community Management API product "
            "(r_organization_social, rw_organization_admin) approved for your app."
        )
    if resp.status_code == 404:
        return None, "LinkedIn resource not found (HTTP 404). Check the URN/id."
    if resp.status_code == 429:
        return None, "LinkedIn rate limit exceeded (HTTP 429). Try again later."
    if resp.status_code >= 400:
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        message = (body.get("message") if isinstance(body, dict) else None) or resp.text[:300]
        return None, f"LinkedIn API error (HTTP {resp.status_code}): {message}"

    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {"elements": data} if isinstance(data, list) else {}
    created_id = resp.headers.get("x-restli-id")
    if created_id and "id" not in data:
        data["id"] = created_id
    return data, None


async def rest_paged(
    credentials: dict[str, str],
    path: str,
    params: dict[str, Any],
    limit: int,
    page_size: int = 100,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch a Rest.li collection with start/count paging, up to `limit` elements."""
    elements: list[dict[str, Any]] = []
    start = 0
    while len(elements) < limit:
        count = min(page_size, limit - len(elements))
        data, error = await rest(
            credentials, "GET", path, params={**params, "start": start, "count": count}
        )
        if error:
            return None, error
        batch = [e for e in (data.get("elements") or []) if isinstance(e, dict)]
        elements.extend(batch)
        if len(batch) < count:
            break
        start += len(batch)
    return elements[:limit], None


# -- author and actor resolution ---------------------------------------------


async def get_person_urn(credentials: dict[str, str]) -> tuple[str | None, str | None]:
    """The authenticated member's person URN (needs the openid and profile scopes)."""
    data, error = await rest(credentials, "GET", "/v2/userinfo")
    if error:
        return None, error
    sub = data.get("sub")
    if not sub:
        return None, "LinkedIn /userinfo returned no member id ('sub')."
    return f"urn:li:person:{sub}", None


async def resolve_author(
    credentials: dict[str, str], author: str | None
) -> tuple[str | None, str | None]:
    """Resolve an author to a URN.

    Accepts: nothing (LINKEDIN_ORG_ID if the deployment set it, else the
    authenticated member), 'me', a numeric organization id, or any full URN.

    LINKEDIN_ORG_ID is a process-wide default, so a server shared by several
    callers should leave it unset and have each call pass `author`. Every tool
    echoes the URN it resolved, so what was used is always visible.
    """
    value = (author or "").strip()
    if not value:
        default_org = os.getenv("LINKEDIN_ORG_ID", "").strip()
        if default_org:
            value = default_org
        else:
            return await get_person_urn(credentials)
    if value.lower() in ("me", "self", "member"):
        return await get_person_urn(credentials)
    if value.startswith("urn:li:"):
        return value, None
    if value.isdigit():
        return f"urn:li:organization:{value}", None
    return None, (
        f"'{value}' is not a valid author. Use 'me', a numeric organization id, "
        "or a full URN (urn:li:organization:123 / urn:li:person:abc). "
        "Find your org id with list_organizations."
    )


def org_id_from_urn(urn: str) -> str | None:
    if urn and urn.startswith("urn:li:organization:"):
        return urn.rsplit(":", 1)[-1]
    return None


# -- serializers -------------------------------------------------------------


def post_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    urn = node.get("id", "")
    content = node.get("content") or {}
    return {
        "id": urn,
        "author": node.get("author"),
        "text": node.get("commentary"),
        "visibility": node.get("visibility"),
        "state": node.get("lifecycleState"),
        "article": (content.get("article") or {}).get("source"),
        "media": (content.get("media") or {}).get("id"),
        "published_at": ms_to_iso(node.get("publishedAt") or node.get("createdAt")),
        "url": post_url(urn) if urn else None,
    }


def comment_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("commentUrn") or node.get("$URN") or node.get("id"),
        "actor": node.get("actor"),
        "text": (node.get("message") or {}).get("text"),
        "likes": (node.get("likesSummary") or {}).get("totalLikes", 0),
        "created_at": ms_to_iso((node.get("created") or {}).get("time")),
    }


def reaction_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    created = node.get("created") or {}
    return {
        "actor": node.get("actor") or created.get("actor") or node.get("root"),
        "type": node.get("reactionType"),
        "created_at": ms_to_iso(created.get("time")),
    }
