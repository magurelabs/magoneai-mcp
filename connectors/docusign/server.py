"""The docusign connector.

Ported from the v1 mixin form by a codemod.

14 tools, 9 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

SERVER_NAME = "docusign"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple
import httpx
import sys
from pathlib import Path
from typing import Any, Dict
import base64
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

AUTH_SERVER = os.environ.get("DOCUSIGN_AUTH_SERVER", "account-d.docusign.com")

TOKEN_URL = f"https://{AUTH_SERVER}/oauth/token"

USERINFO_URL = f"https://{AUTH_SERVER}/oauth/userinfo"

_TOKEN_REFRESH_LEEWAY = 60  # seconds

# Access tokens by connection, so every tool call does not re-run the OAuth
# exchange. v1 held these as class attributes of the auth mixin; the port
# flattened the methods that use them and left the attributes behind, so the
# first call to any DocuSign tool raised NameError.
#
# Keyed on a digest of the connection's credentials rather than the refresh
# token itself: a process-lifetime dict keyed by raw tokens is a credential
# store nobody thinks of as one.
_token_cache: Dict[str, Tuple[str, Dict[str, str], float]] = {}
_token_cache_lock = asyncio.Lock()


def _cache_key(client_id: str, refresh_token: str) -> str:
    import hashlib

    return hashlib.sha256(f"{client_id}\x00{refresh_token}".encode()).hexdigest()

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

async def _get_access_token(credentials: Dict[str, str]) -> str:
    """Exchange refresh token for a fresh access token (uncached — callers
    needing the account too should use _esign_auth, which caches)."""
    access_token, _ = await _get_access_token_with_expiry(credentials)
    return access_token

async def _get_access_token_with_expiry(credentials: Dict[str, str]) -> Tuple[str, int]:
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

async def _get_account_info(access_token: str) -> Dict[str, str]:
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

async def _esign_auth(credentials: Dict[str, str]) -> Tuple[str, Dict[str, str]]:
    """Resolve a fresh (access_token, account) pair, from cache when possible.

    Also caches expires_in from the token response, refreshing
    `_TOKEN_REFRESH_LEEWAY` seconds early so a cached token is never
    handed to the eSignature API already expired.
    """
    refresh_token = credentials.get("docusign_refresh_token")
    client_id = credentials.get("docusign_client_id")
    if not refresh_token or not client_id:
        # Let _get_access_token raise its normal, specific error.
        access_token = await _get_access_token(credentials)
        account = await _get_account_info(access_token)
        return access_token, account

    cache_key = _cache_key(client_id, refresh_token)
    now = time.monotonic()

    cached = _token_cache.get(cache_key)
    if cached and cached[2] > now:
        return cached[0], cached[1]

    async with _token_cache_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[2] > time.monotonic():
            return cached[0], cached[1]

        access_token, expires_in = await _get_access_token_with_expiry(credentials)
        account = await _get_account_info(access_token)
        expires_at = time.monotonic() + max(0, expires_in - _TOKEN_REFRESH_LEEWAY)
        _token_cache[cache_key] = (access_token, account, expires_at)
        return access_token, account

async def _esign_request(credentials: Dict[str, str],
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
    access_token, account = _auth if _auth else await _esign_auth(credentials)
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

async def _esign_request_binary(credentials: Dict[str, str], path: str
) -> Tuple[bytes, str]:
    """GET a binary response (e.g. a document) from the eSignature REST API.

    Returns (content_bytes, content_type).
    """
    access_token, account = await _esign_auth(credentials)
    url = f"{account['base_uri']}/restapi/v2.1/accounts/{account['account_id']}{path}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            raise ValueError(f"Docusign API error ({response.status_code}): {response.text}")
        return response.content, response.headers.get(
            "content-type", "application/octet-stream"
        )


CREDENTIAL_HEADERS = [
    "X-Docusign-Refresh-Token",
    "X-Docusign-Client-Id",
    "X-Docusign-Client-Secret",
]

_DATA_URI_RE = re.compile(r"^data:[^;]*;base64,", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"\s+")

def _normalize_base64(value: str) -> str:
    """Strip the wrappings producers commonly add around base64 payloads:
    MIME-style line wrapping (base64.encodebytes) and `data:<mime>;base64,`
    URIs. Without this, `base64.b64decode(..., validate=True)` rejects both
    as "not valid base64" even though the payload itself is fine (same
    normalization as the outlook_email connector's
    normalize_base64 - duplicated here rather than imported since each MCP
    server is a standalone Docker build with no cross-server import path)."""
    text = value.strip()
    match = _DATA_URI_RE.match(text)
    if match:
        text = text[match.end() :]
    return _WHITESPACE_RE.sub("", text)

def _default_from_date() -> str:
    """Last 30 days, matching what list_envelopes/search_documents document."""
    return (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

def _paged_envelope_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Docusign's envelope-list endpoints return `resultSetSize` (this page's
    count) and `totalSetSize` (total matches across all pages) alongside the
    envelopes themselves - both were previously discarded, with `total` set
    to len(page), so "how many envelopes are pending?" silently answered with
    the page size instead of the real count whenever results were truncated."""
    envelopes = data.get("envelopes", [])
    total_available = data.get("totalSetSize")
    try:
        total_available = int(total_available) if total_available is not None else len(envelopes)
    except (TypeError, ValueError):
        total_available = len(envelopes)
    return {
        "envelopes": envelopes,
        "total": len(envelopes),
        "total_available": total_available,
        "has_more": total_available > len(envelopes),
    }



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /envelopes
@server.tool(
    name='create_envelope',
    description="Create and send an envelope for signature. Pass `email_subject`, `signer_email`, `signer_name`, `document_base64` (base64-encoded document bytes), and `document_name` (filename with extension, e.g. contract.pdf). The signer gets a 'Sign here' tag anchored to the literal text `/sign/` in the document — include that marker in the document. Docusign silently drops the signature tab (not an error) if that marker isn't found — use `anchor_string` to match a different literal text. Set `send_now=false` to save as a draft instead of sending. Set `embedded=true` for in-app signing: Docusign does NOT email this signer at all, you must call `get_signing_url` and hand them that link yourself.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_subject': {'type': 'string',
                                      'description': 'Subject of the signing email'},
                    'signer_email': {'type': 'string',
                                     'description': "Signer's email address"},
                    'signer_name': {'type': 'string', 'description': "Signer's full name"},
                    'document_base64': {'type': 'string',
                                        'description': 'Base64-encoded document'},
                    'document_name': {'type': 'string',
                                      'description': 'Document filename with extension'},
                    'send_now': {'type': 'boolean',
                                 'description': 'Send immediately (default: true)',
                                 'default': True},
                    'embedded': {'type': 'boolean',
                                 'description': 'Enable embedded signing via '
                                                'get_signing_url (default: false)',
                                 'default': False},
                    'anchor_string': {'type': 'string',
                                      'description': 'Literal text in the document to '
                                                     'anchor the signature tab to '
                                                     '(default: /sign/)',
                                      'default': '/sign/'}},
     'required': ['email_subject',
                  'signer_email',
                  'signer_name',
                  'document_base64',
                  'document_name']})
