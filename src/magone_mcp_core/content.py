"""Content handles: how a tool moves bytes without holding storage credentials.

The boundary gate refuses `MINIO_*`, `DATABASE_URL` and a `minio` import inside
a connector, and its message says storage access goes through a content
handle. This is that handle.

The problem it solves is narrow and specific. Several tools legitimately need
to put bytes somewhere (a CSV export, an email attachment, a rendered deck) or
read bytes back. v1 solved it by handing every server the object-store keys and
the platform's `DATABASE_URL`, so a connector that only searched the web could
read any tenant's files. That is the coupling this repository exists to
remove, and removing it is not optional: the same keys are what makes one
compromised server a fleet-wide incident.

## Not yet adopted, and that is deliberate

**Nothing in this repository calls this module today.** No connector here
needs platform storage, and a platform that does may solve the same problem
another way, for example through its own file API. So "content handles replace
every storage reach-through" describes an interface that exists, not a
migration that has happened.

It is written down here because the opposite is worse: a documented control
that is not implemented reads as a guarantee, and someone reads the sentence
and stops looking - the same defect as the `tools/list` read-only filter this
repository had to fix. Wire a connector onto this interface or retire it; do
not cite it as a control in the meantime.

**The interface is the deliverable, not any one implementation.** A connector
asks a `ContentHandleService` for a handle and never learns where the bytes
live. Two implementations ship behind it:

- `PresignedMinter` - the default. The service mints a time-bounded URL and
  the caller uploads or downloads directly. No proxying, no bytes through the
  platform. A presigned URL is replayable until it expires; object stores
  offer no single-redemption semantics, so this design makes no "single-use"
  claim for the URL. The handle RECORD is single-use.
- a platform-proxy implementation, which belongs to the platform that runs
  the connector because it is the only part that knows that platform exists.
  It ships behind this same interface so that a deployment whose ingress
  cannot answer a presigned URL still works, and so the choice is a
  configuration rather than a rewrite.

## Two properties that are load-bearing

**`object_key` is chosen here, never by the caller.** A caller that names its
own key can name someone else's. It does not have to overwrite anything to do
damage: writing to a key another tenant is about to use makes their write fail
and hands their reader your bytes. An owner tag on an existing object does not
prevent that, because squatting is not overwriting. So the key is derived from
the principal and a fresh UUID, and a caller-supplied name only ever survives
as a sanitised display filename inside it.

**A handle is bound to its principal, and expires.** A download handle is
issued only for a key under the principal's own prefix, and redemption checks
the caller is the principal it was issued to: "a handle is a name, not a
capability". `redeemed_at` is set on first use and a second redemption is
refused. Lifetimes: 15 minutes for a write, 60 for a read.
"""

from __future__ import annotations

import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

__all__ = [
    "ContentHandle",
    "ContentHandleError",
    "ContentHandleService",
    "Direction",
    "HandleExpired",
    "HandleRedeemed",
    "HandleState",
    "PresignedMinter",
    "PresignedUrlSigner",
    "safe_display_name",
]

# Everything outside this set is dropped from a caller-supplied filename before
# it is allowed anywhere near a key. Deliberately ASCII-only: the key is an
# infrastructure identifier, and a non-ASCII byte in one has to survive a
# latin-1 HTTP header on the way back out, which it does not. The display name
# a user sees is carried as metadata, not as the key.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# Long enough for a large attachment on a slow vendor API,
# short enough that a leaked URL is a narrow window.
_UPLOAD_TTL_SECONDS = 15 * 60
_DOWNLOAD_TTL_SECONDS = 60 * 60


