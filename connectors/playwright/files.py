"""Read a file off the shared volume and hand it back as an MCP embedded resource.

There is no storage behind this connector. A file the browser saved goes back
to the caller inside the tool result, as the specification's
``EmbeddedResource`` holding ``BlobResourceContents`` (base64 bytes, a MIME
type and an ``artifact://<name>`` URI), and the caller keeps it wherever it
keeps files. A platform such as MagOne stores the returned resource itself; a
laptop client can save it to disk.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from urllib.parse import quote

import structlog
from mcp.types import BlobResourceContents, EmbeddedResource

from .config import content_type_for
from .paths import ArtifactError

logger = structlog.get_logger("mcp.playwright.artifacts")

URI_SCHEME = "artifact://"


def output_filename(source: Path, requested: str | None) -> str:
    """The returned name: a bare basename that keeps the source file's extension."""
    name = Path((requested or "").strip().replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return source.name
    if Path(name).suffix.lower() != source.suffix.lower():
        name = f"{name}{source.suffix.lower()}"
    return name


def read_bounded(path: Path, limit: int) -> bytes:
    """Bytes of ``path``, refusing a file over ``limit`` or one swapped for a link."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ArtifactError(f"Cannot open the artifact ({type(exc).__name__}).") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError("The artifact is not a regular file.")
        if info.st_size > limit:
            raise ArtifactError(
                f"File is {info.st_size:,} bytes, over the {limit:,} byte limit "
                "(ARTIFACTS_MAX_BYTES)."
            )
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ArtifactError(f"File grew past the {limit:,} byte limit while reading.")
    if not content:
        raise ArtifactError("File is empty. If it is a video, call browser_stop_video first.")
    return content


def remove_local(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("artifact_delete_failed", path=path.name, error=type(exc).__name__)
        return False


def artifact_uri(filename: str) -> str:
    """``artifact://<name>``, percent-encoded so any file name is a valid URI."""
    return URI_SCHEME + quote(filename, safe="")


def embedded(filename: str, content: bytes, mime_type: str) -> EmbeddedResource:
    """One file as an embedded resource: base64 bytes, MIME type and URI."""
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(
            uri=artifact_uri(filename),
            mime_type=mime_type,
            blob=base64.b64encode(content).decode("ascii"),
        ),
    )


def describe(filename: str, content: bytes, mime_type: str) -> dict:
    """What the text item says about a returned file."""
    return {
        "filename": filename,
        "uri": artifact_uri(filename),
        "mime_type": mime_type,
        "size_bytes": len(content),
    }


def read_artifact(path: Path, options: dict) -> tuple[dict, EmbeddedResource]:
    """Read ``path`` into an embedded resource; delete the local copy if asked.

    ``options`` carries ``filename`` (requested name or None), ``max_bytes`` and
    ``delete``. The file is deleted only after it has been read in full, so a
    refusal leaves it on the volume for another attempt.
    """
    mime_type = content_type_for(path) or "application/octet-stream"
    filename = output_filename(path, options.get("filename"))
    content = read_bounded(path, int(options["max_bytes"]))
    deleted = remove_local(path) if options.get("delete") else False
    logger.info("artifact_returned", size_bytes=len(content), mime_type=mime_type, deleted=deleted)
    summary = {**describe(filename, content, mime_type), "deleted_local_copy": deleted}
    return summary, embedded(filename, content, mime_type)