@declare_output_schema({'properties': {'envelope_id': {'description': "Created envelope's id", 'type': 'string'},
                                       'status': {'description': 'Resulting envelope status', 'type': 'string'}},
                        'type': 'object'})
async def create_envelope(credentials: Dict[str, str],
    email_subject: str,
    signer_email: str,
    signer_name: str,
    document_base64: str,
    document_name: str,
    send_now: bool = True,
    embedded: bool = False,
    anchor_string: str = "/sign/",
) -> Dict[str, Any]:
    document_base64 = _normalize_base64(document_base64)
    try:
        base64.b64decode(document_base64, validate=True)
    except Exception as e:
        raise ValueError(f"document_base64 is not valid base64: {e}")

    extension = Path(document_name).suffix.lstrip(".") or "pdf"

    signer: Dict[str, Any] = {
        "email": signer_email,
        "name": signer_name,
        "recipientId": "1",
        "routingOrder": "1",
        "tabs": {
            "signHereTabs": [
                {
                    "anchorString": anchor_string,
                    "anchorUnits": "pixels",
                    "anchorXOffset": "0",
                    "anchorYOffset": "0",
                }
            ]
        },
    }
    if embedded:
        # clientUserId marks this recipient for embedded (in-app) signing —
        # required before get_signing_url's recipient-view call will work.
        signer["clientUserId"] = signer_email

    payload = {
        "emailSubject": email_subject,
        "status": "sent" if send_now else "created",
        "documents": [
            {
                "documentBase64": document_base64,
                "name": document_name,
                "fileExtension": extension,
                "documentId": "1",
            }
        ],
        "recipients": {"signers": [signer]},
    }

    data = await _esign_request(credentials, "POST", "/envelopes", json_data=payload)
    return {"envelope_id": data.get("envelopeId"), "status": data.get("status")}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /envelopes
