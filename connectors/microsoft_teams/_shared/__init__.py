"""Vendored Microsoft Graph helpers, for the same reason as the Google ones.

v1 kept `_get_access_token` and `_graph_request` as methods on a repository-wide
`MicrosoftGraph` mixin, and every tool called them as `self._graph_request(...)`.
The port flattened the tool bodies to module level and stripped the `self` from
those calls, but carried the methods across for only two of the four Microsoft
connectors. `outlook_calendar` and `microsoft_teams` were left calling a name
that exists nowhere.

That failed in the worst available way. The module imports, because Python
resolves a global at call time. `tools/list` answers. Every gate in this
repository - including the import gate written after eleven connectors were
found not to import - stays green. And each ported body ends in
`except Exception`, so the NameError comes back as

    {"success": false, "error": "Failed to list events"}

which reads as an expired token or a Graph outage. Measured: 6 of 6
`outlook_calendar` tools and 12 of 12 `microsoft_teams` tools, all of them
answering like a vendor problem.

Vendored per connector rather than shared from one place, which is this
repository's deliberate pattern: a connector is published as an independent
package and must not import a sibling.
"""

import asyncio
import random
import time
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

import httpx


class GraphNotFoundError(ValueError):
    """Raised only for an actual Graph 404 — distinguishes a real "resource
    doesn't exist" from other errors whose message text happens to mention
    "not found" (e.g. a 400 for a malformed id)."""


class GraphAuthError(ValueError):
    """No usable token: a missing or rejected refresh token, or a Graph 401.

    A ValueError like every other Graph failure here, so the existing tools
    report it exactly as before; the paged tools key their `error_code` on it.
    """


class GraphForbiddenError(ValueError):
    """A Graph 403: the token works but lacks the permission, or the user
    cannot see the resource."""


class GraphRetryLaterError(ValueError):
    """Graph throttled the call (429) or was unavailable (503, 504), and still
    was after the in-call retries.

    `retry_after` is Graph's own `Retry-After` in seconds when it sent one, so
    a caller that can wait durably does not have to guess.
    """

    def __init__(self, message: str, status: int, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


GRAPH_API_URL = "https://graph.microsoft.com/v1.0"

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Graph answers throttling with 429 and Retry-After, and asks callers to wait
# that long rather than retry at once, because every request counts against the
# limit: https://learn.microsoft.com/en-us/graph/throttling. A 429 is a
# request the rate limiter turned away, so it is retried for every method.
# 503 and 504 are retried for reads only: a 504 means the gateway stopped
# waiting, not that Graph did nothing, so resending a write could apply it
# twice (a message posted twice). A write answered 503 or 504 is reported
# as before and the caller decides. The budget bounds the whole call, so one
# throttled page never runs into a host's tool timeout; past it the caller
# gets GraphRetryLaterError with Retry-After to wait on itself.
_RETRY_STATUSES = frozenset({429, 503, 504})
_WRITE_RETRY_STATUSES = frozenset({429})
_READ_METHODS = frozenset({"GET", "HEAD"})
_MAX_RETRIES = 3
_RETRY_BUDGET_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 20.0

# The only host and API version a pagination link may name. A link is sent with
# the bearer token, so following one anywhere else would hand that token over.
_GRAPH_HOST = "graph.microsoft.com"
_GRAPH_VERSION_PREFIX = "/v1.0"


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """Graph's Retry-After in seconds, or None when absent or not a number."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Retry-After when Graph sent one, else exponential backoff with jitter.

    Retry-After is never shortened: retrying before it is the immediate retry
    Graph asks callers not to make. A wait longer than the budget ends the call
    instead, and the caller gets the wait to honour itself.
    """
    asked = _retry_after_seconds(response)
    if asked is not None:
        return asked
    backoff = min(2.0**attempt, _MAX_BACKOFF_SECONDS)
    return random.uniform(backoff / 2, backoff)  # noqa: S311 - jitter, not security


def graph_link_matches(link: str, resource_path: str) -> bool:
    """True if `link` is a Graph v1.0 URL for exactly `resource_path`.

    Used for `@odata.nextLink` values a caller hands back as a cursor. It must
    be https on the Graph host, and name the same resource the rest of the call
    names, so a cursor taken from one chat cannot be replayed against another.
    Graph may percent-encode an id in the path, so both sides are compared
    decoded and case-folded.
    """
    try:
        parts = urlsplit(link)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.hostname != _GRAPH_HOST or parts.port is not None:
        return False
    if parts.username or parts.password or parts.fragment:
        return False
    expected = f"{_GRAPH_VERSION_PREFIX}{resource_path}"
    return unquote(parts.path).casefold() == unquote(expected).casefold()


async def _get_access_token(credentials: Dict[str, str]) -> str:
    """Exchange refresh token for access token."""
    refresh_token = credentials.get("microsoft_refresh_token")
    if not refresh_token:
        raise GraphAuthError("Missing X-Microsoft-Refresh-Token header")

    client_id = credentials.get("microsoft_client_id")
    client_secret = credentials.get("microsoft_client_secret")
    tenant_id = credentials.get("microsoft_tenant_id") or "common"

    if not client_id or not client_secret:
        raise GraphAuthError(
            "Missing OAuth credentials. Pass client_id and client_secret via headers."
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL.format(tenant=tenant_id),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "https://graph.microsoft.com/.default offline_access",
            },
        )

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except Exception:
                error_data = {}
            error_desc = error_data.get("error_description", response.text[:500])
            raise GraphAuthError(f"Token refresh failed ({response.status_code}): {error_desc}")

        token_data = response.json()
        if "access_token" not in token_data:
            raise GraphAuthError("Token endpoint returned unexpected response (no access_token)")
        return token_data["access_token"]

