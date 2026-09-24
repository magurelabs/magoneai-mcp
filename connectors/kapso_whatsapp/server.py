"""The kapso-whatsapp connector.

Ported from the v1 mixin form by a codemod.

7 tools, 4 of them proposed read-only. Every input schema
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

from magone_mcp_core import ConnectorServer, declare_schema

SERVER_NAME = "kapso-whatsapp"
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

import os
from typing import Any, Dict, Optional
import httpx
from .tools._client import CREDENTIAL_HEADERS, GRAPH_VERSION
from typing import Any, Dict, List, Optional
from .tools._client import CREDENTIAL_HEADERS


DEFAULT_BASE_URL = "https://api.kapso.ai/meta/whatsapp"

GRAPH_VERSION = os.getenv("KAPSO_GRAPH_VERSION", "v24.0")

REQUEST_TIMEOUT_SECONDS = 30.0

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
        # `error` must be a string for the result to be reported as a failure
        # (`isError`); a dict there made every vendor 4xx and 5xx read as a
        # success. The vendor's body travels alongside as `detail`.
        return {
            "success": False,
            "status": resp.status_code,
            "error": _error_text(resp.status_code, data),
            "detail": data,
        }
    return {"success": True, "status": resp.status_code, "data": data}


def _error_text(status: int, data: Any) -> str:
    """The vendor's own message where it has one, else the status."""
    message = ""
    if isinstance(data, dict):
        inner = data.get("error")
        if isinstance(inner, dict):
            message = str(inner.get("message") or inner.get("error_user_msg") or "")
        elif isinstance(inner, str):
            message = inner
        message = message or str(data.get("message") or "")
    elif isinstance(data, str):
        message = data[:300]
    return f"Kapso API error {status}" + (f": {message}" if message else "")

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

_MISSING_PHONE = {
    "success": False,
    "status": 400,
    "error": "Missing phone_number_id (pass it or set a default on the connection).",
}

_DEFAULT_LIMIT = 20

_MAX_LIMIT = 100

def _clamp_limit(limit: int) -> int:
    """Keep the page size within Kapso's 1-100 bounds."""
    return max(1, min(int(limit), _MAX_LIMIT))

def _scoped_path(phone_number_id: str, resource: str) -> str:
    """Build a phone-number-scoped Graph path, e.g. /v24.0/123/conversations."""
    return f"/{GRAPH_VERSION}/{phone_number_id}/{resource}"

_MEDIA_TYPES = ["image", "document", "video", "audio"]



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_contacts',
    description="List WhatsApp contacts known to a phone number.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'customer_id': {'type': 'string',
                                    'description': 'Optional Kapso customer id to filter '
                                                   'contacts'},
                    'limit': {'type': 'integer', 'default': 20},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Phone number id (defaults to the '
                                                       "connection's)."}},
     'required': []})
async def list_contacts(credentials: Dict[str, str],
    customer_id: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    params = {"customer_id": customer_id, "limit": _clamp_limit(limit)}
    return await request(credentials, "GET", _scoped_path(pnid, "contacts"), params=params)



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_conversations',
    description="List recent WhatsApp conversations for a phone number, newest first.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'status': {'type': 'string',
                               'enum': ['active', 'ended'],
                               'description': 'Filter by conversation status (omit for '
                                              'all)'},
                    'limit': {'type': 'integer', 'default': 20},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Phone number id (defaults to the '
                                                       "connection's)."}},
     'required': []})
async def list_conversations(credentials: Dict[str, str],
    status: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    params = {"status": status, "limit": _clamp_limit(limit)}
    return await request(credentials, "GET", _scoped_path(pnid, "conversations"), params=params)



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_messages',
    description="List messages, newest first. Pass conversation_id to scope to one chat.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'conversation_id': {'type': 'string',
                                        'description': 'Conversation id to filter by (from '
                                                       'list_conversations)'},
                    'direction': {'type': 'string',
                                  'enum': ['inbound', 'outbound'],
                                  'description': 'Filter by message direction'},
                    'limit': {'type': 'integer', 'default': 20},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Phone number id (defaults to the '
                                                       "connection's)."}},
     'required': []})
async def list_messages(credentials: Dict[str, str],
    conversation_id: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    params = {
        "conversation_id": conversation_id,
        "direction": direction,
        "limit": _clamp_limit(limit),
    }
    return await request(credentials, "GET", _scoped_path(pnid, "messages"), params=params)



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_templates',
    description="List approved WhatsApp message templates available to a phone number.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'limit': {'type': 'integer', 'default': 20},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Phone number id (defaults to the '
                                                       "connection's)."}},
     'required': []})
