"""Shared Microsoft Graph API utilities."""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
# Hosts an app-only Graph request may target via an absolute URL (deltaLink
# pagination). Anything else is rejected before the bearer token is sent —
# stops a poisoned delta_link from exfiltrating the token.
_ALLOWED_GRAPH_HOSTS = frozenset({"graph.microsoft.com"})
# App-only token cache: refresh `_APP_ONLY_REFRESH_LEEWAY` seconds before the
# Azure AD-reported expiry so we never present an expired token to Graph.
_APP_ONLY_REFRESH_LEEWAY = 60  # seconds
# Bounded retry for app-only Graph calls on throttling (429) / transient (503).
# A scheduled folder walk fans out many calls, so respecting Retry-After is
# required to avoid escalating throttling into a hard failure.
_MAX_THROTTLE_RETRIES = 5
_MAX_BACKOFF_SECONDS = 60.0
# Fragment size for chunked attachment uploads. Graph requires each fragment
# (except the last) to be a multiple of 320 KiB; 5 MiB = 320 KiB * 16.
_UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024


class GraphAuthError(ValueError):
    """The caller has no usable Microsoft credentials: a missing refresh token or
    OAuth app credentials, a failed token refresh, or a 401 from Graph. Tools
    surface its message so the caller can tell "not connected" from a failure."""


class SendUnconfirmed(ValueError):
    """A /send call failed in transit (a timeout or a dropped connection).

    Graph may have sent the message anyway, and the draft id is then the id of
    the sent message, so the caller must keep the draft rather than delete it,
    and the person is told to check Sent Items before sending again."""


SEND_UNCONFIRMED = (
    "Outlook did not confirm the send in time; it may have gone out. "
    "Check Sent Items before sending it again."
)


_AUTH_REASON_MAX_CHARS = 200


def auth_reason(reason: Any) -> str:
    """The identity provider's own explanation, bounded for a tool result.

    A GraphAuthError message reaches the caller, so only a structured reason
    (an OAuth `error_description`, a Graph `error.message`) goes into it, cut
    to its first line and _AUTH_REASON_MAX_CHARS. A raw response body never
    does: without a structured reason this returns "no reason given"."""
    if not isinstance(reason, str) or not reason.strip():
        return "no reason given"
    line = " ".join(reason.strip().splitlines()[0].split())
    if len(line) > _AUTH_REASON_MAX_CHARS:
        line = line[: _AUTH_REASON_MAX_CHARS - 3].rstrip() + "..."
    return line


def graph_failure(action: str, exc: Exception) -> Dict[str, Any]:
    """Error result for a tool whose Graph call raised.

    A credential problem keeps its message, in the "<action>: <reason>" shape the
    calendar connector uses, so the caller sees the mailbox is not connected
    instead of an opaque failure it would retry. A send that may have gone out
    (SendUnconfirmed) keeps its message too, so nobody resends blindly.
    Anything else stays a bare `action`, so Graph internals do not leak."""
    if isinstance(exc, (GraphAuthError, SendUnconfirmed)):
        return {"success": False, "error": f"{action}: {exc}"}
    return {"success": False, "error": action}


class GraphNotFoundError(ValueError):
    """Raised only for an actual Graph 404 — distinguishes a real "resource
    doesn't exist" from other errors whose message text happens to mention
    "not found" (e.g. a 400 for a malformed id)."""


