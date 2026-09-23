"""Vendored helpers from the v1 repository-wide shared package.

The port created this package with only a docstring and vendored nothing, so
every Google connector failed at import on the first line that used it.

`_google_request` had a second problem. In v1 it was a mixin method called as
`self._google_request(...)`, and the codemod that converts mixin methods to
module-level functions stripped the `self` from the call sites but never
carried the method across. The result was twelve call sites across three
connectors invoking a name that exists nowhere, each with a
`# type: ignore[attr-defined]` suppressing the only warning anyone would have
seen. Those tools parsed, imported, listed and would have raised `NameError`
the moment a model called one.

Both are restored here as module-level functions, which is the shape the
ported call sites already expect.
"""

from typing import Any, Dict, Optional

import httpx

# Declared on each tool so the framework merges these request headers into the
# `credentials` dict a handler receives. The names are the wire contract with
# the platform's connection form and must not be renamed casually: a header
# the platform still sends under the old name arrives as nothing at all.
GOOGLE_CREDENTIAL_HEADERS = [
    "X-Google-Refresh-Token",
    "X-Google-Client-Id",
    "X-Google-Client-Secret",
]

GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

__all__ = [
    "GOOGLE_CREDENTIAL_HEADERS",
    "GOOGLE_OAUTH_TOKEN_URL",
    "google_get_access_token",
    "_google_request",
]


async def google_get_access_token(
    credentials: Dict[str, str], scopes: Optional[str] = None
) -> str:
    """Exchange the caller's refresh token for an access token.

    The credentials are the caller's own, arriving as request headers. Nothing
    here is a platform credential, which is what lets this connector run
    against somebody else's Google account from a laptop.
    """
    refresh_token = credentials.get("google_refresh_token")
    if not refresh_token:
        raise ValueError("Missing X-Google-Refresh-Token header")

    client_id = credentials.get("google_client_id")
    client_secret = credentials.get("google_client_secret")
    if not client_id or not client_secret:
        raise ValueError(
            "Missing OAuth credentials. Pass client_id and client_secret via headers."
        )

    data: Dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if scopes:
        data["scope"] = scopes

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_OAUTH_TOKEN_URL, data=data)

    if response.status_code == 400:
        try:
            error_data = response.json()
        except ValueError:
            error_data = {}
        raise ValueError(
            f"Token refresh failed: {error_data.get('error_description', 'Token refresh failed')}"
        )

    response.raise_for_status()
    access_token = response.json().get("access_token")
    if not access_token:
        raise ValueError("Token endpoint returned unexpected response (no access_token)")
    return access_token


async def _google_request(
    credentials: Dict[str, str],
    method: str,
    base_url: str,
    path: str,
    json_data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    scopes: Optional[str] = None,
) -> Dict[str, Any]:
    """An authenticated JSON request to a Google API.

    Keeps the leading underscore because twelve ported call sites already spell
    it that way. Renaming it would be tidier and would mean editing carried-across
    handler bodies, which is the one thing the port is trying not to do.
    """
    access_token = await google_get_access_token(credentials, scopes=scopes)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method=method,
            url=f"{base_url}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=json_data,
            params=params,
        )

    if response.status_code == 401:
        raise ValueError("Authentication failed. Please re-authenticate.")
    if response.status_code == 403:
        raise ValueError("Access denied. Check permissions and scopes.")
    if response.status_code == 404:
        raise ValueError("Resource not found.")
    if response.status_code == 429:
        raise ValueError("Rate limit exceeded. Try again later.")
    if response.status_code >= 400:
        try:
            error_data = response.json() if response.content else {}
        except ValueError:
            error_data = {}
        error_msg = error_data.get("error", response.text[:500])
        raise ValueError(f"Google API error ({response.status_code}): {error_msg}")

    if response.status_code in (204, 202):
        return {}
    return response.json()
