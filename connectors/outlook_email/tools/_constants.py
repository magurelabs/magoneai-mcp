"""Shared constants and helpers for Outlook email tools."""

import base64
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
    "X-Allowed-Mailboxes",
]

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# Wrappings tolerated around an incoming base64 attachment payload; see
# normalize_base64.
DATA_URI_RE = re.compile(r"^data:[\w.+-]*/?[\w.+-]*(;[\w.+-]+=[\w.+-]+)*;base64,", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")

MAX_SUBJECT_LEN = 998
MAX_BODY_SIZE = 5 * 1024 * 1024  # 5MB
MAX_RESULTS_CAP = 100
MAX_ATTACHMENTS = 10
MAX_BATCH_SIZE = 50
MAX_DOWNLOAD_SIZE = 25 * 1024 * 1024  # 25MB, overall per-URL download ceiling
# (outgoing attachment_urls only — unrelated to MAX_ATTACHMENT_SIZE below)
# Above this, an attachment is too big to base64-embed in a single create/send
# request (Graph's practical limit for that is a few MB) and must instead go
# through Graph's chunked upload-session API.
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024  # 3MB
# Cap for downloading an *existing* email attachment via download_attachment.
# Deliberately separate from MAX_DOWNLOAD_SIZE (which caps fetching external
# URLs for outgoing attachments) so raising one doesn't silently raise the
# other. Env-configurable since a deployment's downstream consumer (e.g.
# email-intake's own MinIO-backed attachment storage) may want a different
# ceiling than the default.
MAX_ATTACHMENT_SIZE = int(os.getenv("MAX_ATTACHMENT_DOWNLOAD_SIZE_MB", "25")) * 1024 * 1024  # 25MB
# Cap on the combined size of all attachment_urls in one create_draft/send_email
# call. Per-file/count limits alone don't stop the total from exceeding
# Exchange Online's own message-size ceiling (tenant-configured — commonly
# 25MB, up to 150MB on some plans); this catches an over-limit total with a
# clear error instead of letting Graph fail deep in the upload/send flow.
# 50MB assumes a tenant configured above the 25MB default; lower it to 25MB
# if unsure of the target tenant's actual ceiling.
MAX_TOTAL_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50MB
MAX_ID_LENGTH = 500

VALID_FOLDERS = {
    "inbox",
    "drafts",
    "sentitems",
    "deleteditems",
    "junkemail",
    "archive",
    "outbox",
}

# List versions for JSON Schema enum constraints
VALID_FOLDERS_LIST = [
    "inbox",
    "drafts",
    "sentitems",
    "deleteditems",
    "junkemail",
    "archive",
    "outbox",
]
VALID_IMPORTANCE_LIST = ["low", "normal", "high"]


def resolve_graph_scope(tool: Any, mailbox: Optional[str]) -> Tuple[Callable, str]:
    """(request_fn, base_path) for a mailbox-scoped Graph call. A shared mailbox
    (webhook-triggered flows - no signed-in user) routes app-only against
    /users/{mailbox}; omitted, it's the delegated /me path. Shared by get_email,
    list_attachments, download_attachment - all three need identical routing."""
    if mailbox:
        return tool._graph_request_app_only, f"/users/{mailbox.strip()}"
    return tool._graph_request, "/me"


def parse_allowed_mailboxes(credentials: Dict[str, str]) -> set:
    """Parse the connection's comma-separated allowed_mailboxes credential into
    a lowercased set. Shared by check_mailbox_allowed and
    list_mailbox_subscriptions's filtering."""
    return {
        addr.strip().lower()
        for addr in (credentials.get("allowed_mailboxes") or "").split(",")
        if addr.strip()
    }


def check_mailbox_allowed(credentials: Dict[str, str], mailbox: str) -> Optional[str]:
    """Authoritative allowlist check for app-only mailbox access. The credential
    (not the caller-supplied `mailbox` argument) is the source of truth - unlike
    SharePoint's site_id pattern (shared/microsoft_auth.py), where the argument
    falls back to the credential as a default, this REJECTS any mailbox not
    present in the connection's configured allowed_mailboxes. No allowlist
    configured means app-only access is disabled, not "allow everything"."""
    allowed = parse_allowed_mailboxes(credentials)
    if not allowed:
        return "This connection has no allowed_mailboxes configured; app-only mailbox access is disabled"
    if mailbox.strip().lower() not in allowed:
        return f"mailbox {mailbox!r} is not permitted for this connection"
    return None


_UNSAFE_STORAGE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_storage_key_component(value: str) -> str:
    """Collapse anything outside the filename-safe set to '_' so a MinIO
    object key built from a free-text attachment name never trips
    XMinioInvalidObjectName ('#', '(', ')', spaces, a trailing '.', etc.).
    Storage keys are sanitised the same way the platform's own email intake
    sanitises them, so a key produced here resolves there.
    byte-for-byte - this tool replaces that function's call site, so the same
    attachment must produce the same storage_key either way."""
    collapsed = _UNSAFE_STORAGE_KEY_CHARS.sub("_", value).strip("._")
    return collapsed or "attachment"


def dedupe_storage_key_component(value: str, occurrence: int) -> str:
    """Insert an ordinal suffix ('-2', '-3', ...) before the extension when
    the same sanitized name repeats within one email's attachment batch, so a
    second 'photo.jpg' doesn't overwrite the first. Mirrors apps/api's
    _dedupe_component byte-for-byte."""
    if occurrence == 1:
        return value
    stem, dot, ext = value.rpartition(".")
    if not dot:
        return f"{value}-{occurrence}"
    return f"{stem}-{occurrence}.{ext}"








def validate_id(value: str, label: str = "ID") -> Optional[str]:
    """Validate an ID used in URL paths. Returns error string or None on success."""
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if "/" in value or ".." in value:
        return f"{label} contains invalid characters"
    if len(value) > MAX_ID_LENGTH:
        return f"{label} is too long (max {MAX_ID_LENGTH} chars)"
    return None


def parse_recipients(
    raw: str,
    label: str = "email",
) -> Tuple[Optional[str], List[Dict]]:
    """Parse comma-separated emails into Graph API recipient list.

    Returns (error_message, recipients). error_message is None on success.
    """
    addresses = [a.strip() for a in raw.split(",") if a.strip()]
    if not addresses:
        return "No valid recipients provided", []
    for addr in addresses:
        if not EMAIL_RE.match(addr):
            return f"Invalid {label} address: {addr}", []
    return None, [{"emailAddress": {"address": a}} for a in addresses]


ATTACHMENT_FORMS_DOC = (
    "Files to attach (max 10). Three accepted forms per item: (1) a platform file_id "
    'UUID or storage key as a plain string, (2) {"file_id": "<uuid-or-key>"}, or '
    "(3) a pre-fetched {filename, content_base64, content_type}. Forms 1 and 2 are "
    "replaced with form 3 by the platform before this tool runs, so pass the "
    "identifier as-is: never put it in `filename`, and never send an empty "
    "`content_base64`. "
    "DO NOT ENCODE `content_base64` YOURSELF. Form 3 exists for bytes the platform "
    "has already fetched, not for base64 you write: a hand-encoded attachment "
    "arrives as a file that opens and is silently corrupt. If you hold no file_id "
    "or storage key, send the mail without an attachment and say so - do not "
    "improvise one, and do not retype data from the conversation into a file."
)


def attachments_schema() -> Dict[str, Any]:
    """JSON Schema for the `attachments` parameter.

    A fresh dict per call so the two tools that use it can never alias one
    mutable object.

    The string and ``file_id`` forms matter: an agent handed a platform file_id
    has to be able to express it, and a schema that only admitted the
    ``content_base64`` object pushed models into inventing one - putting the
    id in ``filename`` and sending ``content_base64: ""``. Resolution happens
    before validation, so by the time this schema is applied a resolved item is
    always form 3 anyway.
    """
    return {
        "type": "array",
        "items": {
            "anyOf": [
                {
                    "type": "string",
                    "description": "Platform file_id (UUID) or storage key",
                },
                {
                    "type": "object",
                    "properties": {"file_id": {"type": "string"}},
                    "required": ["file_id"],
                },
                {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "content_base64": {"type": "string"},
                        "content_type": {"type": "string"},
                    },
                    "required": ["content_base64"],
                },
            ]
        },
        "maxItems": MAX_ATTACHMENTS,
        "description": ATTACHMENT_FORMS_DOC,
    }