@server.tool(
    name='create_envelope_from_template',
    description="Create and send an envelope from an existing Docusign template. Pass `template_id` (from list_templates), `signer_email`, `signer_name`, and `role_name` (the template's signer role — check list_templates' roles if unsure). Optional `email_subject` overrides the template's default.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'template_id': {'type': 'string', 'description': 'Template id'},
                    'signer_email': {'type': 'string',
                                     'description': "Signer's email address"},
                    'signer_name': {'type': 'string', 'description': "Signer's full name"},
                    'role_name': {'type': 'string',
                                  'description': 'Template signer role name (default: '
                                                 'Signer)',
                                  'default': 'Signer'},
                    'email_subject': {'type': 'string',
                                      'description': "Overrides the template's default "
                                                     'subject'}},
     'required': ['template_id', 'signer_email', 'signer_name']})
@declare_output_schema({'properties': {'envelope_id': {'description': "Created envelope's id", 'type': 'string'},
                                       'status': {'description': 'Resulting envelope status', 'type': 'string'}},
                        'type': 'object'})
async def create_envelope_from_template(credentials: Dict[str, str],
    template_id: str,
    signer_email: str,
    signer_name: str,
    role_name: str = "Signer",
    email_subject: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "templateId": template_id,
        "status": "sent",
        "templateRoles": [
            {
                "email": signer_email,
                "name": signer_name,
                "roleName": role_name,
            }
        ],
    }
    if email_subject:
        payload["emailSubject"] = email_subject

    data = await _esign_request(credentials, "POST", "/envelopes", json_data=payload)
    return {"envelope_id": data.get("envelopeId"), "status": data.get("status")}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='get_account_details',
    description="Get the connected Docusign account's name, id, and settings.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'account': {'type': 'object'}}, 'type': 'object'})
async def get_account_details(credentials: Dict[str, str]) -> Dict[str, Any]:
    data = await _esign_request(credentials, "GET", "")
    return {"account": data}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: _esign_request_binary() -> client.get, returning document bytes.
@server.tool(
    name='get_document',
    description="Download a document from an envelope. Pass `envelope_id` and `document_id` (from get_envelope_status or list_envelopes; use `document_id='combined'` for every document in the envelope merged into one PDF). Returns the file base64-encoded — large documents produce a large response.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'envelope_id': {'type': 'string', 'description': 'Envelope id'},
                    'document_id': {'type': 'string',
                                    'description': "Document id, or 'combined' for all "
                                                   'documents merged',
                                    'default': 'combined'}},
     'required': ['envelope_id']})
