"""The google-gmail connector.

Ported from the v1 mixin form by a codemod.

3 tools, 2 of them proposed read-only. Every input schema
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

SERVER_NAME = "google-gmail"
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

import base64
import re
from email.message import EmailMessage
from typing import Any, Dict, List, Optional
from ._shared import GOOGLE_CREDENTIAL_HEADERS, _google_request


_DATA_URI_RE = re.compile(r"^data:[\w.+-]*/?[\w.+-]*(;[\w.+-]+=[\w.+-]+)*;base64,", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"\s+")

def _normalize_base64(value: str) -> str:
    """Strip the wrappings producers commonly add around base64 payloads:
    MIME-style line wrapping (base64.encodebytes) and `data:<mime>;base64,`
    URIs. Plain b64decode discards characters outside the alphabet but keeps
    the letters inside such a prefix, so the payload decodes to garbage with
    no error at all - normalizing first lets the decode below stay strict
    (same normalization as the outlook_email connector's
    normalize_base64 - duplicated here rather than imported since each MCP
    server is a standalone Docker build with no cross-server import path)."""
    text = value.strip()
    match = _DATA_URI_RE.match(text)
    if match:
        text = text[match.end() :]
    return _WHITESPACE_RE.sub("", text)

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1"

GMAIL_SCOPE = (
    "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"
)



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /users/me/messages/{message_id}
@server.tool(
    name='google_gmail_get_message',
    description="Get Gmail message details by message ID.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'message_id': {'type': 'string'},
                    'format': {'type': 'string',
                               'enum': ['metadata', 'full', 'minimal'],
                               'default': 'full'}},
     'required': ['message_id']})
@declare_output_schema({'properties': {'id': {'type': 'string'},
                                       'payload': {'type': 'object'},
                                       'snippet': {'type': 'string'},
                                       'threadId': {'type': 'string'}},
                        'required': ['id', 'threadId'],
                        'type': 'object'})
async def google_gmail_get_message(credentials: Dict[str, Any],
    message_id: str,
    format: str = "full",
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"format": format}
    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="GET",
        base_url=GMAIL_API_BASE_URL,
        path=f"/users/me/messages/{message_id}",
        params=params,
        scopes=GMAIL_SCOPE,
    )
    return data



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='google_gmail_list_messages',
    description="List Gmail messages using query syntax.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': 'Gmail search query (e.g. from:alice '
                                             'newer_than:7d).'},
                    'max_results': {'type': 'integer',
                                    'minimum': 1,
                                    'maximum': 100,
                                    'default': 20,
                                    'description': 'Maximum number of messages to list.'},
                    'label_ids': {'type': 'array',
                                  'items': {'type': 'string'},
                                  'description': 'Optional Gmail label IDs to filter on.'}}})
@declare_output_schema({'properties': {'messages': {'items': {'properties': {'id': {'type': 'string'},
                                                                             'threadId': {'type': 'string'}},
                                                              'required': ['id', 'threadId'],
                                                              'type': 'object'},
                                                    'type': 'array'},
                                       'resultSizeEstimate': {'type': 'integer'}},
                        'required': ['messages'],
                        'type': 'object'})
async def google_gmail_list_messages(credentials: Dict[str, Any],
    query: Optional[str] = None,
    max_results: int = 20,
    label_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "maxResults": max_results,
    }
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = label_ids

    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="GET",
        base_url=GMAIL_API_BASE_URL,
        path="/users/me/messages",
        params=params,
        scopes=GMAIL_SCOPE,
    )
    return {
        "messages": data.get("messages", []),
        "resultSizeEstimate": data.get("resultSizeEstimate", 0),
    }



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST, path built at runtime
@server.tool(
    name='google_gmail_send_message',
    description="Send a plain-text Gmail message.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'to': {'type': 'string', 'description': 'Recipient email address.'},
                    'subject': {'type': 'string'},
                    'body': {'type': 'string'},
                    'attachments': {'type': 'array',
                                    'items': {'anyOf': [{'type': 'string',
                                                         'description': 'File ID resolved '
                                                                        'automatically by '
                                                                        'the platform'},
                                                        {'type': 'object',
                                                         'description': 'Inline attachment '
                                                                        'resolved by the '
                                                                        'platform from a '
                                                                        'file_id',
                                                         'properties': {'filename': {'type': 'string'},
                                                                        'content_base64': {'type': 'string'},
                                                                        'content_type': {'type': 'string'}},
                                                         'required': ['filename',
                                                                      'content_base64']}]},
                                    'description': 'Optional array of file IDs to attach. '
                                                   'The platform automatically resolves '
                                                   'file IDs to base64 attachments.'}},
     'required': ['to', 'subject', 'body']})
@declare_output_schema({'properties': {'id': {'type': 'string'},
                                       'labelIds': {'items': {'type': 'string'}, 'type': 'array'},
                                       'threadId': {'type': 'string'}},
                        'required': ['id', 'threadId'],
                        'type': 'object'})
async def google_gmail_send_message(credentials: Dict[str, Any],
    to: str,
    subject: str,
    body: str,
    attachments: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if attachments:
        for att in attachments:
            if not (isinstance(att, dict) and "content_base64" in att):
                file_id = att.get("file_id") if isinstance(att, dict) else att
                return {
                    "success": False,
                    "error": (
                        f"Attachment file_id '{file_id}' could not be resolved to "
                        "inline content by the platform. The file may not exist, "
                        "may be too large, or you may not have access to it."
                    ),
                }

            content_base64 = att["content_base64"]
            if not isinstance(content_base64, str) or not content_base64:
                return {
                    "success": False,
                    "error": (
                        f"Attachment '{att.get('filename', 'attachment')}' is missing "
                        "content_base64."
                    ),
                }
            try:
                # validate=True so a malformed payload fails loudly here rather
                # than sending a corrupt attachment the recipient cannot open.
                raw_bytes = base64.b64decode(_normalize_base64(content_base64), validate=True)
            except ValueError:
                return {
                    "success": False,
                    "error": (
                        f"Attachment '{att.get('filename', 'attachment')}' could not be "
                        "decoded: content_base64 is not valid base64."
                    ),
                }
            if not raw_bytes:
                return {
                    "success": False,
                    "error": f"Attachment '{att.get('filename', 'attachment')}' is empty.",
                }
            content_type = att.get("content_type", "application/octet-stream")
            maintype, subtype = (
                content_type.split("/", 1)
                if "/" in content_type
                else ("application", "octet-stream")
            )
            subtype = subtype.split(";")[0].strip()

            msg.add_attachment(
                raw_bytes,
                maintype=maintype,
                subtype=subtype,
                filename=att.get("filename", "attachment"),
            )

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="POST",
        base_url=GMAIL_API_BASE_URL,
        path="/users/me/messages/send",
        json_data={"raw": encoded},
        scopes=GMAIL_SCOPE,
    )
    return data
