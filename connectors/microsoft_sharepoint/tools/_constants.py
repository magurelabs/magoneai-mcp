"""Shared constants for Microsoft SharePoint MCP tools (Graph sites & drives)."""

import re

MAX_RESULTS_CAP = 200
MAX_ID_LENGTH = 500
MAX_SEARCH_LEN = 256

CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}$")


def validate_drive_item_id(value: str, label: str = "ID") -> str | None:
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if "/" in value or ".." in value:
        return f"{label} contains invalid characters"
    if len(value) > MAX_ID_LENGTH:
        return f"{label} is too long (max {MAX_ID_LENGTH} chars)"
    return None


def validate_site_id(value: str, label: str = "site_id") -> str | None:
    """Site ids from Graph often contain commas; still block path traversal."""
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if ".." in value or "\n" in value or "\r" in value:
        return f"{label} contains invalid characters"
    if len(value) > 300:
        return f"{label} is too long"
    return None


def validate_hostname(hostname: str) -> str | None:
    h = (hostname or "").strip().lower()
    if not h:
        return "hostname cannot be empty"
    if not _HOSTNAME_RE.match(h):
        return "hostname must look like tenant.sharepoint.com"
    if ".." in h or "/" in h:
        return "invalid hostname"
    return None


def validate_site_path(site_path: str) -> str | None:
    if site_path is None:
        return None
    p = site_path.strip()
    if not p:
        return None
    if ".." in p:
        return "site_path cannot contain .."
    if len(p) > 500:
        return "site_path is too long"
    return None