@declare_output_schema({'properties': {'content_type': {'description': 'MIME type of the returned file',
                                                        'type': 'string'},
                                       'document_base64': {'description': 'Base64-encoded document',
                                                           'type': 'string'}},
                        'type': 'object'})
async def get_document(credentials: Dict[str, str], envelope_id: str, document_id: str = "combined"
) -> Dict[str, Any]:
    content, content_type = await _esign_request_binary(
        credentials, f"/envelopes/{envelope_id}/documents/{document_id}"
    )
    return {
        "document_base64": base64.b64encode(content).decode("ascii"),
        "content_type": content_type,
    }



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /envelopes/{envelope_id}
@server.tool(
    name='get_envelope_status',
    description="Get the current status and recipient details for an envelope by `envelope_id`.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'envelope_id': {'type': 'string', 'description': 'Envelope id'}},
     'required': ['envelope_id']})
@declare_output_schema({'properties': {'envelope_id': {'description': 'Envelope id', 'type': 'string'},
                                       'recipients': {'description': 'Recipient statuses', 'type': 'array'},
                                       'status': {'description': 'Envelope status', 'type': 'string'}},
                        'type': 'object'})
async def get_envelope_status(credentials: Dict[str, str], envelope_id: str
) -> Dict[str, Any]:
    # Resolve the token/account once and reuse it for both calls below —
    # otherwise this single tool call costs two full auth round trips
    # (refresh-token exchange + userinfo) on top of the two API calls.:
    auth = await _esign_auth(credentials)
    envelope = await _esign_request(
        credentials, "GET", f"/envelopes/{envelope_id}", _auth=auth
    )
    recipients = await _esign_request(
        credentials, "GET", f"/envelopes/{envelope_id}/recipients", _auth=auth
    )
    return {
        "envelope_id": envelope.get("envelopeId"),
        "status": envelope.get("status"),
        "recipients": recipients.get("signers", []),
    }



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to False - POSTs to create a recipient view, minting a single-use signing URL; that is a capability grant, not a read.
@server.tool(
    name='get_signing_url',
    description="Get an embedded signing URL for a recipient of an envelope created with `embedded=true`. Pass `envelope_id`, the `signer_email`/`signer_name` used at creation, and `return_url` (where Docusign redirects after signing). The URL is single-use and expires quickly — fetch it right before handing it to the signer.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'envelope_id': {'type': 'string', 'description': 'Envelope id'},
                    'signer_email': {'type': 'string',
                                     'description': "Signer's email, as used at creation"},
                    'signer_name': {'type': 'string',
                                    'description': "Signer's name, as used at creation"},
                    'return_url': {'type': 'string',
                                   'description': 'Redirect target after signing '
                                                  'completes'},
                    'recipient_id': {'type': 'string',
                                     'description': "Recipient id, if not the default '1' "
                                                    "(create_envelope always uses '1')",
                                     'default': '1'}},
     'required': ['envelope_id', 'signer_email', 'signer_name', 'return_url']})
@declare_output_schema({'properties': {'signing_url': {'description': 'One-time embedded signing URL',
                                                       'type': 'string'}},
                        'type': 'object'})
async def get_signing_url(credentials: Dict[str, str],
    envelope_id: str,
    signer_email: str,
    signer_name: str,
    return_url: str,
    recipient_id: str = "1",
) -> Dict[str, Any]:
    payload = {
        "returnUrl": return_url,
        "authenticationMethod": "none",
        "email": signer_email,
        "userName": signer_name,
        "recipientId": recipient_id,
        "clientUserId": signer_email,
    }
    data = await _esign_request(
        credentials, "POST", f"/envelopes/{envelope_id}/views/recipient", json_data=payload
    )
    return {"signing_url": data.get("url")}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='get_user_details',
    description="Get the details of the connected (impersonated) Docusign user.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'user': {'type': 'object'}}, 'type': 'object'})