def _throttle_delay_seconds(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429/503: honor Retry-After when present,
    else exponential backoff (2**attempt) capped at _MAX_BACKOFF_SECONDS."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(int(retry_after)), _MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    return min(2.0**attempt, _MAX_BACKOFF_SECONDS)


class MicrosoftGraph:
    """Provides Microsoft Graph API access."""

    async def _get_access_token(self, credentials: Dict[str, str]) -> str:
        """Exchange refresh token for access token."""
        refresh_token = credentials.get("microsoft_refresh_token")
        if not refresh_token:
            raise ValueError("Missing X-Microsoft-Refresh-Token header")

        client_id = credentials.get("microsoft_client_id")
        client_secret = credentials.get("microsoft_client_secret")
        tenant_id = credentials.get("microsoft_tenant_id") or "common"

        if not client_id or not client_secret:
            raise ValueError(
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
                raise ValueError(f"Token refresh failed ({response.status_code}): {error_desc}")

            token_data = response.json()
            if "access_token" not in token_data:
                raise ValueError("Token endpoint returned unexpected response (no access_token)")
            return token_data["access_token"]

    async def _graph_request(
        self,
        credentials: Dict[str, str],
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        access_token: Optional[str] = None,
        prefer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make authenticated request to Microsoft Graph API.

        Pass `access_token` to reuse one already fetched (e.g. across several
        calls in the same request) instead of refreshing it per call.
        Pass `prefer` to add a Graph `Prefer` header value, e.g.
        IdType="ImmutableId" so a created message's id survives folder moves."""
        if access_token is None:
            access_token = await self._get_access_token(credentials)

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

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method,
                url=f"{GRAPH_API_URL}{endpoint}",
                headers=headers,
                json=json_data,
                params=params,
            )

            if response.status_code == 401:
                raise ValueError("Authentication failed. Please re-authenticate.")
            elif response.status_code == 403:
                raise ValueError("Access denied. Check permissions.")
            elif response.status_code == 404:
                raise GraphNotFoundError("Resource not found.")
            elif response.status_code == 429:
                raise ValueError("Rate limit exceeded. Try again later.")
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

    async def _upload_large_attachment(
        self,
        credentials: Dict[str, str],
        message_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attach a file too large to base64-inline into a create/send request.

        Streams it into an existing draft message via Graph's chunked
        upload-session API (supports up to 150MB, vs. a few MB inline).

        Pass `access_token` to reuse one already fetched — useful when
        uploading several large attachments in one call, so each doesn't
        trigger its own token refresh."""
        session = await self._graph_request(
            credentials,
            "POST",
            f"/me/messages/{message_id}/attachments/createUploadSession",
            json_data={
                "AttachmentItem": {
                    "attachmentType": "file",
                    "name": filename,
                    "size": len(content),
                    "contentType": content_type,
                }
            },
            access_token=access_token,
        )
        upload_url = session["uploadUrl"]
        total = len(content)

        # The upload URL is pre-authorized by Graph; it must NOT receive our
        # bearer token, so this uses a bare httpx client, not _graph_request.
        async with httpx.AsyncClient(timeout=60.0) as client:
            attachment: Dict[str, Any] = {}
            for start in range(0, total, _UPLOAD_CHUNK_SIZE):
                end = min(start + _UPLOAD_CHUNK_SIZE, total) - 1
                chunk = content[start : end + 1]
                response = await client.put(
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    },
                    content=chunk,
                )
                response.raise_for_status()
                # Graph completes an attachment upload session with 201 Created
                # and an empty body (just a Location header). Only parse when a
                # body is actually present, otherwise response.json() raises
                # JSONDecodeError on the empty completion response.
                if response.status_code == 201 and response.content:
                    attachment = response.json()
            return attachment

    # Process-wide app-only token cache: (tenant_id, client_id) -> (token,
    # expires_at_monotonic_s). Keyed by tenant+client, so a single shared cache
    # never leaks a token across tenants/apps even if several MicrosoftGraph
    # subclasses run in one process. The lock collapses concurrent cold-cache
    # fetches into one; it's created here at class-definition time (not lazily)
    # so two coroutines hitting an empty cache can't each build a *different*
    # lock and both fetch. (asyncio.Lock() needs no running loop to construct
    # on Python 3.10+.)
    _app_only_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
    _app_only_token_lock: asyncio.Lock = asyncio.Lock()

    async def _get_app_only_token(self, credentials: Dict[str, str]) -> str:
        """Acquire an app-only access token via client credentials grant.

        Used for service-to-service flows (e.g., the scheduled SharePoint
        sync use case) where no user is in the loop. Requires an Azure AD
        app with admin-consented application permissions on the target
        Graph scopes (e.g., Sites.Read.All, Files.Read.All).

        Tokens are cached per (tenant_id, client_id) until ~1 minute before
        their reported expiry, so a multi-thousand-call sync does not hammer
        Azure AD's token endpoint and trigger throttling.
        """
        client_id = credentials.get("microsoft_client_id")
        client_secret = credentials.get("microsoft_client_secret")
        tenant_id = credentials.get("microsoft_tenant_id")

        if not client_id or not client_secret:
            raise ValueError(
                "Missing app-only credentials. Pass client_id and client_secret via headers."
            )
        if not tenant_id:
            raise ValueError("Missing tenant_id. App-only OAuth requires an explicit tenant id.")

        cache_key = (tenant_id, client_id)
        now = time.monotonic()

        # Fast path: cache hit.
        cached = self._app_only_token_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

        # Slow path: refresh under the shared lock so concurrent callers collapse.
        async with self._app_only_token_lock:
            # Re-check under the lock — another coroutine may have refreshed.
            cached = self._app_only_token_cache.get(cache_key)
            if cached and cached[1] > time.monotonic():
                return cached[0]

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    TOKEN_URL.format(tenant=tenant_id),
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                        "grant_type": "client_credentials",
                    },
                )

                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                    except Exception:
                        error_data = {}
                    error_desc = error_data.get(
                        "error_description", response.text[:500] or "Token acquisition failed"
                    )
                    raise ValueError(
                        f"App-only token acquisition failed ({response.status_code}): {error_desc}"
                    )

                token_data = response.json()
                if "access_token" not in token_data:
                    raise ValueError(
                        "Token endpoint returned unexpected response (no access_token)."
                    )

                token = token_data["access_token"]
                # `expires_in` is seconds-from-now per OAuth2. Default to 3600
                # if Azure ever omits it (shouldn't, but be defensive). Refresh
                # _APP_ONLY_REFRESH_LEEWAY seconds early so we never present an
                # expired token mid-flight.
                expires_in = int(token_data.get("expires_in", 3600))
                expires_at = time.monotonic() + max(0, expires_in - _APP_ONLY_REFRESH_LEEWAY)
                self._app_only_token_cache[cache_key] = (token, expires_at)
                return token

    async def _graph_request_app_only(
        self,
        credentials: Dict[str, str],
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make a Graph API request using app-only OAuth.

        `endpoint` accepts either a relative path (e.g., "/drives/{id}/root/delta")
        or an absolute URL (e.g., a returned @odata.deltaLink). Both are supported
        so that delta pagination works naturally.

        Returns {"_error": "delta_expired", "_status": 410} on HTTP 410 — callers
        should reset their deltaLink and re-list from scratch rather than treat it
        as a hard failure.
        """
        # SSRF guard: pin absolute URLs to graph.microsoft.com before the
        # bearer token is acquired. A poisoned delta_link must not be able to
        # cause the app-only Graph token to be sent to an attacker-controlled
        # host. Relative paths always join GRAPH_API_URL and are safe.
        if endpoint.startswith("http"):
            host = (urlparse(endpoint).hostname or "").lower()
            if host not in _ALLOWED_GRAPH_HOSTS:
                raise ValueError(
                    f"App-only Graph request rejected: absolute URL must target "
                    f"graph.microsoft.com (got host={host!r})."
                )
            url = endpoint
        else:
            url = f"{GRAPH_API_URL}{endpoint}"

        access_token = await self._get_app_only_token(credentials)

        headers: Dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            attempt = 0
            while True:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    params=params,
                )
                # Retry throttling / transient errors with backoff before
                # surfacing them. Token stays valid across retries (~1h life).
                if response.status_code in (429, 503) and attempt < _MAX_THROTTLE_RETRIES:
                    await asyncio.sleep(_throttle_delay_seconds(response, attempt))
                    attempt += 1
                    continue
                break

            if response.status_code in (401, 403):
                try:
                    error_data = response.json() if response.content else {}
                except Exception:
                    error_data = {}
                detail = error_data.get("error", {}).get("message", response.text[:500])
                if response.status_code == 401:
                    raise ValueError(
                        f"Authentication failed. Check app credentials and admin consent. ({detail})"
                    )
                raise ValueError(
                    "Access denied. Verify the app has the required admin-consented "
                    "application permission for this resource (e.g. Mail.Read for mailbox "
                    "access, or a Sites.Selected per-site grant for SharePoint). "
                    f"({detail})"
                )
            elif response.status_code == 404:
                try:
                    error_data = response.json() if response.content else {}
                except Exception:
                    error_data = {}
                detail = error_data.get("error", {}).get("message", response.text[:500])
                raise GraphNotFoundError(
                    f"Resource not found. ({detail})" if detail else "Resource not found."
                )
            elif response.status_code == 410:
                # deltaLink expired — caller should reset and re-list.
                return {"_error": "delta_expired", "_status": 410}
            elif response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "unknown")
                raise ValueError(
                    f"Rate limited after {attempt} retries. Retry-After: {retry_after}s"
                )
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