def normalize_base64(value: str) -> str:
    """Strip the wrappings producers commonly add around base64 payloads.

    ``validate=True`` rejects anything outside the base64 alphabet, which is
    the behaviour we want for genuine garbage but not for two shapes that
    legitimately reach us: MIME-style line wrapping (``base64.encodebytes``)
    and ``data:<mime>;base64,`` URIs. Normalizing here keeps the strict decode
    downstream instead of trading it for a permissive one.
    """
    text = value.strip()
    match = DATA_URI_RE.match(text)
    if match:
        text = text[match.end() :]
    return WHITESPACE_RE.sub("", text)


def _as_text(value: Any, fallback: str) -> str:
    """Return *value* as a non-empty string, falling back when it is neither.

    Callers reach this helper through a JSON schema that already types these
    fields, but ``build_inline_attachment`` is a public helper: a non-string
    filename used to raise TypeError out of the caller's ValueError handler
    and surface as a generic failure.
    """
    if isinstance(value, str) and value:
        return value
    return fallback


def build_inline_attachment(
    item: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    """Turn a pre-fetched {filename, content_base64, content_type} dict (e.g.
    from the platform's file_id resolver) into the same shapes download.py
    produces from a URL, so send_email can merge both sources with one
    size/count check.

    Returns (graph_attachment_or_none, large_attachment_or_none, info). Raises
    ValueError (binascii.Error is a subclass) when the item is an unresolved
    file reference, or when content_base64 is missing, doesn't decode, is
    empty, or exceeds the per-file download ceiling that attachment_urls
    already enforces.
    """
    # The schema advertises three item forms, but only the inline one carries
    # bytes: the file_id string and {"file_id": ...} shapes are meant to be
    # rewritten by the platform's resolver before the call arrives. Reaching
    # here with one un-rewritten means no resolver ran (a direct MCP call, or
    # a reference it declined to resolve), and there is nothing to attach.
    # Say so as a ValueError - the callers catch that and name the attachment,
    # whereas a str falling through to .get() raised AttributeError and
    # surfaced as a bare "Failed to send email".
    if not isinstance(item, dict) or ("content_base64" not in item and "file_id" in item):
        raise ValueError(
            "attachment is an unresolved file reference; it must arrive as "
            "{filename, content_base64, content_type}"
        )

    content_base64 = item.get("content_base64")
    if not isinstance(content_base64, str) or not content_base64:
        raise ValueError("attachment is missing content_base64")

    raw = base64.b64decode(normalize_base64(content_base64), validate=True)
    if not raw:
        raise ValueError("attachment is empty")
    if len(raw) > MAX_DOWNLOAD_SIZE:
        raise ValueError(f"attachment exceeds {MAX_DOWNLOAD_SIZE // (1024 * 1024)}MB limit")

    filename = re.sub(r"[^\w.\-]", "_", _as_text(item.get("filename"), "attachment"))
    content_type = _as_text(item.get("content_type"), "application/octet-stream")
    info = {"name": filename, "size_bytes": len(raw), "content_type": content_type}

    if len(raw) <= INLINE_ATTACHMENT_LIMIT:
        graph_attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentType": content_type,
            # Re-encode from the decoded bytes rather than forwarding the
            # caller's string: what Graph decodes then always matches the
            # len(raw) the size budget was charged for.
            "contentBytes": base64.b64encode(raw).decode("ascii"),
        }
        return graph_attachment, None, info

    return None, {"filename": filename, "content": raw, "content_type": content_type}, info