async def get_user_details(credentials: Dict[str, str]) -> Dict[str, Any]:
    auth = await _esign_auth(credentials)
    _, account = auth
    data = await _esign_request(
        credentials, "GET", f"/users/{account['user_id']}", _auth=auth
    )
    return {"user": data}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /envelopes
@server.tool(
    name='list_envelopes',
    description="List envelopes on the connected Docusign account. Optional `from_date` (YYYY-MM-DD) limits results to envelopes created on/after that date; defaults to the last 30 days. Optional `status` filters by envelope status (e.g. sent, delivered, completed, declined, voided). Optional `count` (page size, Docusign default 100) and `start_position` (offset) page through results when `has_more` is true in the response.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'from_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                    'status': {'type': 'string', 'description': 'Envelope status filter'},
                    'count': {'type': 'integer', 'description': 'Page size'},
                    'start_position': {'type': 'integer',
                                       'description': 'Offset into results'}}})
@declare_output_schema({'properties': {'envelopes': {'description': 'Matching envelopes (this page)', 'type': 'array'},
                                       'has_more': {'description': 'True if more results exist beyond this page',
                                                    'type': 'boolean'},
                                       'total': {'description': 'Count in this page', 'type': 'integer'},
                                       'total_available': {'description': 'Total matching envelopes across all pages',
                                                           'type': 'integer'}},
                        'type': 'object'})
async def list_envelopes(credentials: Dict[str, str],
    from_date: Optional[str] = None,
    status: Optional[str] = None,
    count: Optional[int] = None,
    start_position: Optional[int] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"from_date": from_date or _default_from_date()}
    if status:
        params["status"] = status
    if count is not None:
        params["count"] = count
    if start_position is not None:
        params["start_position"] = start_position

    data = await _esign_request(credentials, "GET", "/envelopes", params=params)
    return _paged_envelope_result(data)



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /groups
@server.tool(
    name='list_groups',
    description="List groups on the connected Docusign account.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'groups': {'description': 'Account groups', 'type': 'array'},
                                       'total': {'description': 'Total count returned', 'type': 'integer'}},
                        'type': 'object'})
async def list_groups(credentials: Dict[str, str]) -> Dict[str, Any]:
    data = await _esign_request(credentials, "GET", "/groups")
    groups = data.get("groups", [])
    return {"groups": groups, "total": len(groups)}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /billing_plan
@server.tool(
    name='list_products',
    description="Get the billing plan and feature set purchased for the connected account.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'billing_plan': {'type': 'object'}}, 'type': 'object'})
async def list_products(credentials: Dict[str, str]) -> Dict[str, Any]:
    data = await _esign_request(credentials, "GET", "/billing_plan")
    return {"billing_plan": data}



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /templates
@server.tool(
    name='list_templates',
    description="List templates available on the connected Docusign account. Optional `count` (page size) and `start_position` (offset) page through results when `has_more` is true in the response.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'count': {'type': 'integer', 'description': 'Page size'},
                    'start_position': {'type': 'integer',
                                       'description': 'Offset into results'}}})
@declare_output_schema({'properties': {'has_more': {'description': 'True if more results exist beyond this page',
                                                    'type': 'boolean'},
                                       'templates': {'description': 'Available templates (this page)',
                                                     'type': 'array'},
                                       'total': {'description': 'Count in this page', 'type': 'integer'},
                                       'total_available': {'description': 'Total templates across all pages',
                                                           'type': 'integer'}},
                        'type': 'object'})
