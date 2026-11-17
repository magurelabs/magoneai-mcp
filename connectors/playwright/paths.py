"""Resolve a path the model passed to a returnable file inside the artifacts root.

The model copies the path out of Playwright's reply, so it is untrusted: it may
be absolute (``/artifacts/video-1.webm``) or relative to the root
(``.playwright-mcp/page-1.png``), relative to Playwright's own working
directory (``../../artifacts/video-1.webm``) or the whole markdown link
Playwright replies with. It may also be an attempt to read any other file this
container can see. Every check below runs on the REAL path (symlinks
followed), so a link inside the root that points outside it is refused exactly
like ``../../etc/passwd``.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

from .config import CONTENT_TYPES, content_type_for

_ALLOWED = ", ".join(sorted(CONTENT_TYPES))

_MARKDOWN_LINK = re.compile(r"^\s*-?\s*\[[^\]]*\]\((?P<path>[^)]+)\)\s*$")

# Tokens a relayed result's text is split into to find the files it names
# ("Saved to /artifacts/page-1.png", or a "[Video](path)" link). Splitting first
# and testing each token's extension avoids running a regex over a huge blob,
# where a backtracking pattern would hang on, say, a megabyte of console output.
_TOKEN_SPLIT = re.compile(r"[\s()\[\]<>'\"`,;]+")
_RETURNABLE_SUFFIXES = tuple(sorted(CONTENT_TYPES))


class ArtifactError(ValueError):
    """A path that cannot be returned. The message is shown to the model."""


def _is_within(root: Path, candidate: Path) -> bool:
    return candidate != root and os.path.commonpath([root, candidate]) == str(root)


def unwrap_path(raw_path: str) -> str:
    """Strip a markdown link (``- [Video](../../artifacts/v.webm)``) or ``<...>``."""
    text = (raw_path or "").strip()
    match = _MARKDOWN_LINK.match(text)
    if match:
        text = match.group("path").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text


def _candidate(text: str, root: Path) -> Path:
    """The real path to check.

    Absolute paths are taken as they are. A relative path is tried against the
    root first; Playwright also prints paths relative to its own working
    directory (``../../artifacts/video-1.webm`` from ``/home/node``), so when
    that escapes the root or does not exist, the basename inside the root is
    used. Containment of the REAL path, checked by the caller, is the boundary.
    """
    if os.path.isabs(text):
        return Path(os.path.realpath(text))
    joined = Path(os.path.realpath(root / text))
    if _is_within(root, joined) and os.path.lexists(joined):
        return joined
    return Path(os.path.realpath(root / PurePosixPath(text).name))


def resolve_artifact(raw_path: str, root: Path) -> Path:
    """Return the real path of a returnable regular file under ``root``."""
    text = unwrap_path(raw_path)
    if not text:
        raise ArtifactError("path is required.")
    if "\x00" in text:
        raise ArtifactError("path contains an invalid character.")

    real = _candidate(text, root)
    if not _is_within(root, real):
        raise ArtifactError("path is outside the artifacts directory.")

    try:
        info = os.stat(real)
    except FileNotFoundError:
        raise ArtifactError(
            f"No artifact at '{text}'. Call list_artifacts to see what exists."
        ) from None
    except OSError as exc:
        raise ArtifactError(f"Cannot read '{text}' ({type(exc).__name__}).") from None
    if stat.S_ISDIR(info.st_mode):
        raise ArtifactError(f"'{text}' is a directory, not a file.")
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactError(f"'{text}' is not a regular file.")
    if content_type_for(real) is None:
        raise ArtifactError(f"File type '{real.suffix or 'none'}' is not allowed. Use: {_ALLOWED}.")
    return real


def candidate_real_path(raw_path: str, root: Path) -> str | None:
    """The real path a raw string would address under ``root``, or None if outside it.

    Like the first half of ``resolve_artifact`` but without the existence, file-type
    or content-type checks: used to record, from a relayed result's text, the files
    a session has produced, which may not have finished being written yet.
    """
    text = unwrap_path(raw_path)
    if not text or "\x00" in text:
        return None
    real = _candidate(text, root)
    return str(real) if _is_within(root, real) else None


def extract_paths(text: str, root: Path) -> list[str]:
    """The real paths of returnable files a relayed result's text names, inside ``root``."""
    found: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_SPLIT.split(text or ""):
        if not token.lower().endswith(_RETURNABLE_SUFFIXES):
            continue
        real = candidate_real_path(token, root)
        if real is not None and real not in seen:
            seen.add(real)
            found.append(real)
    return found


def relative_to_root(path: Path, root: Path) -> str:
    """The path as the model should pass it back: relative to the root, POSIX style."""
    return path.relative_to(root).as_posix()