def safe_display_name(filename: str, fallback: str = "file") -> str:
    """An ASCII, path-free version of a caller's filename.

    Path separators, `..`, control bytes and every non-ASCII character are
    removed rather than escaped. A name that reduces to nothing becomes
    `fallback`, because an empty segment in a key is a different key.

    The extension is preserved separately when the stem does not survive. A
    wholly non-ASCII name like `تقرير.pptx` becomes `file.pptx` rather than
    `pptx`: the extension is what tells a browser and a user what the thing
    is, and losing it turns a deck into an unopenable blob. Sanitising the
    name and the extension together is how that gets lost quietly.
    """
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""

    clean_stem = _UNSAFE.sub("_", stem).strip("._-")
    clean_extension = _UNSAFE.sub("", extension).strip("._-").lower()

    # A stem of only separators is the same as no stem. `тест-файл.csv` reduces
    # to `-`, which is a filename nobody can read and a key nobody can trace.
    if not clean_stem.strip("._-"):
        clean_stem = fallback
    if not clean_extension:
        return clean_stem[:120]
    return f"{clean_stem[: 120 - len(clean_extension) - 1]}.{clean_extension[:20]}"


# Not StrEnum: these are exported and handed to caller-written signers, and
# StrEnum would change what str() of a member returns.
class Direction(str, Enum):  # noqa: UP042
    """What a handle permits. A handle is never both.

    `write` and `read`: an upload handle cannot be read back.
    """

    UPLOAD = "upload"
    DOWNLOAD = "download"


class HandleState(str, Enum):  # noqa: UP042
    """The handle state model. `finalized` is set by the platform once it has
    registered the object the handle produced."""

    MINTED = "minted"
    REDEEMED = "redeemed"
    EXPIRED = "expired"
    FINALIZED = "finalized"


class ContentHandleError(Exception):
    """A handle could not be issued or redeemed."""


class HandleExpired(ContentHandleError):
    pass


class HandleRedeemed(ContentHandleError):
    pass


@dataclass(frozen=True)
class ContentHandle:
    """A time-bounded, single-use permission to move one object.

    `url` is what the caller actually uses. Everything else is the record that
    makes the permission auditable: who asked, which way the bytes go, which
    object, and when it stops being valid.
    """

    id: str
    principal: str
    direction: Direction
    object_key: str
    expires_at: float
    url: str
    display_name: str
    redeemed_at: float | None = None
    state: HandleState = HandleState.MINTED
    metadata: dict[str, str] = field(default_factory=dict)

    def seconds_remaining(self, now: float | None = None) -> float:
        return self.expires_at - (time.time() if now is None else now)

    def is_expired(self, now: float | None = None) -> bool:
        return self.seconds_remaining(now) <= 0


class PresignedUrlSigner(Protocol):
    """Whatever can sign a URL for the deployment's object store.

    Kept as a Protocol so the core package depends on no storage SDK. A
    deployment supplies one; the tests supply a deterministic fake.
    """

    def sign(self, object_key: str, direction: Direction, ttl_seconds: int) -> str: ...


class ContentHandleService(ABC):
    """What a tool is given. It has no other way to reach storage."""

    @abstractmethod
    async def issue_upload(
        self,
        principal: str,
        filename: str,
        *,
        content_type: str | None = None,
        ttl_seconds: int = _UPLOAD_TTL_SECONDS,
    ) -> ContentHandle:
        """A permission to write one new object. The key is chosen here."""

    @abstractmethod
    async def issue_download(
        self,
        principal: str,
        object_key: str,
        *,
        ttl_seconds: int = _DOWNLOAD_TTL_SECONDS,
    ) -> ContentHandle:
        """A permission to read one existing object the principal may read."""

    @abstractmethod
    async def redeem(self, handle_id: str, principal: str) -> ContentHandle:
        """Mark a handle used by the principal it was issued to.

        Raises rather than returning a stale handle, and refuses any other
        principal: a handle is a name, not a capability.
        """


class _InMemoryHandleStore:
    """Handle records for an implementation with nowhere else to put them.

    A real deployment keeps these in its own database; the presigned minter is
    usable without one, and says so rather than pretending to be durable.
    """

    def __init__(self) -> None:
        self._handles: dict[str, ContentHandle] = {}

    def put(self, handle: ContentHandle) -> None:
        self._handles[handle.id] = handle

    def get(self, handle_id: str) -> ContentHandle | None:
        return self._handles.get(handle_id)

    def replace(self, handle: ContentHandle) -> None:
        self._handles[handle.id] = handle


