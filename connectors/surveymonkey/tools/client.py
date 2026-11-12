"""Shared HTTP client for the SurveyMonkey v3 API.

Auth is a single static bearer token (a SurveyMonkey Private App access
token) - no refresh flow needed, unlike an OAuth server. Error bodies are
SurveyMonkey's own ``{"error": {id, name, message, http_status_code}}``
shape (confirmed against SurveyMonkey/public_api_docs, 2026-08), not a
generic REST error - map it explicitly rather than guessing at a shape.
"""

from typing import Any, Dict, Optional

import httpx

BASE_URL = "https://api.surveymonkey.com/v3"
CREDENTIAL_HEADERS = ["X-Surveymonkey-Access-Token"]


def _token(credentials: Dict[str, str]) -> str:
    token = credentials.get("surveymonkey_access_token") or credentials.get("access_token")
    if not token:
        raise ValueError("SurveyMonkey access token is required")
    return token


def compact(params: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        err = body.get("error") or {}
        name = err.get("name") or response.reason_phrase
        message = err.get("message") or ""
        code = err.get("id")
        return f"{name} (code {code}): {message}".strip() if code is not None else str(name)
    except Exception:
        return response.text[:500]


async def request(
    credentials: Dict[str, str],
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    """Call the SurveyMonkey v3 API and raise ValueError with a clear message on failure.

    Connection-level failures (DNS, firewall/proxy blocking, timeout) are
    caught explicitly here rather than left to propagate as a raw httpx
    exception - unhandled, protocol.py's generic exception handler reports
    them as an opaque "Internal error (ConnectTimeout)" with no indication
    it's a network problem, not a bug (this is exactly what happened
    debugging a real deployment - the fix belongs here, not in a resend).
    """
    # Read at call time: server.py imports this module, so a top-level
    # import of server.py from here would be circular.
    from ..server import USER_AGENT

    token = _token(credentials)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
            response = await client.request(
                method,
                path,
                params=compact(params or {}),
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
    except httpx.ConnectError as exc:
        raise ValueError(
            "Could not connect to SurveyMonkey's API (api.surveymonkey.com). This is a "
            "network problem, not a bad request - check for a firewall, proxy, or VPN "
            "blocking outbound HTTPS from wherever this server is running."
        ) from exc
    except httpx.TimeoutException as exc:
        raise ValueError(
            "SurveyMonkey API request timed out. If this keeps happening, the same "
            "network path is likely blocking or throttling the connection - check "
            "firewall/proxy/VPN settings rather than retrying."
        ) from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"SurveyMonkey API request failed: {exc}") from exc

    if response.status_code == 401:
        raise ValueError(f"SurveyMonkey authentication failed: {_error_message(response)}")
    if response.status_code == 403:
        raise ValueError(f"SurveyMonkey authorization failed: {_error_message(response)}")
    if response.status_code == 429:
        reset = response.headers.get("X-Ratelimit-App-Global-Minute-Reset") or response.headers.get(
            "X-Ratelimit-App-Global-Day-Reset"
        )
        suffix = f"; resets in {reset}s" if reset else ""
        raise ValueError(f"SurveyMonkey rate limit exceeded{suffix}")
    if response.status_code in (502, 503):
        raise ValueError(f"SurveyMonkey API temporarily unavailable: {_error_message(response)}")
    if response.status_code >= 400:
        raise ValueError(
            f"SurveyMonkey API error ({response.status_code}): {_error_message(response)}"
        )

    if not response.content:
        return None
    return response.json()
