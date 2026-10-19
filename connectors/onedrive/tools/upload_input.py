"""Upload inputs: validate the destination and resolve the bytes to send.

Content arrives one of two ways, both in the call itself: `content` (text) or
`content_base64` (any bytes). v1 had a third, `file_id`, which read a file out
of the MagOne platform through a platform-internal route. A public connector
uses the caller's own credentials and storage and works the same on a laptop,
so that path is not here: a caller holding a file sends its bytes.
"""

import base64
import binascii
import mimetypes
from dataclasses import dataclass
from urllib.parse import quote

from ._constants import CONFLICT_BEHAVIORS, validate_id

# Characters OneDrive refuses in a file or folder name.
_FORBIDDEN_NAME_CHARS = set('"*:<>?/\\|')
_MAX_NAME_LENGTH = 255
_OCTET_STREAM = "application/octet-stream"
_CONTENT_SOURCES = ("content", "content_base64")


class UploadInputError(ValueError):
    """An upload argument is invalid; the message is safe to show the model."""


@dataclass(frozen=True)
class UploadPayload:
    filename: str
    content: bytes
    content_type: str


def _check_name(name: str | None, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise UploadInputError(f"{label} cannot be empty")
    if name in (".", "..") or len(name) > _MAX_NAME_LENGTH:
        raise UploadInputError(f"{label} is not a valid OneDrive name")
    if any(ch in _FORBIDDEN_NAME_CHARS or ord(ch) < 32 for ch in name):
        raise UploadInputError(f"{label} contains characters OneDrive does not allow")
    return name


def item_path(folder_path: str | None, folder_id: str | None, filename: str) -> str:
    """The Graph path addressing `filename` inside the destination folder.

    `folder_id` wins over `folder_path`; with neither, the drive root. Every
    segment is validated and percent-encoded, so no argument can step out of
    the path it names.
    """
    name = quote(_check_name(filename, "filename"), safe="")
    if folder_id:
        err = validate_id(folder_id, "folder_id")
        if err:
            raise UploadInputError(err)
        return f"/me/drive/items/{quote(folder_id, safe='')}:/{name}:"
    segments = [s for s in (folder_path or "").replace("\\", "/").split("/") if s.strip()]
    encoded = [quote(_check_name(s, "folder_path segment"), safe="") for s in segments]
    return f"/me/drive/root:/{'/'.join([*encoded, name])}:"


def check_conflict(value: str | None) -> str:
    behavior = (value or "rename").strip().lower()
    if behavior not in CONFLICT_BEHAVIORS:
        raise UploadInputError(f"conflict_behavior must be one of {CONFLICT_BEHAVIORS}")
    return behavior


def _check_size(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise UploadInputError(
            f"File is {size} bytes, over the {max_bytes} byte upload limit "
            "(ONEDRIVE_MAX_UPLOAD_MB)."
        )


def _decode(content: str | None, content_base64: str | None, max_bytes: int) -> bytes:
    if content_base64 is not None:
        # Decoded size is about 3/4 of the encoded length: refuse before decoding.
        _check_size(len(content_base64) * 3 // 4 - 2, max_bytes)
        try:
            raw = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UploadInputError("content_base64 is not valid base64") from exc
    else:
        raw = (content or "").encode("utf-8")
    _check_size(len(raw), max_bytes)
    return raw


def resolve_payload(
    filename: str | None,
    content: str | None,
    content_base64: str | None,
    content_type: str | None,
    max_bytes: int,
) -> UploadPayload:
    """Bytes, name and type for the upload, from exactly one content source."""
    given = {"content": content, "content_base64": content_base64}
    sources = [k for k in _CONTENT_SOURCES if given[k] is not None]
    if len(sources) != 1:
        raise UploadInputError("Provide exactly one of content or content_base64")
    if not filename:
        raise UploadInputError("filename is required")
    raw = _decode(content, content_base64, max_bytes)
    fallback = "text/plain" if sources[0] == "content" else _OCTET_STREAM
    guessed = mimetypes.guess_type(filename)[0] or fallback
    return UploadPayload(filename, raw, content_type or guessed)