class PresignedMinter(ContentHandleService):
    """The default: mint a bounded URL and let the caller move the bytes.

    Bytes never pass through the platform, which is the reason to prefer it.
    Its one requirement is that whoever holds the handle can reach the object
    store directly, and that is exactly the assumption a locked-down ingress
    breaks - which is why the interface exists and why a platform can ship a
    proxy behind it.
    """

    def __init__(
        self,
        signer: PresignedUrlSigner,
        *,
        prefix: str = "handles",
        store: _InMemoryHandleStore | None = None,
    ) -> None:
        self._signer = signer
        self._prefix = prefix.strip("/")
        self._store = store or _InMemoryHandleStore()

    def _owner_prefix(self, principal: str) -> str:
        owner = _UNSAFE.sub("_", principal).strip("._") or "unknown"
        return f"{self._prefix}/{owner}/"

    def _key_for(self, principal: str, filename: str) -> tuple[str, str]:
        """Derive the object key. The caller's name survives only as a suffix.

        The UUID is what makes the key unguessable, so a caller cannot name a
        key another principal is about to use. `principal` is included for
        operator legibility, not for isolation: isolation is the UUID.
        """
        display = safe_display_name(filename)
        return f"{self._owner_prefix(principal)}{uuid.uuid4().hex}_{display}", display

    async def issue_upload(
        self,
        principal: str,
        filename: str,
        *,
        content_type: str | None = None,
        ttl_seconds: int = _UPLOAD_TTL_SECONDS,
    ) -> ContentHandle:
        if not principal:
            raise ContentHandleError("a handle needs a principal; refusing to issue one")
        if ttl_seconds <= 0:
            raise ContentHandleError("a handle with no lifetime is not a handle")

        object_key, display = self._key_for(principal, filename)
        handle = ContentHandle(
            id=uuid.uuid4().hex,
            principal=principal,
            direction=Direction.UPLOAD,
            object_key=object_key,
            expires_at=time.time() + ttl_seconds,
            url=self._signer.sign(object_key, Direction.UPLOAD, ttl_seconds),
            display_name=display,
            metadata={"content_type": content_type} if content_type else {},
        )
        self._store.put(handle)
        return handle

    async def issue_download(
        self,
        principal: str,
        object_key: str,
        *,
        ttl_seconds: int = _DOWNLOAD_TTL_SECONDS,
    ) -> ContentHandle:
        if not principal:
            raise ContentHandleError("a handle needs a principal; refusing to issue one")
        if ttl_seconds <= 0:
            raise ContentHandleError("a handle with no lifetime is not a handle")
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ContentHandleError(f"not a usable object key: {object_key!r}")
        # Only the principal's own objects. Without this, any principal could
        # be handed a signed URL for another tenant's key by naming it.
        if not object_key.startswith(self._owner_prefix(principal)):
            raise ContentHandleError("that object does not belong to this principal")

        handle = ContentHandle(
            id=uuid.uuid4().hex,
            principal=principal,
            direction=Direction.DOWNLOAD,
            object_key=object_key,
            expires_at=time.time() + ttl_seconds,
            url=self._signer.sign(object_key, Direction.DOWNLOAD, ttl_seconds),
            display_name=safe_display_name(object_key.rsplit("/", 1)[-1]),
        )
        self._store.put(handle)
        return handle

    async def redeem(self, handle_id: str, principal: str) -> ContentHandle:
        handle = self._store.get(handle_id)
        if handle is None:
            raise ContentHandleError("no such handle")
        if handle.principal != principal:
            # Reported as absent, not as someone else's: the caller learns
            # nothing about handles it does not own.
            raise ContentHandleError("no such handle")
        if handle.redeemed_at is not None:
            raise HandleRedeemed(f"handle {handle_id} was already redeemed; a handle is single-use")
        if handle.is_expired():
            expired = ContentHandle(
                **{**handle.__dict__, "state": HandleState.EXPIRED},
            )
            self._store.replace(expired)
            raise HandleExpired(f"handle {handle_id} expired")

        redeemed = ContentHandle(
            **{
                **handle.__dict__,
                "redeemed_at": time.time(),
                "state": HandleState.REDEEMED,
            }
        )
        self._store.replace(redeemed)
        return redeemed