async def _graph_request(credentials: Dict[str, str],
    method: str,
    endpoint: str,
    json_data: Optional[Dict] = None,
    params: Optional[Dict] = None,
    access_token: Optional[str] = None,
    prefer: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Make authenticated request to Microsoft Graph API.

    Pass `access_token` to reuse one already fetched (e.g. across several
    calls in the same request) instead of refreshing it per call.
    Pass `prefer` to add a Graph `Prefer` header value, e.g.
    IdType="ImmutableId" so a created message's id survives folder moves.
    Pass `url` to request an absolute Graph URL instead of `endpoint`, e.g. an
    `@odata.nextLink`; the caller must have checked it with
    `graph_link_matches`, and `params` must be None since the link carries its
    own query.

    A 429 is retried after Graph's Retry-After (or a backoff) within a fixed
    budget, then raised as GraphRetryLaterError; so are 503 and 504 on a read.
    A write answered 503 or 504 is not resent, since it may have been applied."""
    if url is not None:
        if params:
            raise ValueError("An absolute Graph URL carries its own query; pass no params.")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != _GRAPH_HOST:
            raise ValueError("Refusing to send a Graph token to a non-Graph URL.")

    if access_token is None:
        access_token = await _get_access_token(credentials)

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Collect Prefer values: $search needs the non-indexed-query hint, and
    # callers may request more (e.g. IdType="ImmutableId" on message create).
    prefer_values: List[str] = []
    if params and "$search" in params:
        headers["ConsistencyLevel"] = "eventual"
        prefer_values.append("HonorNonIndexedQueriesWarning=true")
    if prefer:
        prefer_values.append(prefer)
    if prefer_values:
        headers["Prefer"] = ", ".join(prefer_values)

    retry_statuses = _RETRY_STATUSES if method.upper() in _READ_METHODS else _WRITE_RETRY_STATUSES
    deadline = time.monotonic() + _RETRY_BUDGET_SECONDS
    async with httpx.AsyncClient(timeout=30.0) as client:
        attempt = 0
        while True:
            response = await client.request(
                method=method,
                url=url or f"{GRAPH_API_URL}{endpoint}",
                headers=headers,
                json=json_data,
                params=params,
            )
            if response.status_code not in retry_statuses:
                break
            delay = _retry_delay(response, attempt)
            if attempt >= _MAX_RETRIES or time.monotonic() + delay > deadline:
                retry_after = _retry_after_seconds(response)
                if response.status_code == 429:
                    raise GraphRetryLaterError(
                        "Rate limit exceeded. Try again later.", 429, retry_after
                    )
                raise GraphRetryLaterError(
                    f"Microsoft Graph is temporarily unavailable ({response.status_code}). "
                    "Try again later.",
                    response.status_code,
                    retry_after,
                )
            attempt += 1
            await asyncio.sleep(delay)

        if response.status_code == 401:
            raise GraphAuthError("Authentication failed. Please re-authenticate.")
        elif response.status_code == 403:
            raise GraphForbiddenError("Access denied. Check permissions.")
        elif response.status_code == 404:
            raise GraphNotFoundError("Resource not found.")
        elif response.status_code >= 400:
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            error_msg = error_data.get("error", {}).get("message", response.text[:500])
            raise ValueError(f"Graph API error ({response.status_code}): {error_msg}")

        if response.status_code in (204, 202):
            return {}
        return response.json()
