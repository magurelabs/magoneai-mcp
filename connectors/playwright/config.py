"""Settings for the Playwright connector, read from the environment on every call.

Read per call rather than at import, so a test (or an operator restarting with
a new value) never sees a stale setting.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "/artifacts"
MIB = 1024 * 1024
# What one tool result may carry in files, before base64. A result is sent in
# one HTTP response, and base64 grows it by a third, so 25 MB of file becomes
# about 33 MB on the wire that a client must hold in memory for one call. Kept
# low on purpose: the result-character budget only measures text, so this is
# the only bound on a returned binary. An operator who needs to return a larger
# recording raises ARTIFACTS_MAX_BYTES.
DEFAULT_MAX_BYTES = 25 * MIB

# Extension -> content type. Anything else Playwright writes is not returned.
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".yml": "application/yaml",
    ".yaml": "application/yaml",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

_FALSE_VALUES = {"0", "false", "no", "off"}
# Characters of the result budget kept for publish_test_report's JSON summary.
REPORT_TEXT_MARGIN = 20_000


def artifacts_root() -> Path:
    """The shared volume, resolved, so containment checks compare real paths."""
    raw = (os.getenv("ARTIFACTS_ROOT") or DEFAULT_ROOT).strip() or DEFAULT_ROOT
    return Path(os.path.realpath(raw))


def max_bytes() -> int:
    """ARTIFACTS_MAX_BYTES: the file bytes one tool result may carry in total."""
    raw = (os.getenv("ARTIFACTS_MAX_BYTES") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def delete_after_publish() -> bool:
    """ARTIFACTS_DELETE_AFTER_PUBLISH, true unless explicitly switched off."""
    raw = (os.getenv("ARTIFACTS_DELETE_AFTER_PUBLISH") or "").strip().lower()
    return raw not in _FALSE_VALUES


def report_max_bytes(result_chars: int) -> int:
    """The file bytes publish_test_report may return: what fits the result budget.

    A report goes back in ONE tool result, and a client that hands results to
    a model (or simply caps what it accepts) measures that result in
    characters: ``MCP_MAX_RESULT_CHARS``, 1,000,000 by default. Base64 turns
    every 3 bytes into 4 characters, so the files may carry three quarters of
    that budget, less a margin for the JSON summary: about 735 KB by default.
    ARTIFACTS_MAX_BYTES can only lower it. A deployment that wants bigger
    reports raises MCP_MAX_RESULT_CHARS (and ARTIFACTS_MAX_BYTES with it).
    """
    from_budget = max(0, (int(result_chars) - REPORT_TEXT_MARGIN) * 3 // 4)
    return min(max_bytes(), from_budget)


def content_type_for(path: Path) -> str | None:
    """The content type for a returnable file, or None when its type is refused."""
    return CONTENT_TYPES.get(path.suffix.lower())
