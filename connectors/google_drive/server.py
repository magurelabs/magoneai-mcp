"""The google-drive connector.

Ported from the v1 mixin form by a codemod.

3 tools, 3 of them proposed read-only. Every input schema
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

SERVER_NAME = "google-drive"
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

from typing import Any, Dict, List, Optional
from ._shared import GOOGLE_CREDENTIAL_HEADERS, _google_request, _google_request_text


DRIVE_API_BASE_URL = "https://www.googleapis.com/drive/v3"

DRIVE_DOWNLOAD_BASE_URL = "https://www.googleapis.com/drive/v3"

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

GOOGLE_NATIVE_EXPORT_MAP: Dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",  # Google Docs → plain text
    "application/vnd.google-apps.spreadsheet": "text/csv",  # Google Sheets → CSV
    "application/vnd.google-apps.presentation": "text/plain",  # Google Slides → plain text
    "application/vnd.google-apps.drawing": "image/svg+xml",  # Google Drawings → SVG
    "application/vnd.google-apps.script": "application/json",  # Apps Script → JSON
}



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /files/{file_id}/export
@server.tool(
    name='google_drive_get_file_content',
    description="Get metadata and text content for a Google Drive file. Automatically exports Google Docs as plain text, Sheets as CSV, and Slides as plain text. Optionally override the export format via export_mime_type.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'file_id': {'type': 'string',
                                'description': 'ID of the file in Google Drive.'},
                    'export_mime_type': {'type': 'string',
                                         'description': 'Override the export MIME type for '
                                                        'Google Docs/Sheets/Slides (e.g. '
                                                        "'text/plain', 'text/csv'). If "
                                                        'omitted, the best format is '
                                                        'chosen automatically based on the '
                                                        'file type. Not needed for regular '
                                                        'files.'}},
     'required': ['file_id']})
@declare_output_schema({'properties': {'content': {'description': 'Exported textual content. Populated automatically '
                                                                  'for Google Docs/Sheets/Slides.',
                                                   'type': 'string'},
                                       'content_note': {'description': 'Human-readable note about why content may be '
                                                                       'empty (e.g. binary file).',
                                                        'type': 'string'},
                                       'id': {'type': 'string'},
                                       'mimeType': {'type': 'string'},
                                       'modifiedTime': {'type': 'string'},
                                       'name': {'type': 'string'},
                                       'size': {'type': 'string'},
                                       'webViewLink': {'type': 'string'}},
                        'required': ['id', 'name', 'mimeType'],
                        'type': 'object'})
async def google_drive_get_file_content(credentials: Dict[str, Any],
    file_id: str,
    export_mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Get metadata and textual content for a Drive file.

    For Google native types (Docs/Sheets/Slides), content is exported
    automatically even if export_mime_type is not provided. The MIME type
    is resolved from the file's own mimeType field via GOOGLE_NATIVE_EXPORT_MAP.
    """

    # First get metadata — we need the mimeType to decide how to export
    meta = await google_drive_get_file_metadata(credentials, file_id)

    file_mime = meta.get("mimeType", "")
    content = ""
    content_note = ""

    # Resolve export MIME type: caller override → auto-detect from file type → nothing
    resolved_export_mime = export_mime_type or GOOGLE_NATIVE_EXPORT_MAP.get(file_mime)

    if resolved_export_mime:
        params: Dict[str, Any] = {
            "mimeType": resolved_export_mime,
        }
        content = await _google_request_text(
            credentials=credentials,
            method="GET",
            base_url=DRIVE_DOWNLOAD_BASE_URL,
            path=f"/files/{file_id}/export",
            params=params,
            scopes=DRIVE_SCOPE,
        )
    elif file_mime.startswith("application/vnd.google-apps."):
        # A Google native type we don't have a mapping for (e.g. Forms, Maps)
        content_note = (
            f"This Google file type ({file_mime}) cannot be exported as text. "
            "Open the webViewLink directly to view its contents."
        )
    elif file_mime.startswith("text/") or file_mime in (
        "application/json",
        "application/xml",
        "application/csv",
    ):
        # Standard text file uploaded to Drive (not a Google format).
        # We can download its raw content using the standard download endpoint (alt=media)
        # rather than the export endpoint.
        params: Dict[str, Any] = {
            "alt": "media",
        }
        try:
            content = await _google_request_text(
                credentials=credentials,
                method="GET",
                base_url=DRIVE_DOWNLOAD_BASE_URL,
                path=f"/files/{file_id}",
                params=params,
                scopes=DRIVE_SCOPE,
            )
        except Exception as e:
            content_note = f"Failed to download raw text file: {e}"
    else:
        # Regular binary file (PDF, image, etc.) — Drive export endpoint is not applicable
        content_note = (
            f"Binary file ({file_mime}). Text extraction via export is not supported. "
            "Download the file directly using its webViewLink."
        )

    result: Dict[str, Any] = dict(meta)
    result["content"] = content
    if content_note:
        result["content_note"] = content_note
    return result



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /files/{file_id}
@server.tool(
    name='google_drive_get_file_metadata',
    description="Get basic metadata for a Google Drive file by ID.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'file_id': {'type': 'string',
                                'description': 'ID of the file in Google Drive.'}},
     'required': ['file_id']})
