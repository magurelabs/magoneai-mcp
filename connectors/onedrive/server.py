"""The onedrive connector.

Ported from v1 `mcp-servers/onedrive`. Uploads to, and lists, reads, moves,
copies, shares, searches and deletes items in, the signed-in user's own
OneDrive through Microsoft Graph, with the caller's own Microsoft credentials.

11 tools, 4 of them proposed read-only. Input schemas are v1's, carried
across via `declare_schema`, except `onedrive_upload_file`, which no longer
takes `file_id` (see below) and so now requires `filename`, and
`onedrive_upload_files`, which uploads several inline files in one call. v1's
`onedrive_list_shared_with_me` is dropped: Microsoft has deprecated both
`/me/drive/sharedWithMe` and `/me/drive/recent` (each stops returning data
after November 2026) with no supported successor, so there is no route to list
shared items; `onedrive_list_files` and `onedrive_search_files` find files.

## Credentials

Per call, as request headers, the same four every delegated Microsoft
connector here reads: `X-Microsoft-Refresh-Token`, `X-Microsoft-Client-Id`,
`X-Microsoft-Client-Secret` and `X-Microsoft-Tenant-Id` (default `common`).
The framework folds them into the handler's `credentials` dict and each call
exchanges the refresh token for a Graph access token. None of them is logged,
and the access token is never sent to an upload or copy-monitor URL, which
are pre-authorized.

The Azure app needs the delegated Graph permissions `Files.ReadWrite`,
`User.Read` and `offline_access` (`_shared.ONEDRIVE_OAUTH_SCOPES`).

## Uploads

Up to 4 MB is one `PUT .../content`. Above that, a Graph upload session in
10 MiB fragments (a multiple of the 320 KiB Graph requires) with
`Content-Range`; a 5xx or 429 waits (`Retry-After` when sent), asks the
session what it still expects and resumes. `ONEDRIVE_MAX_UPLOAD_MB` caps the
total (default 250), checked before any request.

Content comes in the call: `content` (text) or `content_base64`. v1's
`file_id` read a MagOneAI platform file through a platform-internal route, which
a connector that must work on a laptop with the caller's own storage cannot
do, so it is gone. The connector declares a 75 MiB request body cap (the
framework default is 10 MiB); base64 is a third larger than the file, so
`MCP_MAX_BODY_BYTES` has to be raised for an inline upload above about 55 MB.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema, error_fields
from magone_mcp_core.redact import scrub_text, upstream_error

from ._shared import get_access_token, graph_request
from .tools import copy_item, drive_root, upload_graph, upload_many
from .tools._constants import (
    FOLDER_CONFLICT_BEHAVIORS,
    MAX_LINK_EXPIRY_DAYS,
    MAX_RESULTS_CAP,
    MIN_LINK_EXPIRY_DAYS,
    VALID_LINK_SCOPES,
    VALID_LINK_TYPES,
    clamp,
    encode_id,
    max_upload_bytes,
    validate_id,
)
from .tools._format import (
    ITEM_SCHEMA,
    format_item,
    format_search_hit,
    format_uploaded,
)
from .tools.upload_input import (
    UploadInputError,
    check_conflict,
    check_name,
    item_path,
    resolve_payload,
)

SERVER_NAME = "onedrive"
SERVER_VERSION = "2.0.0"

logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

# Uploads arrive inline as base64, a third larger than the file, and
# onedrive_upload_files takes up to 20 per call. The framework's 10 MiB default
# refused any call carrying more than about 7.5 MB of files before the tool
# ran. 75 MiB, the cap outlook_email declares for the same reason, admits about
# 55 MB of files per call while bounding what one request may hold in memory.
# MCP_MAX_BODY_BYTES still overrides it, e.g. to upload a larger file inline.
MAX_REQUEST_BODY_BYTES = 75 * 1024 * 1024

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="Microsoft OneDrive: upload, files, folders, sharing, search",
    max_body_bytes=MAX_REQUEST_BODY_BYTES,
)

_ERROR = {"type": "string", "description": "Present when success is false"}
_NULLABLE_STR = {"type": ["string", "null"]}


def _failed(tool: str, exc: Exception) -> dict[str, Any]:
    """A refusal for the model, and a log line that carries no credential."""
    logger.warning(f"{tool}_error", error_type=type(exc).__name__, error=upstream_error(exc))
    return {"success": False, "error": scrub_text(str(exc)), **error_fields(exc)}


def _skip_token(next_link: Any) -> str | None:
    """The opaque `$skipToken` from Graph's `@odata.nextLink`, or None at the end.

    Graph paginates by handing back a full nextLink URL; this pulls its skip
    token out so the caller gets a small cursor to pass back rather than a URL.
    """
    if not isinstance(next_link, str) or not next_link:
        return None
    query = parse_qs(urlparse(next_link).query)
    for key in ("$skiptoken", "$skipToken", "skiptoken", "skipToken"):
        values = query.get(key)
        if values and values[0].strip():
            return values[0]
    return None


# REVIEWED: readOnlyHint=True. Evidence: GET /me/drive/{root|items/id}/children
@server.tool(
    name="onedrive_list_files",
    description=(
        "List files and folders in OneDrive. Pass `folder_id` to list a specific folder "
        "(omit for root) and `max_results` (default: 50, max: 200). When the result has "
        "`has_more` true, pass its `next_cursor` back as `cursor` for the next page. "
        "Returns items with id, name, type (file/folder), size and modified_date. "
        "Use onedrive_download_file for a file's download URL."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="read_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "folder_id": {
                "type": "string",
                "description": "Item ID of the folder to list (omit for root)",
            },
            "max_results": {
                "type": "integer",
                "description": "Max items to return (default: 50, max: 200)",
                "default": 50,
            },
            "cursor": {
                "type": "string",
                "description": "Page cursor: a previous call's next_cursor (omit for page one)",
            },
        },
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "count": {"type": "integer"},
            "has_more": {"type": "boolean"},
            "next_cursor": {
                "type": ["string", "null"],
                "description": "Pass back as cursor for the next page; null when has_more is false",
            },
            "items": {"type": "array", "items": ITEM_SCHEMA},
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_list_files(
    credentials: dict,
    folder_id: str | None = None,
    max_results: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    max_results = clamp(max_results, 1, MAX_RESULTS_CAP)
    if folder_id:
        err = validate_id(folder_id, "folder_id")
        if err:
            return {"success": False, "error": err}
        endpoint = f"/me/drive/items/{encode_id(folder_id)}/children"
    else:
        endpoint = "/me/drive/root/children"

    params: dict[str, Any] = {
        "$top": max_results,
        "$select": "id,name,file,folder,size,lastModifiedDateTime",
        "$orderby": "name asc",
    }
    # list children does not support $skip; $skipToken is its documented cursor,
    # handed back from the previous page's @odata.nextLink.
    if cursor and cursor.strip():
        params["$skipToken"] = cursor.strip()
    try:
        result = await graph_request(credentials, "GET", endpoint, params=params)
    except Exception as exc:
        return _failed("onedrive_list_files", exc)

    items = result.get("value", [])
    next_cursor = _skip_token(result.get("@odata.nextLink"))
    return {
        "success": True,
        "count": len(items),
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
        "items": [format_item(i) for i in items],
    }


# REVIEWED: readOnlyHint=True. Evidence: GET /me/drive/items/{item_id}
@server.tool(
    name="onedrive_get_file",
    description=(
        "Get metadata for a file or folder by its item ID. Returns name, size, "
        "mime_type, modified_date, created_date, and web_url for opening in browser. "
        "Use onedrive_download_file for a file's download URL."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="read_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "OneDrive item ID"},
        },
        "required": ["item_id"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "type": {"type": "string", "enum": ["file", "folder"]},
            "size": {"type": ["integer", "null"]},
            "mime_type": _NULLABLE_STR,
            "modified_date": _NULLABLE_STR,
            "created_date": _NULLABLE_STR,
            "web_url": _NULLABLE_STR,
            "parent_id": _NULLABLE_STR,
            "parent_path": _NULLABLE_STR,
            "child_count": {"type": ["integer", "null"]},
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_get_file(credentials: dict, item_id: str) -> dict[str, Any]:
    err = validate_id(item_id, "item_id")
    if err:
        return {"success": False, "error": err}
    select = "id,name,file,folder,size,lastModifiedDateTime,createdDateTime,webUrl,parentReference"
    try:
        item = await graph_request(
            credentials, "GET", f"/me/drive/items/{encode_id(item_id)}", params={"$select": select}
        )
    except Exception as exc:
        return _failed("onedrive_get_file", exc)
    return {"success": True, **format_item(item, full=True)}


# REVIEWED: readOnlyHint=False, destructiveHint=True: conflict_behavior=replace
# overwrites an existing file. Evidence: PUT .../content, POST createUploadSession
@server.tool(
    name="onedrive_upload_file",
    description=(
        "Upload a file to the user's OneDrive. Give the destination as `folder_path` "
        "(e.g. 'Reports/2026', omit for the root) or `folder_id`, plus `filename`. "
        "Give the content as exactly one of: `content` (text) or `content_base64` "
        "(binary). Files over 4 MB are sent in resumable chunks automatically. "
        "`conflict_behavior`: rename (default), replace, or fail. Returns id, name, "
        "size, web_url and parent_path."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Name to save the file as"},
            "folder_path": {"type": "string", "description": "Destination folder path"},
            "folder_id": {"type": "string", "description": "Destination folder item ID"},
            "content": {"type": "string", "description": "Plain-text file content"},
            "content_base64": {"type": "string", "description": "Base64-encoded file content"},
            "content_type": {"type": "string", "description": "MIME type (guessed if omitted)"},
            "conflict_behavior": {
                "type": "string",
                "enum": ["rename", "replace", "fail"],
                "default": "rename",
                "description": "What to do when the name is taken",
            },
        },
        "required": ["filename"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "size": {"type": ["integer", "null"]},
            "web_url": _NULLABLE_STR,
            "parent_id": _NULLABLE_STR,
            "parent_path": _NULLABLE_STR,
            "upload_method": {"type": "string", "enum": ["simple", "session"]},
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_upload_file(
    credentials: dict,
    filename: str | None = None,
    folder_path: str | None = None,
    folder_id: str | None = None,
    content: str | None = None,
    content_base64: str | None = None,
    content_type: str | None = None,
    conflict_behavior: str = "rename",
) -> dict[str, Any]:
    try:
        conflict = check_conflict(conflict_behavior)
        payload = resolve_payload(
            filename, content, content_base64, content_type, max_upload_bytes()
        )
        path = item_path(folder_path, folder_id, payload.filename)
    except UploadInputError as exc:
        return {"success": False, "error": str(exc)}

    try:
        token = await get_access_token(credentials)
        async with httpx.AsyncClient(timeout=upload_graph.UPLOAD_TIMEOUT) as client:
            item, method = await upload_graph.upload_bytes(client, token, path, payload, conflict)
    except Exception as exc:
        return _failed("onedrive_upload_file", exc)

    logger.info("onedrive_upload_complete", method=method, size_bytes=len(payload.content))
    return {"success": True, "upload_method": method, **format_uploaded(item)}


# REVIEWED: readOnlyHint=False, destructiveHint=True, as onedrive_upload_file:
# conflict_behavior=replace overwrites. Evidence: one upload_file per attachment
@server.tool(
    name="onedrive_upload_files",
    description=(
        "Upload several files to the user's OneDrive in one call (at most 20). Put each "
        "file in `attachments` as {filename, content_base64, content_type}. A host that "
        "supports it, such as MagOneAI, can resolve a run-file reference in `attachments` "
        "into that inline shape before the call, so a large file such as a recording is "
        "not cut by a per-string limit; called directly, pass the inline objects. "
        "Destination: `folder_path` (e.g. 'Reports/2026', created if missing) or "
        "`folder_id`. Files over 4 MB are sent in resumable chunks. One bad file fails "
        "alone. Returns uploaded and failed lists."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "attachments": {
                "type": "array",
                "description": "Files to upload, each {filename, content_base64, content_type}",
                "maxItems": 20,
                "items": {
                    "type": ["object", "string"],
                    "description": "An inline file; a reference only if a platform resolves it",
                    "properties": {
                        "filename": {"type": "string", "description": "Name to save as"},
                        "content_base64": {
                            "type": "string",
                            "description": "Base64-encoded file content",
                        },
                        "content_type": {
                            "type": "string",
                            "description": "MIME type (guessed if omitted)",
                        },
                    },
                },
            },
            "folder_path": {"type": "string", "description": "Destination folder path"},
            "folder_id": {"type": "string", "description": "Destination folder item ID"},
            "conflict_behavior": {
                "type": "string",
                "enum": ["rename", "replace", "fail"],
                "default": "rename",
                "description": "What to do when a name is taken",
            },
        },
        "required": ["attachments"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "True when no file failed"},
            "uploaded": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "size": {"type": ["integer", "null"]},
                        "web_url": _NULLABLE_STR,
                        "parent_id": _NULLABLE_STR,
                        "parent_path": _NULLABLE_STR,
                        "upload_method": {"type": "string", "enum": ["simple", "session"]},
                    },
                },
            },
            "failed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "error": {"type": "string"},
                    },
                },
            },
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_upload_files(
    credentials: dict,
    attachments: list,
    folder_path: str | None = None,
    folder_id: str | None = None,
    conflict_behavior: str = "rename",
) -> dict[str, Any]:
    try:
        conflict = check_conflict(conflict_behavior)
        items = upload_many.inline_items(attachments)
    except UploadInputError as exc:
        return {"success": False, "error": str(exc)}

    try:
        token = await get_access_token(credentials)
        result = await upload_many.upload_many(token, items, (folder_path, folder_id), conflict)
    except Exception as exc:
        return _failed("onedrive_upload_files", exc)

    logger.info(
        "onedrive_upload_files_complete",
        uploaded=len(result["uploaded"]),
        failed=len(result["failed"]),
    )
    return result


# REVIEWED: readOnlyHint=True. Evidence: GET /me/drive/items/{item_id}
@server.tool(
    name="onedrive_download_file",
    description=(
        "Get a temporary pre-authorized download URL for a OneDrive file. "
        "The URL is valid for a short time (typically 1 hour). "
        "Pass the item's `item_id` (from onedrive_list_files or onedrive_get_file)."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="read_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "OneDrive item ID"},
        },
        "required": ["item_id"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "size": {"type": ["integer", "null"]},
            "download_url": {
                "type": "string",
                "description": "Pre-authorized URL, valid for roughly an hour",
            },
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_download_file(credentials: dict, item_id: str) -> dict[str, Any]:
    err = validate_id(item_id, "item_id")
    if err:
        return {"success": False, "error": err}
    try:
        item = await graph_request(
            credentials,
            "GET",
            f"/me/drive/items/{encode_id(item_id)}",
            params={"$select": "id,name,file,size,@microsoft.graph.downloadUrl"},
        )
    except Exception as exc:
        return _failed("onedrive_download_file", exc)

    if "folder" in item:
        return {"success": False, "error": "Item is a folder, not a file"}
    download_url = item.get("@microsoft.graph.downloadUrl")
    if not download_url:
        return {"success": False, "error": "No download URL available for this item"}
    return {
        "success": True,
        "id": item.get("id"),
        "name": item.get("name"),
        "size": item.get("size"),
        "download_url": download_url,
    }


# REVIEWED: destructiveHint=True. Evidence: DELETE /me/drive/items/{item_id}
@server.tool(
    name="onedrive_delete_item",
    description=(
        "Permanently delete a file or folder from OneDrive. "
        "Deleting a folder removes all its contents. This action cannot be undone. "
        "Pass the `item_id` (from onedrive_list_files or onedrive_get_file)."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "OneDrive item ID to delete"},
        },
        "required": ["item_id"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "deleted_id": {"type": "string"},
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_delete_item(credentials: dict, item_id: str) -> dict[str, Any]:
    err = validate_id(item_id, "item_id")
    if err:
        return {"success": False, "error": err}
    try:
        await graph_request(credentials, "DELETE", f"/me/drive/items/{encode_id(item_id)}")
    except Exception as exc:
        return _failed("onedrive_delete_item", exc)
    return {"success": True, "deleted_id": item_id}


# REVIEWED: destructiveHint=False: only adds, and a name clash renames or fails.
# Evidence: POST /me/drive/{root|items/id}/children
@server.tool(
    name="onedrive_create_folder",
    description=(
        "Create a new folder in OneDrive. Pass `name` for the folder name and optionally "
        "`parent_folder_id` to create inside a specific folder (omit for root). "
        "Set `conflict_behavior` to 'rename' (default) to auto-rename if a folder with "
        "the same name exists, or 'fail' to return an error."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Folder name"},
            "parent_folder_id": {
                "type": "string",
                "description": "Parent folder item ID (omit for root)",
            },
            "conflict_behavior": {
                "type": "string",
                "description": "What to do if folder name exists: 'rename' or 'fail'",
                "enum": ["rename", "fail"],
                "default": "rename",
            },
        },
        "required": ["name"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "web_url": _NULLABLE_STR,
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_create_folder(
    credentials: dict,
    name: str,
    parent_folder_id: str | None = None,
    conflict_behavior: str = "rename",
) -> dict[str, Any]:
    try:
        name = check_name(name, "name")
        conflict = check_conflict(conflict_behavior, FOLDER_CONFLICT_BEHAVIORS)
    except UploadInputError as exc:
        return {"success": False, "error": str(exc)}
    if parent_folder_id:
        err = validate_id(parent_folder_id, "parent_folder_id")
        if err:
            return {"success": False, "error": err}
        endpoint = f"/me/drive/items/{encode_id(parent_folder_id)}/children"
    else:
        endpoint = "/me/drive/root/children"

    body = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": conflict,
    }
    try:
        item = await graph_request(credentials, "POST", endpoint, json_data=body)
    except Exception as exc:
        return _failed("onedrive_create_folder", exc)
    return {
        "success": True,
        "id": item.get("id"),
        "name": item.get("name"),
        "web_url": item.get("webUrl"),
    }


# REVIEWED: destructiveHint=False: makes a new item, never overwrites one.
# Evidence: POST /me/drive/items/{item_id}/copy, then GET the monitor URL
@server.tool(
    name="onedrive_copy_item",
    description=(
        "Copy a file or folder to a new location in OneDrive. "
        "Pass `item_id` (source), `destination_folder_id` (target folder: omit, or pass "
        "'root', to copy to the drive root), "
        "and optionally `new_name` to rename the copy. "
        "Returns the new item's ID and name once the copy completes."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "Source item ID to copy"},
            "destination_folder_id": {
                "type": "string",
                "description": "Target folder item ID (omit, or 'root', for the drive root)",
            },
            "new_name": {
                "type": "string",
                "description": "New name for the copied item (optional)",
            },
        },
        "required": ["item_id"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "status": {
                "type": "string",
                "enum": ["completed", "accepted", "in_progress"],
                "description": "Graph copies asynchronously; only 'completed' carries resource_id",
            },
            "resource_id": {"type": "string", "description": "Item ID of the copy"},
            "note": {"type": "string"},
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_copy_item(
    credentials: dict,
    item_id: str,
    destination_folder_id: str | None = None,
    new_name: str | None = None,
) -> dict[str, Any]:
    err = validate_id(item_id, "item_id")
    if err:
        return {"success": False, "error": err}
    if destination_folder_id:
        err = validate_id(destination_folder_id, "destination_folder_id")
        if err:
            return {"success": False, "error": err}

    body: dict[str, Any] = {}
    if new_name:
        try:
            body["name"] = check_name(new_name, "new_name")
        except UploadInputError as exc:
            return {"success": False, "error": str(exc)}
    try:
        token = await get_access_token(credentials)
        body["parentReference"] = await drive_root.parent_reference(
            token, destination_folder_id, with_drive_id=True
        )
        return await copy_item.copy(token, item_id, body)
    except Exception as exc:
        return _failed("onedrive_copy_item", exc)


# REVIEWED: destructiveHint=True: relocates (and may rename) an existing item.
# Evidence: PATCH /me/drive/items/{item_id}
@server.tool(
    name="onedrive_move_item",
    description=(
        "Move a file or folder to a different folder in OneDrive. "
        "Pass `item_id` (source), `destination_folder_id` (target folder, or 'root' "
        "for the drive root), and optionally `new_name` to rename while moving."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "Item ID to move"},
            "destination_folder_id": {
                "type": "string",
                "description": "Target folder item ID, or 'root' for the drive root",
            },
            "new_name": {
                "type": "string",
                "description": "New name for the item after moving (optional)",
            },
        },
        "required": ["item_id", "destination_folder_id"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "web_url": _NULLABLE_STR,
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_move_item(
    credentials: dict,
    item_id: str,
    destination_folder_id: str,
    new_name: str | None = None,
) -> dict[str, Any]:
    for label, value in (("item_id", item_id), ("destination_folder_id", destination_folder_id)):
        err = validate_id(value, label)
        if err:
            return {"success": False, "error": err}
    body: dict[str, Any] = {"parentReference": {"id": destination_folder_id}}
    if new_name:
        try:
            body["name"] = check_name(new_name, "new_name")
        except UploadInputError as exc:
            return {"success": False, "error": str(exc)}
    try:
        if drive_root.is_root(destination_folder_id):
            token = await get_access_token(credentials)
            body["parentReference"] = await drive_root.parent_reference(
                token, destination_folder_id, with_drive_id=False
            )
        item = await graph_request(
            credentials, "PATCH", f"/me/drive/items/{encode_id(item_id)}", json_data=body
        )
    except Exception as exc:
        return _failed("onedrive_move_item", exc)
    return {
        "success": True,
        "id": item.get("id"),
        "name": item.get("name"),
        "web_url": item.get("webUrl"),
    }


# REVIEWED: destructiveHint=False: adds a sharing permission, removes nothing.
# Evidence: POST /me/drive/items/{item_id}/createLink
@server.tool(
    name="onedrive_create_share_link",
    description=(
        "Create a shareable link for a OneDrive file or folder. "
        "Pass `item_id` (a specific item; the drive root cannot be shared), "
        "`link_type` ('view', 'edit', or 'embed'), "
        "`scope` ('anonymous' for a public link that needs no sign-in, or "
        "'organization' for internal). `expiry_days` (1-30) is required when "
        "`scope` is 'anonymous' so a public link cannot live forever, and optional "
        "otherwise."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="write_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "OneDrive item ID"},
            "link_type": {
                "type": "string",
                "description": "Link permission type",
                "enum": ["view", "edit", "embed"],
                "default": "view",
            },
            "scope": {
                "type": "string",
                "description": "Link scope",
                "enum": ["anonymous", "organization"],
                "default": "organization",
            },
            "expiry_days": {
                "type": "integer",
                "description": "Link expiry in days (1-30); required when scope is 'anonymous'",
            },
        },
        "required": ["item_id"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "share_url": {"type": "string"},
            "link_type": {"type": "string"},
            "scope": {"type": "string"},
            "expiry": _NULLABLE_STR,
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_create_share_link(
    credentials: dict,
    item_id: str,
    link_type: str = "view",
    scope: str = "organization",
    expiry_days: int | None = None,
) -> dict[str, Any]:
    err = validate_id(item_id, "item_id")
    if err:
        return {"success": False, "error": err}
    # `/items/root` is the whole drive: sharing it, especially anonymously, hands
    # out everything. The dedicated-item tools take real ids, so refuse the alias.
    if item_id.strip().lower() == "root":
        return {
            "success": False,
            "error": "item_id cannot be the drive root; share a specific item",
        }
    # Re-checked here although the schema's enum guards it: a direct call does
    # not pass through schema validation.
    if link_type not in VALID_LINK_TYPES:
        return {"success": False, "error": f"link_type must be one of {VALID_LINK_TYPES}"}
    if scope not in VALID_LINK_SCOPES:
        return {"success": False, "error": f"scope must be one of {VALID_LINK_SCOPES}"}
    # An anonymous link needs no sign-in, so without an expiry it is an
    # indefinite public URL. Graph only applies a default expiry when the tenant
    # enforces one, so require the caller to set one here.
    if scope == "anonymous" and expiry_days is None:
        return {
            "success": False,
            "error": (
                f"expiry_days ({MIN_LINK_EXPIRY_DAYS}-{MAX_LINK_EXPIRY_DAYS}) is required "
                "for an anonymous link"
            ),
        }

    # The `root` alias is refused above, but the drive root's real id is handed
    # back (as parent_id) by onedrive_get_file, so check the item itself: Graph
    # puts a `root` facet on the drive root. Never hand out a link to the whole
    # drive, by alias or by id.
    try:
        item = await graph_request(
            credentials,
            "GET",
            f"/me/drive/items/{encode_id(item_id)}",
            params={"$select": "id,root"},
        )
    except Exception as exc:
        return _failed("onedrive_create_share_link", exc)
    if "root" in item:
        return {"success": False, "error": "item_id is the drive root; share a specific item"}

    body: dict[str, Any] = {"type": link_type, "scope": scope}
    if expiry_days is not None:
        days = clamp(expiry_days, MIN_LINK_EXPIRY_DAYS, MAX_LINK_EXPIRY_DAYS)
        expiry = datetime.now(UTC) + timedelta(days=days)
        body["expirationDateTime"] = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = await graph_request(
            credentials, "POST", f"/me/drive/items/{encode_id(item_id)}/createLink", json_data=body
        )
    except Exception as exc:
        return _failed("onedrive_create_share_link", exc)

    link = result.get("link") or {}
    return {
        "success": True,
        "share_url": link.get("webUrl"),
        "link_type": link.get("type"),
        "scope": link.get("scope"),
        "expiry": result.get("expirationDateTime"),
    }


# REVIEWED: readOnlyHint=True. Evidence: GET /me/drive/root/search(q='...')
@server.tool(
    name="onedrive_search_files",
    description=(
        "Search for files and folders across your entire OneDrive by name or content. "
        "Pass `query` (search term), `max_results` (default: 25, max: 200), "
        "and optional `file_type` filter (e.g. 'docx', 'pdf', 'xlsx'). "
        "Returns matching items with id, name, type, size, modified_date, and web_url."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-Microsoft-Refresh-Token",
        "X-Microsoft-Client-Id",
        "X-Microsoft-Client-Secret",
        "X-Microsoft-Tenant-Id",
    ],
    permission_group="read_onedrive",
    required_scopes=["https://graph.microsoft.com/Files.ReadWrite"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (filename or content)"},
            "max_results": {
                "type": "integer",
                "description": "Max results (default: 25, max: 200)",
                "default": 25,
            },
            "file_type": {
                "type": "string",
                "description": "Filter by file extension, e.g. 'pdf', 'docx' (optional)",
            },
        },
        "required": ["query"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "count": {"type": "integer"},
            "query": {"type": "string"},
            "has_more": {"type": "boolean"},
            "items": {"type": "array", "items": ITEM_SCHEMA},
            "error": _ERROR,
        },
        "required": ["success"],
    }
)
async def onedrive_search_files(
    credentials: dict,
    query: str,
    max_results: int = 25,
    file_type: str | None = None,
) -> dict[str, Any]:
    if not query or not query.strip():
        return {"success": False, "error": "query cannot be empty"}
    max_results = clamp(max_results, 1, MAX_RESULTS_CAP)
    # The term is interpolated into the path as search(q='...'). OData escapes a
    # single quote by doubling it, so the term cannot break out of the string;
    # percent-encoding then keeps a '#' (which would truncate the path to a
    # fragment), '?', '&' and the rest from changing the URL. graph_request
    # leaves existing %-escapes intact, so this is not double-encoded.
    encoded_query = quote(query.strip().replace("'", "''"), safe="")
    params = {
        "$top": max_results,
        "$select": "id,name,file,folder,size,lastModifiedDateTime,webUrl",
    }
    try:
        result = await graph_request(
            credentials, "GET", f"/me/drive/root/search(q='{encoded_query}')", params=params
        )
    except Exception as exc:
        return _failed("onedrive_search_files", exc)

    items = result.get("value", [])
    # Graph's search has no extension filter, so the tool applies one itself.
    if file_type:
        ext = file_type.lower().lstrip(".")
        items = [
            i for i in items if "folder" not in i and i.get("name", "").lower().endswith(f".{ext}")
        ]
    return {
        "success": True,
        "count": len(items),
        "query": query,
        "has_more": "@odata.nextLink" in result,
        "items": [format_search_hit(i) for i in items],
    }
