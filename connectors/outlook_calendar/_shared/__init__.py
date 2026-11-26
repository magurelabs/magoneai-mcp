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

from typing import Any, Dict, List, Optional

import httpx
from magone_mcp_core import UpstreamError


class GraphNotFoundError(UpstreamError):
    """Raised only for an actual Graph 404 — distinguishes a real "resource
    doesn't exist" from other errors whose message text happens to mention
    "not found" (e.g. a 400 for a malformed id)."""

    def __init__(self, message: str):
        super().__init__(message, code="not_found", status=404)


GRAPH_API_URL = "https://graph.microsoft.com/v1.0"

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"



async def _get_access_token(credentials: Dict[str, str]) -> str:
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

async def _graph_request(credentials: Dict[str, str],
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

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method=method,
            url=f"{GRAPH_API_URL}{endpoint}",
            headers=headers,
            json=json_data,
            params=params,
        )

        # An UpstreamError (a ValueError) carrying the code a host acts on,
        # and Graph's Retry-After on a throttle (magone_mcp_core.errors).
        status = response.status_code
        if status == 404:
            raise GraphNotFoundError("Resource not found.")
        if status >= 400:
            if status == 401:
                message = "Authentication failed. Please re-authenticate."
            elif status == 403:
                message = "Access denied. Check permissions."
            elif status == 429:
                message = "Rate limit exceeded. Try again later."
            else:
                try:
                    error_data = response.json() if response.content else {}
                except Exception:
                    error_data = {}
                error_msg = error_data.get("error", {}).get("message", response.text[:500])
                message = f"Graph API error ({status}): {error_msg}"
            raise UpstreamError.from_status(message, status, response.headers)

        if response.status_code in (204, 202):
            return {}
        return response.json()
