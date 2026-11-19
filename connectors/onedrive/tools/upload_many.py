"""onedrive_upload_files: several inline files in one call, through `attachments`.

Each attachment is an inline object, `{filename, content_base64,
content_type}`, which any MCP client can send. A platform that resolves file
references (MagOneAI does, for a parameter named `attachments`) rewrites each
reference into exactly that shape before the call leaves it, so the tool only
ever sees inline content and works the same from every client.

Every file goes through the same validation and the same simple PUT or
upload session as onedrive_upload_file. One bad file fails alone; a call
whose list is not inline objects at all is refused before any request.
"""

from typing import Any

import httpx
import structlog

from ._constants import max_upload_bytes
from ._format import format_uploaded
from .upload_graph import UPLOAD_TIMEOUT, UploadError, upload_bytes
from .upload_input import UploadInputError, item_path, resolve_payload

MAX_FILES_PER_CALL = 20

logger = structlog.get_logger("mcp.onedrive")

UNRESOLVED_MESSAGE = (
    "Each attachment needs filename and content_base64. A run-file key or "
    "file_id is only filled in by a platform that resolves attachments."
)


def inline_items(attachments: Any) -> list[dict[str, Any]]:
    """The attachment objects, or UploadInputError before anything is sent."""
    if not isinstance(attachments, list) or not attachments:
        raise UploadInputError("attachments must be a non-empty list")
    if len(attachments) > MAX_FILES_PER_CALL:
        raise UploadInputError(f"At most {MAX_FILES_PER_CALL} files per call")
    for item in attachments:
        if not isinstance(item, dict) or "content_base64" not in item:
            raise UploadInputError(UNRESOLVED_MESSAGE)
    return attachments


async def _upload_one(
    client: httpx.AsyncClient,
    token: str,
    item: dict[str, Any],
    destination: tuple[str | None, str | None],
    conflict: str,
) -> dict[str, Any]:
    payload = resolve_payload(
        item.get("filename"),
        None,
        item.get("content_base64"),
        item.get("content_type"),
        max_upload_bytes(),
    )
    path = item_path(destination[0], destination[1], payload.filename)
    result, method = await upload_bytes(client, token, path, payload, conflict)
    return {"upload_method": method, **format_uploaded(result)}


async def upload_many(
    token: str,
    items: list[dict[str, Any]],
    destination: tuple[str | None, str | None],
    conflict: str,
) -> dict[str, Any]:
    """Upload each item to `destination` (folder_path, folder_id), one at a time."""
    uploaded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
        for item in items:
            name = str(item.get("filename") or "")
            try:
                uploaded.append(await _upload_one(client, token, item, destination, conflict))
            except (UploadInputError, UploadError, httpx.HTTPError) as exc:
                failed.append({"filename": name, "error": str(exc) or type(exc).__name__})
            except Exception as exc:
                # Earlier files are already in OneDrive. Letting this escape
                # would drop `uploaded`, and a retry would upload them twice.
                # Only the type is logged or returned: the text may echo a body.
                kind = type(exc).__name__
                logger.warning("onedrive_upload_files_item_error", error_type=kind)
                failed.append({"filename": name, "error": f"Upload failed unexpectedly ({kind})"})
    return {"success": not failed, "uploaded": uploaded, "failed": failed}
