"""HTTP client helper for the Kapso WhatsApp Cloud API proxy.

Kapso exposes a Meta-shaped WhatsApp Cloud API behind a single base URL with
API-key auth. This wraps httpx with the base URL, the X-API-Key header, and a
uniform success/error response shape so every tool returns the same envelope.

Credential resolution order (covers platform headers, playground file mode, env):
- api key:          credentials["api_key"] | credentials["kapso_api_key"] | KAPSO_API_KEY
- phone_number_id:  per-call arg | credentials["phone_number_id"] | KAPSO_PHONE_NUMBER_ID
"""

import os
from typing import Any, Dict, Optional

import httpx

DEFAULT_BASE_URL = "https://api.kapso.ai/meta/whatsapp"
GRAPH_VERSION = os.getenv("KAPSO_GRAPH_VERSION", "v24.0")
REQUEST_TIMEOUT_SECONDS = 30.0

# Headers the platform forwards per tool call; protocol.py only extracts headers
# a tool explicitly lists, so every @tool must pass this as credential_headers.
CREDENTIAL_HEADERS = ["X-API-Key", "X-Phone-Number-Id"]


def base_url() -> str:
    """Return the Kapso WhatsApp base URL (env-overridable for testing)."""
    return os.getenv("KAPSO_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def resolve_api_key(credentials: Dict[str, str]) -> str:
    """Pull the Kapso API key from headers, file credentials, or env."""
    return (
        credentials.get("api_key")
        or credentials.get("kapso_api_key")
        or os.getenv("KAPSO_API_KEY", "")
    )


def resolve_phone_number_id(credentials: Dict[str, str], phone_number_id: Optional[str]) -> str:
    """Per-call phone_number_id wins, else the connection default, else env."""
    return (
        phone_number_id
        or credentials.get("phone_number_id")
        or os.getenv("KAPSO_PHONE_NUMBER_ID", "")
    )


def _envelope(resp: httpx.Response) -> Dict[str, Any]:
    """Normalize an httpx response into a uniform success/error envelope."""
    try:
        data: Any = resp.json()
    except Exception:
        data = resp.text
    if resp.status_code >= 400:
        return {"success": False, "status": resp.status_code, "error": data}
    return {"success": True, "status": resp.status_code, "data": data}


async def request(
    credentials: Dict[str, str],
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Make a request to the Kapso WhatsApp API and return a uniform envelope.

    `path` is appended to the base URL (e.g. "/conversations"). Pass an absolute
    path beginning with "/". Returns {success, status, data|error}.
    """
    api_key = resolve_api_key(credentials)
    if not api_key:
        return {
            "success": False,
            "status": 401,
            "error": "Missing Kapso API key (set the kapso_api_key credential or KAPSO_API_KEY).",
        }
    url = f"{base_url()}{path}"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.request(
            method, url, headers=headers, params=clean_params or None, json=json_body
        )
    return _envelope(resp)


async def send_message(
    credentials: Dict[str, str],
    phone_number_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """POST a Meta-shaped message payload to the phone number's messages endpoint."""
    body = {"messaging_product": "whatsapp", **payload}
    path = f"/{GRAPH_VERSION}/{phone_number_id}/messages"
    return await request(credentials, "POST", path, json_body=body)
