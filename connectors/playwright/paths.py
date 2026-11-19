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

# Where a Playwright reply names the files its call wrote (0.0.85, captured live):
#
#   ### Result
#   - [Screenshot of viewport](out/page-2026-11-19T08-11-40-598Z.png)
#   ### Snapshot
#   - [Snapshot](out/page-2026-11-19T08-11-40-503Z.yml)
#
# Only a whole line of that shape, inside one of these sections, counts. The
# `### Page` section (URL, title), the `### Ran Playwright code` section and
# any snapshot or console text are the page's words, not Playwright's, so a name
# that appears there is never taken as a file this run produced.
_REPORTING_SECTIONS = frozenset({"result", "snapshot"})

# `### Result` is trusted only for tools whose result IS the file they wrote.
# Other tools put text there that a page controls (console output, network
# bodies, search matches), and a page can print a line shaped like a report.
_FILE_WRITING_TOOLS = frozenset(
    {
        "browser_pdf_save",
        "browser_snapshot",
        "browser_stop_tracing",
        "browser_stop_video",
        "browser_take_screenshot",
    }
)

# Tools whose reply echoes page content: nothing in it ever counts as a report,
# `### Snapshot` included, since the echoed text can open that section too.
_PAGE_ECHO_TOOLS = frozenset(
    {
        "browser_console_messages",
        "browser_find",
        "browser_network_request",
        "browser_network_requests",
        "browser_run_code_unsafe",
    }
)
_SECTION = re.compile(r"^###\s+(?P<name>.+?)\s*$")
_REPORTED_LINE = re.compile(r"^-\s\[[^\]\n]{0,200}\]\((?P<path>[^()\n]{1,1024})\)$")
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

    The first half of ``resolve_artifact``, without the existence and type
    checks, which the caller makes itself on the real path.
    """
    text = unwrap_path(raw_path)
    if not text or "\x00" in text:
        return None
    real = _candidate(text, root)
    return str(real) if _is_within(root, real) else None


def reported_paths(text: str, root: Path, tool: str) -> list[str]:
    """Real paths, inside ``root``, that a Playwright reply to ``tool`` reports writing.

    Only whole ``- [label](path)`` lines, outside a code fence, count, and only
    for a returnable file type: in ``### Snapshot`` for any tool that does not
    echo page content, and in ``### Result`` only for a tool that writes files.
    A reply that opens a reporting section twice has page text posing as a
    section, so it reports nothing. The caller still checks that each file
    exists and was written during the call before trusting it.
    """
    if tool in _PAGE_ECHO_TOOLS:
        return []
    trusted = _REPORTING_SECTIONS if tool in _FILE_WRITING_TOOLS else frozenset({"snapshot"})
    found: list[str] = []
    seen: set[str] = set()
    section, fenced = "", False
    for line in (text or "").splitlines():
        if line.startswith("```"):
            # An inline snapshot or code block: page content, never a report.
            fenced = not fenced
            continue
        header = None if fenced else _SECTION.match(line)
        if header:
            section = header.group("name").strip().lower()
            if section in _REPORTING_SECTIONS and section in seen:
                return []
            seen.add(section)
            continue
        if fenced or section not in trusted:
            continue
        match = _REPORTED_LINE.match(line.rstrip())
        if not match or not match.group("path").lower().endswith(_RETURNABLE_SUFFIXES):
            continue
        real = candidate_real_path(match.group("path"), root)
        if real is not None and real not in found:
            found.append(real)
    return found


def relative_to_root(path: Path, root: Path) -> str:
    """The path as the model should pass it back: relative to the root, POSIX style."""
    return path.relative_to(root).as_posix()
