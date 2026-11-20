"""Normalization of Graph driveItem payloads into the tools' output shape."""

from typing import Any

# Shape of a driveItem after `format_item`. Advertised in the tools' output
# schemas; extra keys are allowed, so the fuller `full=True` form
# (created_date, web_url, parent_id, ...) still validates against it.
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["file", "folder"]},
        "size": {"type": ["integer", "null"]},
        "modified_date": {"type": ["string", "null"]},
        "mime_type": {"type": ["string", "null"]},
        "web_url": {"type": ["string", "null"]},
    },
}


def item_type(item: dict[str, Any]) -> str:
    return "folder" if "folder" in item else "file"


def format_item(item: dict[str, Any], full: bool = False) -> dict[str, Any]:
    """Normalize a Graph driveItem into a flat dict."""
    is_folder = "folder" in item
    out: dict[str, Any] = {
        "id": item.get("id"),
        "name": item.get("name"),
        "type": item_type(item),
        "size": item.get("size"),
        "modified_date": item.get("lastModifiedDateTime"),
    }
    if not is_folder:
        out["mime_type"] = (item.get("file") or {}).get("mimeType")
    if full:
        out["created_date"] = item.get("createdDateTime")
        out["web_url"] = item.get("webUrl")
        parent = item.get("parentReference") or {}
        out["parent_id"] = parent.get("id")
        out["parent_path"] = parent.get("path")
        if is_folder:
            out["child_count"] = (item.get("folder") or {}).get("childCount")
    return out


def format_search_hit(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "type": item_type(item),
        "size": item.get("size"),
        "modified_date": item.get("lastModifiedDateTime"),
        "web_url": item.get("webUrl"),
        "mime_type": (item.get("file") or {}).get("mimeType"),
    }


def format_uploaded(item: dict[str, Any]) -> dict[str, Any]:
    """The fields an upload returns: where the file landed and how to open it."""
    parent = item.get("parentReference") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "size": item.get("size"),
        "web_url": item.get("webUrl"),
        "parent_id": parent.get("id"),
        "parent_path": parent.get("path"),
    }