@declare_output_schema({'properties': {'id': {'type': 'string'},
                                       'mimeType': {'type': 'string'},
                                       'modifiedTime': {'type': 'string'},
                                       'name': {'type': 'string'},
                                       'size': {'type': 'string'},
                                       'webViewLink': {'type': 'string'}},
                        'required': ['id', 'name', 'mimeType'],
                        'type': 'object'})
async def google_drive_get_file_metadata(credentials: Dict[str, Any],
    file_id: str,
) -> Dict[str, Any]:
    """Get metadata for a single Drive file."""

    params = {
        "fields": "id,name,mimeType,webViewLink,modifiedTime,size",
    }
    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="GET",
        base_url=DRIVE_API_BASE_URL,
        path=f"/files/{file_id}",
        params=params,
        scopes=DRIVE_SCOPE,
    )
    return data



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='google_drive_search_files',
    description="Search Google Drive files by query with optional MIME type filters.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': 'Search query in Drive query syntax, e.g. '
                                             "name contains 'report'"},
                    'mime_types': {'type': 'array',
                                   'items': {'type': 'string'},
                                   'description': 'Optional list of MIME types to filter '
                                                  'by.'},
                    'page_size': {'type': 'integer',
                                  'minimum': 1,
                                  'maximum': 100,
                                  'default': 20,
                                  'description': 'Maximum number of files to return.'}},
     'required': ['query']})
@declare_output_schema({'properties': {'files': {'items': {'properties': {'id': {'type': 'string'},
                                                                          'mimeType': {'type': 'string'},
                                                                          'modifiedTime': {'type': 'string'},
                                                                          'name': {'type': 'string'},
                                                                          'webViewLink': {'type': 'string'}},
                                                           'required': ['id', 'name', 'mimeType'],
                                                           'type': 'object'},
                                                 'type': 'array'}},
                        'required': ['files'],
                        'type': 'object'})
async def google_drive_search_files(credentials: Dict[str, Any],
    query: str,
    mime_types: Optional[List[str]] = None,
    page_size: int = 20,
) -> Dict[str, Any]:
    """Search files in Google Drive."""

    q_parts: List[str] = []
    if query:
        q_parts.append(query)
    if mime_types:
        mime_filters = " or ".join([f"mimeType='{mt}'" for mt in mime_types])
        q_parts.append(f"({mime_filters})")
    q = " and ".join(q_parts) if q_parts else ""

    params: Dict[str, Any] = {
        "q": q,
        "pageSize": page_size,
        "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
    }

    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="GET",
        base_url=DRIVE_API_BASE_URL,
        path="/files",
        params=params,
        scopes=DRIVE_SCOPE,
    )
    return {"files": data.get("files", [])}
