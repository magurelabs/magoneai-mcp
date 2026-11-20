"""Vendored Microsoft Graph helpers for the OneDrive connector.

Delegated flow only: every tool acts as the signed-in user on their own drive
(`/me/drive`), so the caller sends that user's refresh token plus the Azure
app's client id and secret, and this module exchanges them for an access token
on each call. Nothing is cached and nothing is written down: the credentials
arrive per request and leave with it.

Vendored rather than imported from a sibling, which is this repository's
deliberate pattern: a connector is published as an independent package and
must not import another connector.

## Scopes

OneDrive has its own scope list, `ONEDRIVE_OAUTH_SCOPES`. Uploading needs
write access, and the other Microsoft connectors here (Outlook, Teams,
SharePoint) neither need nor ask for it, so nothing they declare changes.
`Files.ReadWrite` rather than `Files.ReadWrite.All`: every tool addresses the
signed-in user's own drive, which `Files.ReadWrite` covers. The `.All` variant
only adds files other people shared, which no tool writes to.

The connection's consent screen is built by whoever holds the OAuth app (the
platform, or you on a laptop), not by this server. The list is here so the
requirement is written next to the code that depends on it, and each tool
declares the one Graph scope it needs as `required_scopes`.
"""

from typing import Any

import httpx

GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105 (a URL)

FILES_READWRITE_SCOPE = "https://graph.microsoft.com/Files.ReadWrite"
ONEDRIVE_OAUTH_SCOPES = ["offline_access", "User.Read", FILES_READWRITE_SCOPE]

# The delegated Microsoft credential set, the same headers every other
# delegated Microsoft connector here reads. The framework folds each into the
# handler's `credentials` dict: `X-Microsoft-Refresh-Token` arrives as
# `credentials["microsoft_refresh_token"]`.
MICROSOFT_CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

# The refresh asks for whatever the app was consented for, as v1 did. A token
# consented without Files.ReadWrite then fails at Graph with a 403 that names
# the fix, rather than at the token endpoint with a consent error.
_REFRESH_SCOPE = "https://graph.microsoft.com/.default offline_access"
_TOKEN_TIMEOUT = 30.0
_GRAPH_TIMEOUT = 30.0
_MAX_ERROR_DETAIL = 500


class GraphNotFoundError(ValueError):
    """An actual Graph 404, as distinct from any error that mentions "not found"."""


async def get_access_token(credentials: dict[str, str]) -> str:
    """Exchange the caller's refresh token for a Graph access token."""
    refresh_token = credentials.get("microsoft_refresh_token")
    if not refresh_token:
        raise ValueError("Missing X-Microsoft-Refresh-Token header")
    client_id = credentials.get("microsoft_client_id")
    client_secret = credentials.get("microsoft_client_secret")
    tenant_id = credentials.get("microsoft_tenant_id") or "common"
    if not client_id or not client_secret:
        raise ValueError(
            "Missing OAuth credentials. Pass X-Microsoft-Client-Id and "
            "X-Microsoft-Client-Secret headers."
        )

    async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
        response = await client.post(
            TOKEN_URL.format(tenant=tenant_id),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": _REFRESH_SCOPE,
            },
        )
    if response.status_code >= 400:
        detail = _json(response).get("error_description") or response.text[:_MAX_ERROR_DETAIL]
        raise ValueError(f"Token refresh failed ({response.status_code}): {detail}")
    token = _json(response).get("access_token")
    if not token:
        raise ValueError("Token endpoint returned unexpected response (no access_token)")
    return token


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json() if response.content else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def graph_error(response: httpx.Response) -> ValueError:
    """The exception for a failed Graph response, with a message for the model."""
    status = response.status_code
    if status == 401:
        return ValueError("Authentication failed. Please re-authenticate.")
    if status == 403:
        return ValueError("Access denied. Check permissions.")
    if status == 404:
        return GraphNotFoundError("Resource not found.")
    if status == 429:
        return ValueError("Rate limit exceeded. Try again later.")
    message = (_json(response).get("error") or {}).get("message") or response.text[:300]
    return ValueError(f"Graph API error ({status}): {message}")


async def graph_request(
    credentials: dict[str, str],
    method: str,
    endpoint: str,
    json_data: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """One authenticated Graph call against `GRAPH_API_URL + endpoint`."""
    access_token = await get_access_token(credentials)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_GRAPH_TIMEOUT) as client:
        response = await client.request(
            method=method,
            url=f"{GRAPH_API_URL}{endpoint}",
            headers=headers,
            json=json_data,
            params=params,
        )
    if response.status_code >= 400:
        raise graph_error(response)
    if response.status_code in (202, 204) or not response.content:
        return {}
    return response.json()


__all__ = [
    "FILES_READWRITE_SCOPE",
    "GRAPH_API_URL",
    "MICROSOFT_CREDENTIAL_HEADERS",
    "ONEDRIVE_OAUTH_SCOPES",
    "TOKEN_URL",
    "GraphNotFoundError",
    "get_access_token",
    "graph_error",
    "graph_request",
]