async def list_templates(credentials: Dict[str, str],
    limit: int = _DEFAULT_LIMIT,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    params = {"limit": _clamp_limit(limit)}
    return await request(
        credentials, "GET", _scoped_path(pnid, "message_templates"), params=params
    )



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: send_message() -> request(credentials, "POST", path). Sending a
# message is additive, so destructiveHint stays False.
@server.tool(
    name='send_media_message',
    description="Send a media WhatsApp message (image, document, video, or audio) by public URL. Inside the 24-hour window only.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'to': {'type': 'string',
                           'description': 'Recipient WhatsApp number in E.164 digits, e.g. '
                                          '15551234567'},
                    'media_type': {'type': 'string',
                                   'enum': ['image', 'document', 'video', 'audio'],
                                   'description': 'Kind of media to send'},
                    'link': {'type': 'string',
                             'description': 'Public HTTPS URL of the media file'},
                    'caption': {'type': 'string',
                                'description': 'Optional caption (image, video, document '
                                               'only)'},
                    'filename': {'type': 'string',
                                 'description': 'Optional display filename (document '
                                                'only)'},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Sender WhatsApp phone number id '
                                                       "(defaults to the connection's)."}},
     'required': ['to', 'media_type', 'link']})
async def send_media_message(credentials: Dict[str, str],
    to: str,
    media_type: str,
    link: str,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    if media_type not in _MEDIA_TYPES:
        return {
            "success": False,
            "status": 400,
            "error": f"media_type must be one of {_MEDIA_TYPES}",
        }
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    media: Dict[str, Any] = {"link": link}
    if caption and media_type in ("image", "video", "document"):
        media["caption"] = caption
    if filename and media_type == "document":
        media["filename"] = filename
    payload = {"to": to, "type": media_type, media_type: media}
    return await send_message(credentials, pnid, payload)



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: send_message() -> request(credentials, "POST", path). Sending a
# message is additive, so destructiveHint stays False.
@server.tool(
    name='send_template_message',
    description="Send an approved WhatsApp template message. Required to start a conversation or to message a user outside the 24-hour window.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'to': {'type': 'string',
                           'description': 'Recipient WhatsApp number in E.164 digits, e.g. '
                                          '15551234567'},
                    'template_name': {'type': 'string',
                                      'description': 'Meta-approved template name, e.g. '
                                                     'order_confirmation'},
                    'language_code': {'type': 'string',
                                      'default': 'en_US',
                                      'description': 'Template language/locale code, e.g. '
                                                     'en_US, es, pt_BR'},
                    'body_params': {'type': 'array',
                                    'items': {'type': 'string'},
                                    'default': [],
                                    'description': 'Ordered values for the template body '
                                                   '{{1}}, {{2}}, ...'},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Sender WhatsApp phone number id '
                                                       "(defaults to the connection's)."}},
     'required': ['to', 'template_name']})
async def send_template_message(credentials: Dict[str, str],
    to: str,
    template_name: str,
    language_code: str = "en_US",
    body_params: Optional[List[str]] = None,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    template: Dict[str, Any] = {
        "name": template_name,
        "language": {"code": language_code},
    }
    params = [{"type": "text", "text": str(p)} for p in (body_params or [])]
    if params:
        template["components"] = [{"type": "body", "parameters": params}]
    payload = {"to": to, "type": "template", "template": template}
    return await send_message(credentials, pnid, payload)



# credential_headers: ['X-API-Key', 'X-Phone-Number-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: send_message() -> request(credentials, "POST", path). Sending a
# message is additive, so destructiveHint stays False.
@server.tool(
    name='send_text_message',
    description="Send a free-text WhatsApp message. Only allowed inside the 24-hour customer-service window; outside it use send_template_message.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-API-Key', 'X-Phone-Number-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'to': {'type': 'string',
                           'description': 'Recipient WhatsApp number in E.164 digits, e.g. '
                                          '15551234567'},
                    'text': {'type': 'string', 'description': 'Message body to send'},
                    'preview_url': {'type': 'boolean',
                                    'default': False,
                                    'description': 'Render a link preview for URLs in the '
                                                   'body'},
                    'phone_number_id': {'type': 'string',
                                        'description': 'Sender WhatsApp phone number id '
                                                       "(defaults to the connection's)."}},
     'required': ['to', 'text']})
async def send_text_message(credentials: Dict[str, str],
    to: str,
    text: str,
    preview_url: bool = False,
    phone_number_id: Optional[str] = None,
) -> Dict[str, Any]:
    pnid = resolve_phone_number_id(credentials, phone_number_id)
    if not pnid:
        return _MISSING_PHONE
    payload = {
        "to": to,
        "type": "text",
        "text": {"body": text, "preview_url": preview_url},
    }
    return await send_message(credentials, pnid, payload)
