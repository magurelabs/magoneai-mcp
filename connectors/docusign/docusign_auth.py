"""Docusign OAuth mixin.

Exchanges the platform-supplied refresh token for a short-lived access
token, then resolves the account's API base URI via /oauth/userinfo.
Mirrors shared/microsoft_graph.py's pattern (per-server here since only
docusign uses it today; promote to shared/ if a second Docusign-family
server appears).
"""

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

# Sandbox (account-d.docusign.com) by default — set DOCUSIGN_AUTH_SERVER to
# account.docusign.com for production. server.py imports this module only
# after calling load_env(), so a value set in .env is picked up here too.
AUTH_SERVER = os.environ.get("DOCUSIGN_AUTH_SERVER", "account-d.docusign.com")
TOKEN_URL = f"https://{AUTH_SERVER}/oauth/token"
USERINFO_URL = f"https://{AUTH_SERVER}/oauth/userinfo"

# Refresh this many seconds before the token's reported expiry, so a
# cached token is never presented to the eSignature API already expired.
_TOKEN_REFRESH_LEEWAY = 60  # seconds


def _raise_for_docusign_error(response: httpx.Response, context: str) -> None:
    """Docusign's error body is {"error": ..., "error_description": ...} on
    the OAuth endpoints. httpx.HTTPStatusError isn't in protocol.py's
    _SAFE_EXCEPTIONS, so left to raise_for_status() any non-2xx here surfaces
    to the agent as an opaque "Internal error" instead of the actual reason
    (expired/revoked consent, bad client id, rate limit, ...) - always raise
    ValueError instead, with whatever Docusign told us."""
    if response.status_code < 400:
        return
    try:
        error_data = response.json()
    except Exception:
        error_data = {}
    detail = error_data.get("error_description") or error_data.get("error") or response.text
    raise ValueError(f"{context} ({response.status_code}): {detail}")


class DocusignAuth:
    """Provides authenticated access to the Docusign eSignature REST API."""

    # Process-wide cache: (client_id, refresh_token) -> (access_token,
    # account dict, expires_at_monotonic_s). One _esign_auth() call today
    # costs a token exchange *and* a /oauth/userinfo round trip; caching both
    # together means a burst of tool calls (e.g. get_envelope_status's two
    # requests, or a workflow chaining several docusign tools) pays that
    # cost once instead of per call. Keyed by (client_id, refresh_token) so
    # distinct connections never share a cached token. Lock is created at
    # class-definition time (needs no running loop on Python 3.10+) so two
    # coroutines racing a cold cache can't each build a different lock.
    _token_cache: Dict[Tuple[str, str], Tuple[str, Dict[str, str], float]] = {}
    _token_cache_lock: asyncio.Lock = asyncio.Lock()

    async def _get_access_token(self, credentials: Dict[str, str]) -> str:
        """Exchange refresh token for a fresh access token (uncached — callers
        needing the account too should use _esign_auth, which caches)."""
        access_token, _ = await self._get_access_token_with_expiry(credentials)
        return access_token

    async def _get_access_token_with_expiry(self, credentials: Dict[str, str]) -> Tuple[str, int]:
        """Like _get_access_token, but also returns `expires_in` for caching."""
        refresh_token = credentials.get("docusign_refresh_token")
        if not refresh_token:
            raise ValueError("Missing X-Docusign-Refresh-Token header")

        client_id = credentials.get("docusign_client_id")
        client_secret = credentials.get("docusign_client_secret")
        if not client_id or not client_secret:
            raise ValueError(
                "Missing OAuth credentials. Pass client_id and client_secret via headers."
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL,
                # Docusign documents client authentication via HTTP Basic
                # (base64 client_id:client_secret), not body-form client_id/
                # client_secret fields - use httpx's `auth` so it's built
                # correctly rather than guessing at the encoding by hand.
                auth=(client_id, client_secret),
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            )
            _raise_for_docusign_error(response, "Token refresh failed")
            token_data = response.json()
            if "access_token" not in token_data:
                raise ValueError("Token endpoint returned unexpected response (no access_token)")
            return token_data["access_token"], int(token_data.get("expires_in", 3600))

    async def _get_account_info(self, access_token: str) -> Dict[str, str]:
        """Resolve the default account's id and eSignature base URI."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            _raise_for_docusign_error(response, "Failed to resolve account info")
            data = response.json()

        accounts = data.get("accounts", [])
        if not accounts:
            raise ValueError("Docusign user has no accounts")

        default_account = next((a for a in accounts if a.get("is_default")), accounts[0])
        return {
            "account_id": default_account["account_id"],
            "base_uri": default_account["base_uri"],
            "user_id": data.get("sub", ""),
        }

    async def _esign_auth(self, credentials: Dict[str, str]) -> Tuple[str, Dict[str, str]]:
        """Resolve a fresh (access_token, account) pair, from cache when possible.

        Also caches expires_in from the token response, refreshing
        `_TOKEN_REFRESH_LEEWAY` seconds early so a cached token is never
        handed to the eSignature API already expired.
        """
        refresh_token = credentials.get("docusign_refresh_token")
        client_id = credentials.get("docusign_client_id")
        if not refresh_token or not client_id:
            # Let _get_access_token raise its normal, specific error.
            access_token = await self._get_access_token(credentials)
            account = await self._get_account_info(access_token)
            return access_token, account

        cache_key = (client_id, refresh_token)
        now = time.monotonic()

        cached = self._token_cache.get(cache_key)
        if cached and cached[2] > now:
            return cached[0], cached[1]

        async with self._token_cache_lock:
            cached = self._token_cache.get(cache_key)
            if cached and cached[2] > time.monotonic():
                return cached[0], cached[1]

            access_token, expires_in = await self._get_access_token_with_expiry(credentials)
            account = await self._get_account_info(access_token)
            expires_at = time.monotonic() + max(0, expires_in - _TOKEN_REFRESH_LEEWAY)
            self._token_cache[cache_key] = (access_token, account, expires_at)
            return access_token, account

    async def _esign_request(
        self,
        credentials: Dict[str, str],
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        _auth: Optional[Tuple[str, Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated JSON request against the eSignature REST API.

        `path` is relative to `/restapi/v2.1/accounts/{account_id}`, e.g.
        `/envelopes`. Pass `_auth` (from a prior `_esign_auth` call) to skip
        re-resolving the access token and account when the caller already has
        them — avoids a redundant refresh-token exchange + userinfo round trip.
        """
        access_token, account = _auth if _auth else await self._esign_auth(credentials)
        url = f"{account['base_uri']}/restapi/v2.1/accounts/{account['account_id']}{path}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                json=json_data,
                params=params,
            )
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"message": response.text}
                raise ValueError(
                    f"Docusign API error ({response.status_code}): "
                    f"{error_data.get('message', error_data)}"
                )
            if not response.content:
                return {}
            return response.json()

    async def _esign_request_binary(
        self, credentials: Dict[str, str], path: str
    ) -> Tuple[bytes, str]:
        """GET a binary response (e.g. a document) from the eSignature REST API.

        Returns (content_bytes, content_type).
        """
        access_token, account = await self._esign_auth(credentials)
        url = f"{account['base_uri']}/restapi/v2.1/accounts/{account['account_id']}{path}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if response.status_code >= 400:
                raise ValueError(f"Docusign API error ({response.status_code}): {response.text}")
            return response.content, response.headers.get(
                "content-type", "application/octet-stream"
            )
