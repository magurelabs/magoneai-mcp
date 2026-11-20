"""List recent returnable files under the artifacts root, newest first.

Symlinks are skipped rather than followed, both for directories (os.walk with
followlinks=False) and for files (lstat), so the listing can never describe a
file outside the root.
"""

from __future__ import annotations

import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import content_type_for
from .paths import relative_to_root

SECONDS_PER_MINUTE = 60


def _entry(path: Path, root: Path, info: os.stat_result) -> dict:
    return {
        "path": relative_to_root(path, root),
        "size_bytes": info.st_size,
        "modified_at": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
        "_mtime": info.st_mtime,
    }


def _scan(root: Path, cutoff: float, allowed: set[str] | None) -> list[dict]:
    found: list[dict] = []
    for current, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(current) / name
            if content_type_for(path) is None:
                continue
            # Outside local mode, only files this caller's run produced are listed.
            if allowed is not None and str(path) not in allowed:
                continue
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= cutoff:
                found.append(_entry(path, root, info))
    return found


def list_recent(
    root: Path, since_minutes: int, limit: int, allowed: set[str] | None = None
) -> dict:
    """``{"artifacts": [...], "count", "truncated"}`` for files modified in the window.

    ``allowed`` (when not None) restricts the listing to the real paths this
    caller's own run produced; None lists every returnable file (local mode).
    """
    if not root.is_dir():
        return {"artifacts": [], "count": 0, "truncated": False}
    cutoff = time.time() - since_minutes * SECONDS_PER_MINUTE
    found = sorted(_scan(root, cutoff, allowed), key=lambda item: item["_mtime"], reverse=True)
    kept = found[:limit]
    for item in kept:
        item.pop("_mtime")
    return {"artifacts": kept, "count": len(kept), "truncated": len(found) > limit}