async def list_templates(credentials: Dict[str, str],
    count: Optional[int] = None,
    start_position: Optional[int] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if count is not None:
        params["count"] = count
    if start_position is not None:
        params["start_position"] = start_position
    data = await _esign_request(credentials, "GET", "/templates", params=params or None)
    templates = data.get("envelopeTemplates", [])
    total_available = data.get("totalSetSize")
    try:
        total_available = (
            int(total_available) if total_available is not None else len(templates)
        )
    except (TypeError, ValueError):
        total_available = len(templates)
    return {
        "templates": templates,
        "total": len(templates),
        "total_available": total_available,
        "has_more": total_available > len(templates),
    }



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /envelopes
@server.tool(
    name='search_documents',
    description="Search envelopes by subject/recipient text match (Docusign's envelope search_text filter — this does NOT search inside document content/text, only envelope metadata). Pass `query` (search text) and optional `from_date` (YYYY-MM-DD, defaults to last 30 days). Optional `count` (page size) and `start_position` (offset) page through results when `has_more` is true in the response.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string', 'description': 'Search text'},
                    'from_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                    'count': {'type': 'integer', 'description': 'Page size'},
                    'start_position': {'type': 'integer',
                                       'description': 'Offset into results'}},
     'required': ['query']})
@declare_output_schema({'properties': {'envelopes': {'description': 'Matching envelopes (this page)', 'type': 'array'},
                                       'has_more': {'description': 'True if more results exist beyond this page',
                                                    'type': 'boolean'},
                                       'total': {'description': 'Count in this page', 'type': 'integer'},
                                       'total_available': {'description': 'Total matching envelopes across all pages',
                                                           'type': 'integer'}},
                        'type': 'object'})
async def search_documents(credentials: Dict[str, str],
    query: str,
    from_date: Optional[str] = None,
    count: Optional[int] = None,
    start_position: Optional[int] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "search_text": query,
        "from_date": from_date or _default_from_date(),
    }
    if count is not None:
        params["count"] = count
    if start_position is not None:
        params["start_position"] = start_position
    data = await _esign_request(credentials, "GET", "/envelopes", params=params)
    return _paged_envelope_result(data)



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: PUT /envelopes/{envelope_id}
@server.tool(
    name='send_envelope',
    description="Send a draft envelope (one created with create_envelope's `send_now=false`) by `envelope_id`. Moves it from `created` to `sent` status.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'envelope_id': {'type': 'string', 'description': 'Envelope id'}},
     'required': ['envelope_id']})
@declare_output_schema({'properties': {'envelope_id': {'description': 'Envelope id', 'type': 'string'},
                                       'status': {'description': 'Resulting envelope status', 'type': 'string'}},
                        'type': 'object'})
async def send_envelope(credentials: Dict[str, str], envelope_id: str) -> Dict[str, Any]:
    data = await _esign_request(
        credentials, "PUT", f"/envelopes/{envelope_id}", json_data={"status": "sent"}
    )
    return {
        "envelope_id": data.get("envelopeId", envelope_id),
        "status": data.get("status", "sent"),
    }



# credential_headers: ['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PUT /envelopes/{id} {"status": "voided"}. Terminal in
# Docusign's lifecycle and it kills outstanding signature requests; no
# tool in this connector can move an envelope back out of voided.
# Evidence: PUT /envelopes/{envelope_id}
@server.tool(
    name='void_envelope',
    description="Cancel a sent envelope by `envelope_id`, with a required `reason`.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Docusign-Refresh-Token', 'X-Docusign-Client-Id', 'X-Docusign-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'envelope_id': {'type': 'string', 'description': 'Envelope id'},
                    'reason': {'type': 'string',
                               'description': 'Why the envelope is being voided'}},
     'required': ['envelope_id', 'reason']})
@declare_output_schema({'properties': {'envelope_id': {'description': 'Envelope id', 'type': 'string'},
                                       'status': {'description': 'Resulting envelope status', 'type': 'string'}},
                        'type': 'object'})
async def void_envelope(credentials: Dict[str, str], envelope_id: str, reason: str
) -> Dict[str, Any]:
    data = await _esign_request(
        credentials,
        "PUT",
        f"/envelopes/{envelope_id}",
        json_data={"status": "voided", "voidedReason": reason},
    )
    return {
        "envelope_id": data.get("envelopeId", envelope_id),
        "status": data.get("status", "voided"),
    }
