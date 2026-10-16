"""Constants and validation shared by the OneDrive tools."""

import os

MAX_RESULTS_CAP = 200
MAX_ID_LENGTH = 500

_MIB = 1024 * 1024
# Graph's simple PUT limit. Anything larger goes through an upload session.
SIMPLE_UPLOAD_MAX_BYTES = 4 * _MIB
# Upload-session fragments must be multiples of 320 KiB (all but the last).
CHUNK_ALIGN_BYTES = 320 * 1024
UPLOAD_CHUNK_BYTES = 32 * CHUNK_ALIGN_BYTES  # 10 MiB
DEFAULT_MAX_UPLOAD_MB = 250
MAX_UPLOAD_ENV = "ONEDRIVE_MAX_UPLOAD_MB"
CONFLICT_BEHAVIORS = ["rename", "replace", "fail"]

VALID_LINK_TYPES = ["view", "edit", "embed"]
VALID_LINK_SCOPES = ["anonymous", "organization"]
MIN_LINK_EXPIRY_DAYS = 1
MAX_LINK_EXPIRY_DAYS = 30

# A copy is asynchronous on Graph's side: poll its monitor this many times,
# this far apart, before reporting it as still in progress.
COPY_POLL_ATTEMPTS = 5
COPY_POLL_INTERVAL_SECONDS = 2.0


def max_upload_bytes() -> int:
    """Total upload cap from ONEDRIVE_MAX_UPLOAD_MB (default 250 MB, floor 1 MB)."""
    raw = (os.getenv(MAX_UPLOAD_ENV) or "").strip()
    try:
        mb = int(raw) if raw else DEFAULT_MAX_UPLOAD_MB
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    return max(mb, 1) * _MIB


def validate_id(value: str | None, label: str = "ID") -> str | None:
    """Check an id that is interpolated into a URL path. Returns an error or None.

    A `/` or `..` would let a caller retarget the request at a different Graph
    resource, so every id-bearing tool runs its ids through this first.
    """
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if "/" in value or ".." in value:
        return f"{label} contains invalid characters"
    if len(value) > MAX_ID_LENGTH:
        return f"{label} is too long (max {MAX_ID_LENGTH} chars)"
    return None


def clamp(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)
