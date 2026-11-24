"""The outlook-email connector.

Ported from the v1 mixin form by a codemod.

28 tools, 15 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

SERVER_NAME = "outlook-email"
SERVER_VERSION = "2.0.0"

# One pattern across connectors: the connector, its version, and where to read
# about it, in the `Mozilla/5.0 (compatible; ...)` form crawlers use, which many
# hosts accept where they refuse an unfamiliar string.
USER_AGENT = (
    f"Mozilla/5.0 (compatible; magoneai-{SERVER_NAME}/{SERVER_VERSION}; +https://github.com/magurelabs/magoneai-mcp)"
)

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

# send_email, create_draft and reply_email accept up to 10 attachments and
# 50 MB per call, which base64 turns into roughly 67 MB of request body. The
# framework's 10 MiB default refused those calls before any tool ran. v1 ran
# this connector at 75 MB. MCP_MAX_BODY_BYTES still overrides it.
MAX_REQUEST_BODY_BYTES = 75 * 1024 * 1024

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
    max_body_bytes=MAX_REQUEST_BODY_BYTES,
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

from .tools._constants import CREDENTIAL_HEADERS
from typing import Optional
from .tools._constants import MAX_TOTAL_ATTACHMENT_SIZE
import base64
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
import io
import zipfile
from typing import List
from typing import Any, Dict, Optional, Tuple
from .tools._constants import MAX_BODY_SIZE, MAX_SUBJECT_LEN, VALID_IMPORTANCE_LIST
from typing import Any, Dict, List, Optional
import httpx
from .tools._send import IMMUTABLE_ID_PREFER
import binascii
from typing import Any, Dict, List, Optional, Tuple
from .tools._budget import AttachmentBudgetExceeded, SizeBudget
from .tools._constants import MAX_ATTACHMENTS
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional
# `build_minio_client_from_env` and `get_attachments_bucket` moved to the private
# overlay with `fetch_and_store_attachments`; importing them here was residue that
# stopped this module importing at all.
from .tools._constants import CREDENTIAL_HEADERS, EMAIL_RE, MAX_ATTACHMENT_SIZE
from urllib.parse import urlparse
from .tools._budget import SizeBudget
from .tools.download import download_attachments
from .tools._constants import INLINE_ATTACHMENT_LIMIT, MAX_DOWNLOAD_SIZE
from .tools._constants import CREDENTIAL_HEADERS, MAX_ATTACHMENTS, MAX_BODY_SIZE, MAX_RESULTS_CAP, MAX_SUBJECT_LEN, VALID_IMPORTANCE_LIST
from typing import Any, Dict
from .tools._constants import CREDENTIAL_HEADERS, MAX_RESULTS_CAP, VALID_FOLDERS, VALID_FOLDERS_LIST
from typing import Any, Dict, List
from .tools._constants import CREDENTIAL_HEADERS, MAX_BATCH_SIZE
import html as _html_module
from .tools._constants import CREDENTIAL_HEADERS, EMAIL_RE, MAX_RESULTS_CAP, VALID_FOLDERS, VALID_FOLDERS_LIST
from .tools._constants import CREDENTIAL_HEADERS, MAX_BODY_SIZE
from .tools._constants import CREDENTIAL_HEADERS, INLINE_ATTACHMENT_LIMIT, MAX_ATTACHMENTS, VALID_IMPORTANCE_LIST
from datetime import datetime, timedelta, timezone
from datetime import timezone as _dt_timezone
from ._shared.microsoft_graph import (
    GraphAuthError,
    GraphNotFoundError,
    auth_reason,
    graph_failure,
    graph_reason,
)
from .tools._constants import CREDENTIAL_HEADERS, EMAIL_RE
import time






CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
    "X-Allowed-Mailboxes",
]

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

DATA_URI_RE = re.compile(r"^data:[\w.+-]*/?[\w.+-]*(;[\w.+-]+=[\w.+-]+)*;base64,", re.IGNORECASE)

WHITESPACE_RE = re.compile(r"\s+")

MAX_SUBJECT_LEN = 998

MAX_BODY_SIZE = 5 * 1024 * 1024  # 5MB

MAX_RESULTS_CAP = 100

MAX_ATTACHMENTS = 10

MAX_BATCH_SIZE = 50

MAX_DOWNLOAD_SIZE = 25 * 1024 * 1024  # 25MB, overall per-URL download ceiling

INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024  # 3MB

MAX_ATTACHMENT_SIZE = int(os.getenv("MAX_ATTACHMENT_DOWNLOAD_SIZE_MB", "25")) * 1024 * 1024  # 25MB

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
    /users/{mailbox}; omitted, it's the delegated /me path. Shared by the
    mailbox-taking read tools, set_email_categories and reply_email (both its
    plain path and reply_via_draft's attachment path), which all need identical
    routing. The two request functions differ in one way a caller must respect:
    only the delegated one takes `access_token`, because the app-only one
    manages its own token."""
    if mailbox:
        return tool._graph_request_app_only, f"/users/{mailbox.strip()}"
    return tool._graph_request, "/me"


# `resolve_graph_scope` takes the object that owns `_graph_request` and
# `_graph_request_app_only`. In v1 that was the server mixin instance, so the
# bodies passed `self`. The port flattened those methods to module level and
# left the `self`, which resolves to nothing - a NameError on call, returned
# to the caller as {"success": false, "error": "..."} by the surrounding
# `except Exception`. The module itself now owns those two names, so it is
# what plays the role the instance used to.
_GRAPH_SCOPE_OWNER = sys.modules[__name__]

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

DEFAULT_ATTACHMENTS_BUCKET = "email-attachments"



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

MAX_TEXT_CHARS = 12000

_MAX_PDF_PAGES = 50

_MAX_XLSX_ROWS = 200

_MAX_ZIP_ENTRIES = 200

_MAX_ZIP_TEXT_MEMBERS = 10

_MAX_ZIP_MEMBER_BYTES = 5 * 1024 * 1024

_MAX_ZIP_TOTAL_BYTES = 20 * 1024 * 1024

_TEXT_EXTS = (
    ".txt",
    ".csv",
    ".md",
    ".json",
    ".html",
    ".htm",
    ".xml",
    ".log",
    ".yaml",
    ".yml",
)

_OFFICE_EXTS = (".docx", ".xlsx", ".pptx")

def _truncate(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS] + f"\n...[truncated; {len(text)} chars total]"
    return text

def _extract_pdf(content_bytes: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(content_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        if i >= _MAX_PDF_PAGES:
            pages.append("...[more pages]")
            break
        pages.append(page.extract_text() or "")
    return _truncate("\n".join(pages)) or "(no extractable text in PDF)"

def _extract_docx(content_bytes: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(content_bytes))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return _truncate("\n".join(parts)) or "(empty Word document)"

def _extract_xlsx(content_bytes: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"# Sheet: {ws.title}")
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= _MAX_XLSX_ROWS:
                out.append("...[more rows]")
                break
            out.append(", ".join("" if c is None else str(c) for c in row))
    return _truncate("\n".join(out)) or "(empty spreadsheet)"

def _zip_members(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """Real files in the archive, in listing order.

    Directories carry no content, and ``__MACOSX`` holds the resource forks a
    Mac adds when zipping from Finder. Listing either of them tells the agent
    nothing and pushes the real files past the entry cap.
    """
    members = []
    for zi in zf.infolist():
        if zi.is_dir() or zi.filename.startswith("__MACOSX/"):
            continue
        if zi.filename.rsplit("/", 1)[-1].startswith("."):
            continue
        members.append(zi)
        if len(members) >= _MAX_ZIP_ENTRIES:
            break
    return members

def _extract_zip(filename: str, content_bytes: bytes) -> str:
    """List an archive, then read the text out of as many members as the caps allow.

    The listing alone is worth returning: "what is in this zip" is a question the
    agent could not answer at all before, and a manifest is enough to decide
    whether the archive is even the right one. Reading the members is the bonus.

    Nested archives are listed but never opened. Recursing is how a zip bomb
    turns a 3MB attachment into an out-of-memory kill, and a zip inside a zip is
    rare enough in mail that refusing it costs nothing.
    """
    with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
        members = _zip_members(zf)
        if not members:
            return f"(archive '{filename}' is empty)"

        lines = [f"# Archive: {filename} ({len(members)} file(s))"]
        for zi in members:
            lines.append(f"- {zi.filename} ({zi.file_size:,} bytes)")

        budget = _MAX_ZIP_TOTAL_BYTES
        read = 0
        for zi in members:
            if read >= _MAX_ZIP_TEXT_MEMBERS:
                lines.append(f"\n...[{len(members) - read} more file(s) not read]")
                break
            name = zi.filename.lower()
            if name.endswith(".zip"):
                lines.append(f"\n## {zi.filename}\n(nested archive; not opened)")
                continue
            if not name.endswith(_TEXT_EXTS + _OFFICE_EXTS + (".pdf",)):
                continue
            if zi.file_size > _MAX_ZIP_MEMBER_BYTES or zi.file_size > budget:
                lines.append(f"\n## {zi.filename}\n(too large to read: {zi.file_size:,} bytes)")
                continue
            budget -= zi.file_size
            read += 1
            inner = extract_text(zi.filename, zf.read(zi), _depth=1)
            lines.append(f"\n## {zi.filename}\n{inner}")

        return _truncate("\n".join(lines))

def extract_text(
    filename: str, content_bytes: bytes, content_type: str = "", _depth: int = 0
) -> str:
    """Extract readable text from attachment bytes. Best-effort, never raises.

    ``_depth`` is internal: it is 1 for a file read out of an archive, which is
    what stops archive extraction recursing.
    """
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    try:
        if name.endswith(".pdf") or "pdf" in ct:
            return _extract_pdf(content_bytes)

        if name.endswith(".docx") or "wordprocessingml" in ct:
            return _extract_docx(content_bytes)

        if name.endswith(".xlsx") or "spreadsheetml" in ct:
            return _extract_xlsx(content_bytes)

        if name.endswith(_TEXT_EXTS) or ct.startswith("text/") or "json" in ct or "xml" in ct:
            return _truncate(content_bytes.decode("utf-8", errors="replace"))

        # Archives last: every Office format above is also a ZIP, so this branch
        # must only see what none of them claimed.
        if _depth == 0 and (name.endswith(".zip") or "zip" in ct or "compressed" in ct):
            return _extract_zip(filename, content_bytes)
    except zipfile.BadZipFile:
        return f"(attachment '{filename}' is not a readable archive)"
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        return f"(could not extract text from '{filename}': {type(exc).__name__})"

    return f"(attachment '{filename}' is type '{content_type or 'unknown'}'; not text-extractable)"

def validate_send_inputs(subject: str, body: str, importance: str) -> Optional[str]:
    """Return an error message if a scalar field is invalid, else None."""
    if len(subject) > MAX_SUBJECT_LEN:
        return f"Subject exceeds {MAX_SUBJECT_LEN} characters"
    if len(body) > MAX_BODY_SIZE:
        return "Body exceeds 5MB limit"
    if importance not in VALID_IMPORTANCE_LIST:
        return "Importance must be low, normal, or high"
    return None

def build_message_body(fields: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Build the Graph message body from validated fields, parsing recipients.

    `fields` carries to/subject/body/is_html/importance and optional cc/bcc.
    Returns (error_message, message_body); error is set (and body None) if any
    recipient list is invalid.
    """
    err, to_recipients = parse_recipients(fields["to"], "email")
    if err:
        return err, None

    message_body: Dict[str, Any] = {
        "subject": fields["subject"],
        "body": {
            "contentType": "HTML" if fields["is_html"] else "Text",
            "content": fields["body"],
        },
        "toRecipients": to_recipients,
        "importance": fields["importance"],
    }

    if fields.get("cc"):
        err, cc_recipients = parse_recipients(fields["cc"], "CC")
        if err:
            return err, None
        message_body["ccRecipients"] = cc_recipients

    if fields.get("bcc"):
        err, bcc_recipients = parse_recipients(fields["bcc"], "BCC")
        if err:
            return err, None
        message_body["bccRecipients"] = bcc_recipients

    return None, message_body

async def reply_via_draft(
    graph: Any,
    credentials: Dict[str, str],
    email_id: str,
    reply_all: bool,
    message_patch: Dict[str, Any],
    inline_attachments: List[Dict[str, Any]],
    large_attachments: List[Dict[str, Any]],
    mailbox: Optional[str] = None,
) -> Optional[str]:
    """Create a reply draft, attach, send. Returns the sent message id.

    Attachments are POSTed to the draft one at a time rather than PATCHed onto
    it: Graph does not accept an `attachments` collection on a message PATCH,
    it only exposes the child /attachments navigation property. A PATCH that
    includes them is accepted and silently drops them, which is the same class
    of failure this whole function exists to fix.

    Any failure deletes the half-built draft, so a failed reply never leaves an
    orphan sitting in the user's Drafts folder.

    Pass `mailbox` (an address) to run the whole sequence, including the
    large-attachment upload and the cleanup, against a shared mailbox via
    app-only auth (`/users/{mailbox}/...`) instead of the signed-in user's
    `/me`. A reply triggered by a change notification has no signed-in user.
    The caller has already checked `mailbox` against the allowlist.
    """
    request_fn, base_path = resolve_graph_scope(graph, mailbox)
    is_shared = bool(mailbox)
    access_token = None if is_shared else await graph._get_access_token(credentials)

    async def _request(
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        prefer: Optional[str] = None,
    ) -> Dict[str, Any]:
        if is_shared:
            return await request_fn(credentials, method, path, json_data=json_data, prefer=prefer)
        return await request_fn(
            credentials, method, path, json_data=json_data, access_token=access_token, prefer=prefer
        )

    action = "createReplyAll" if reply_all else "createReply"
    draft_id: Optional[str] = None
    try:
        created = await _request(
            "POST", f"{base_path}/messages/{email_id}/{action}", prefer=IMMUTABLE_ID_PREFER
        )
        draft_id = created.get("id")
        if not draft_id:
            raise ValueError("createReply returned no draft id")

        if message_patch:
            await _request("PATCH", f"{base_path}/messages/{draft_id}", json_data=message_patch)

        for att in inline_attachments:
            await _request("POST", f"{base_path}/messages/{draft_id}/attachments", json_data=att)

        for large in large_attachments:
            await graph._upload_large_attachment(
                credentials,
                draft_id,
                large["filename"],
                large["content"],
                large["content_type"],
                access_token=access_token,
                request_fn=request_fn if is_shared else None,
                base_path=base_path,
            )

        await _request("POST", f"{base_path}/messages/{draft_id}/send")
        return draft_id
    except (ValueError, httpx.HTTPError):
        if draft_id:
            await _cleanup_draft(
                graph,
                credentials,
                draft_id,
                request_fn=request_fn if is_shared else None,
                base_path=base_path,
            )
        raise

IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'

async def gather_attachments(
    attachment_urls: Optional[List[str]],
    attachment_filename: Optional[str],
    attachments: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Merge attachment_urls (fetched here) and attachments (pre-fetched
    base64, e.g. from the platform's file_id resolver) into one Graph-ready
    result, sharing a single count/size budget across both.

    The budget is charged as bytes arrive, not tallied at the end, so an
    over-limit batch aborts before the rest of it is resident.

    Returns (inline_attachments, large_attachments, attachments_info, error).
    On error the first three are empty and the caller should return it as-is.
    """
    if (len(attachment_urls or []) + len(attachments or [])) > MAX_ATTACHMENTS:
        return [], [], [], f"Too many attachments, max {MAX_ATTACHMENTS}"

    inline: List[Dict[str, Any]] = []
    large: List[Dict[str, Any]] = []
    info: List[Dict[str, Any]] = []
    budget = SizeBudget()

    if attachment_urls:
        try:
            url_inline, url_large, url_info = await download_attachments(
                attachment_urls, attachment_filename, budget
            )
        except AttachmentBudgetExceeded as e:
            return [], [], [], str(e)
        except (ValueError, httpx.HTTPError) as e:
            return [], [], [], f"Failed to download attachment: {e}"
        inline += url_inline
        large += url_large
        info += url_info

    for position, item in enumerate(attachments or [], start=1):
        label = _attachment_label(item, position)
        try:
            graph_attachment, large_attachment, item_info = build_inline_attachment(item)
            budget.take(item_info["size_bytes"])
        except AttachmentBudgetExceeded as e:
            return [], [], [], str(e)
        except (binascii.Error, ValueError) as e:
            return [], [], [], f"Invalid attachment ({label}): {e}"
        info.append(item_info)
        if graph_attachment is not None:
            inline.append(graph_attachment)
        if large_attachment is not None:
            large.append(large_attachment)

    return inline, large, info, None

def _attachment_label(item: Any, position: int) -> str:
    """Name an attachment for error messages, by filename when it has a usable
    one and by position otherwise - with several attachments in a call, "one of
    them failed" is not actionable."""
    if isinstance(item, str) and item:
        return item[:80]
    if isinstance(item, dict):
        for key in ("filename", "file_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value[:80]
    return f"#{position}"

async def _cleanup_draft(
    graph: Any,
    credentials: Dict[str, str],
    draft_id: str,
    request_fn: Optional[Callable] = None,
    base_path: str = "/me",
) -> None:
    """Best-effort delete of an unsent draft after a failed send.

    `request_fn` and `base_path` route the delete to a shared mailbox via
    app-only auth instead of the default delegated `/me`; reply_via_draft is
    the caller that passes them."""
    try:
        request = request_fn or graph._graph_request
        await request(credentials, "DELETE", f"{base_path}/messages/{draft_id}")
    except Exception:
        graph.logger.warning("send_email_orphan_draft_cleanup_failed", draft_id=draft_id)

async def send_via_sendmail(
    graph: Any,
    credentials: Dict[str, str],
    message_body: Dict[str, Any],
) -> Optional[str]:
    """Send via /me/sendMail (used when save_to_sent is False).

    Graph returns 202 with no body, so no message id is available; returns
    None. saveToSentItems is False here by construction.
    """
    await graph._graph_request(
        credentials,
        "POST",
        "/me/sendMail",
        json_data={"message": message_body, "saveToSentItems": False},
    )
    return None

async def send_via_draft(
    graph: Any,
    credentials: Dict[str, str],
    message_body: Dict[str, Any],
    large_attachments: List[Dict[str, Any]],
) -> Optional[str]:
    """Create a draft (requesting an immutable id), stream any large
    attachments into it, then send it. Returns the immutable message id.

    On any failure the partially-built draft is deleted before re-raising, so
    no orphan draft is left behind. Sending a draft always lands it in Sent
    Items, so this path is only used when save_to_sent is True.
    """
    # Fetch the token once and reuse it across create + upload(s) + send
    # instead of refreshing on each call.
    access_token = await graph._get_access_token(credentials)
    draft_id: Optional[str] = None
    try:
        created = await graph._graph_request(
            credentials,
            "POST",
            "/me/messages",
            json_data=message_body,
            access_token=access_token,
            prefer=IMMUTABLE_ID_PREFER,
        )
        draft_id = created.get("id")
        for large in large_attachments:
            await graph._upload_large_attachment(
                credentials,
                draft_id,
                large["filename"],
                large["content"],
                large["content_type"],
                access_token=access_token,
            )
        await graph._graph_request(
            credentials,
            "POST",
            f"/me/messages/{draft_id}/send",
            access_token=access_token,
        )
        return draft_id
    except (ValueError, httpx.HTTPError):
        if draft_id:
            await _cleanup_draft(graph, credentials, draft_id)
        raise

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

_WHITESPACE_RE = re.compile(r"[ \t]+")

_NEWLINE_RE = re.compile(r"\n{3,}")

_QUOTE_MARKER_RE = re.compile(r"[\u2502\u2503\u258E\u258F\u2580-\u259F]")

# The line a mail client inserts directly above a quoted previous message -
# whichever comes first marks where THIS message's own new content ends. Covers
# Gmail/Apple Mail/Thunderbird ("On <date>, <name> wrote:") and Outlook's
# classic separator ("-----Original Message-----", any dash count/spacing).
# Confirmed live (Sep 2026): a Gmail reply's Graph body carried the ENTIRE prior
# thread, compounding with every reply, because nothing stripped past this line.
#
# The "On ... wrote:" alternative anchors "wrote:" to end-of-line: the real
# header always ends the line, and without that anchor the pattern matches
# ordinary prose containing "wrote:" anywhere in the next 160 chars - e.g. "On
# the call yesterday, the underwriter wrote: approve it." - which is not a quote
# header at all. The trailing "\r?" before each "$" matters for the same reason:
# Python's MULTILINE "$" only matches right before "\n", so a CRLF plain-text
# body (raw Graph "text" bodies commonly use "\r\n") leaves a stray "\r" the
# anchor cannot otherwise consume, and the header line silently fails to match.
_QUOTE_HEADER_RE = re.compile(
    r"^[ \t]*(?:On .{0,160}?wrote:[ \t]*\r?$|-{2,}\s*Original Message\s*-{2,}[ \t]*\r?$)",
    re.IGNORECASE | re.MULTILINE,
)
# A classic plain-text quote block: 3+ consecutive lines starting with '>' (RFC
# 3676 style - Apple Mail, mutt, plain-text Outlook). Single stray '>' characters
# in real content are common enough (arrows, code) that one alone is not treated
# as quoting; a run of exactly 2 is also common as ordinary content (a short
# arrow list), so only 3+ in a row is treated as an actual quote block.
_QUOTE_BLOCK_RE = re.compile(r"^[ \t]*>.*(?:\n[ \t]*>.*){2,}", re.MULTILINE)

# Signals that what follows a quote header is a FORWARDED message rather than a
# reply's quoted trail - the quoted content there IS the payload the caller is
# after (e.g. a customer forwarding claim documents), so truncating it would
# throw away exactly what an email-intake pipeline needs. Covers Gmail
# ("---------- Forwarded message ---------"), Apple Mail ("Begin forwarded
# message:"), and the From:/Subject: header block Outlook prepends to both a
# forwarded body AND a reply - Outlook gives both the identical
# "-----Original Message-----" + From:/Sent:/To:/Subject: shape, so the header
# block alone cannot tell them apart. The Subject: prefix is the only signal
# that can: a reply's Subject starts with "RE:" (excluded here so it falls
# through to stripping), while a forward's starts with "FW:"/"Fwd:" or, for a
# manually forwarded message, carries no prefix at all - both of the latter
# still match here so a genuine forward's payload is never truncated.
_FORWARD_MARKER_RE = re.compile(
    r"^[ \t]*-{2,}\s*Forwarded message\s*-{2,}[ \t]*\r?$"
    r"|^[ \t]*Begin forwarded message:[ \t]*\r?$"
    r"|^[ \t]*From:[ \t]*\S.*\n(?:[ \t]*(?:Sent|Date|To|Cc):.*\n){0,4}"
    r"[ \t]*Subject:(?![ \t]*RE[ \t]*:)",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_quoted_reply(text: str) -> str:
    """Truncate at the start of a quoted previous message, leaving only this
    message's own new content. Tries the header-line markers first since they are
    unambiguous; only checks for a leading '>' quote block if no header line was
    found. Fails open - text with neither pattern (nothing being quoted, or a
    client/format this does not recognise) is returned unchanged rather than
    risking truncating real content on a false positive. Also fails open when a
    forward marker follows the match (the "quoted" region is a forward's payload,
    not a reply trail) or when truncating would leave nothing at all (a
    bottom-posted reply, or a body that is entirely quoted) - either of those is
    worse than leaving the quote in."""
    match = _QUOTE_HEADER_RE.search(text) or _QUOTE_BLOCK_RE.search(text)
    if not match:
        return text
    if _FORWARD_MARKER_RE.search(text, match.start()):
        return text
    stripped = text[: match.start()].rstrip()
    return stripped if stripped else text

_ME_TOKEN_RE = re.compile(r"\b(from|to|cc|bcc):(?:\(me\)|me\b)", re.IGNORECASE)

_BRACKET_PLACEHOLDER_RE = re.compile(r"\[[^\]]*\]")

_NAME_QUALIFIER_RE = re.compile(r"\b(?:from|to|cc|bcc):(?!\S*@)(\S+)", re.IGNORECASE)

# --- Timezone-aware date formatting ---------------------------------------
# Graph returns receivedDateTime in UTC (e.g. "2026-09-22T13:11:00Z"). It is
# rendered in the caller's IANA timezone when one is given; when that is missing
# OR unrecognised it falls back to UTC, which is never worse than the previous
# behaviour of slicing the raw UTC string.
#
# `timezone` is imported from datetime at module level and the seven tools below
# take a parameter of the same name, so these helpers use an alias. Shadowing is
# currently harmless - no target function uses datetime's `timezone` - but
# relying on that would make an ordinary edit inside one of them break dates.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except Exception:  # pragma: no cover - stdlib since 3.9, guarded for odd builds
    _ZoneInfo = None  # type: ignore[assignment]


def _tz_or_utc(tz):
    """The tzinfo for an IANA name; UTC when missing or unrecognised."""
    name = (tz or "").strip()
    if not name or _ZoneInfo is None:
        return _dt_timezone.utc
    try:
        return _ZoneInfo(name)
    except Exception:
        return _dt_timezone.utc


def _fmt_date(received, tz=None, date_only=False):
    """Format Graph receivedDateTime (UTC) in the caller's tz; UTC fallback.

    Fails open to the old raw-slice behaviour on any parse error, so a shape
    Graph has not returned before degrades instead of raising.
    """
    if not received:
        return ""
    try:
        dt = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt_timezone.utc)
        local = dt.astimezone(_tz_or_utc(tz))
        return local.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")
    except Exception:
        raw = str(received)
        return raw[:10] if date_only else raw[:16].replace("T", " ")


def _strip_html(html: str) -> str:
    """Strip HTML tags, decode entities, and clean to readable plain text."""
    # 1. Remove script/style blocks entirely
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    # 2. Convert <br>, </p>, </div>, </li> to newlines before stripping tags
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    # 3. Strip remaining HTML tags
    text = _HTML_TAG_RE.sub(" ", text)
    # 4. Decode HTML entities (&nbsp;, &amp;, &quot;, etc.)
    text = _html_module.unescape(text)
    # 5. Replace non-breaking space with regular space
    text = text.replace("\xa0", " ")
    # 6. Remove quoted-line markers
    text = _QUOTE_MARKER_RE.sub("", text)
    # 7. Collapse horizontal whitespace
    text = _WHITESPACE_RE.sub(" ", text)
    # 8. Normalize line breaks
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    # 9. Collapse 3+ newlines to 2
    text = _NEWLINE_RE.sub("\n\n", text)
    return text.strip()

def _fmt_addr(email_address: dict) -> str:
    """Format an emailAddress object as 'Name <email>' or just 'email'."""
    ea = email_address.get("emailAddress", {})
    name = ea.get("name", "")
    addr = ea.get("address", "")
    if name and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return addr

def _fmt_addr_list(recipients: list) -> str:
    """Format a list of recipients as a comma-separated string."""
    return ", ".join(_fmt_addr(r) for r in (recipients or []))

def _header_value(result: dict, header_name: str) -> Optional[str]:
    """Pull one RFC 5322 header's raw value out of Graph's internetMessageHeaders
    array ([{"name": ..., "value": ...}, ...], only present when $select asks for
    it). Header names are matched case-insensitively - Graph itself is consistent,
    but this is defensive against any relay that isn't."""
    headers = result.get("internetMessageHeaders") or []
    for h in headers:
        if (h.get("name") or "").lower() == header_name.lower():
            return h.get("value")
    return None

_DEFAULT_EXPIRATION_HOURS = 70

_MIN_EXPIRATION_HOURS = 1

_MAX_EXPIRATION_HOURS = 70

def _expiration_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

def _validate_expiration_hours(hours: Any) -> Optional[str]:
    if not isinstance(hours, int) or isinstance(hours, bool):
        return "expiration_hours must be an integer"
    if not (_MIN_EXPIRATION_HOURS <= hours <= _MAX_EXPIRATION_HOURS):
        return (
            f"expiration_hours must be between {_MIN_EXPIRATION_HOURS} and {_MAX_EXPIRATION_HOURS}"
        )
    return None

def _mailbox_from_resource(resource: str) -> str:
    """Extract the mailbox address from a Graph subscription's `resource`
    string, e.g. "users/foo@bar.com/mailFolders('inbox')/messages" -> "foo@bar.com"."""
    parts = resource.split("/")
    return parts[1].lower() if len(parts) > 1 and parts[0] == "users" else ""

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

GRAPH_API_URL = "https://graph.microsoft.com/v1.0"

_ALLOWED_GRAPH_HOSTS = frozenset({"graph.microsoft.com"})

_APP_ONLY_REFRESH_LEEWAY = 60  # seconds

# App-only access tokens by (tenant_id, client_id), refreshed under one lock.
# v1 held both as class attributes of `MicrosoftGraph`; the port flattened the
# methods and left the attributes behind, so every shared-mailbox call and
# all four subscription tools raised NameError on their first call.
_app_only_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
_app_only_token_lock = asyncio.Lock()

_MAX_THROTTLE_RETRIES = 5

_MAX_BACKOFF_SECONDS = 60.0

_UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024

def _throttle_delay_seconds(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429/503: honor Retry-After when present,
    else exponential backoff (2**attempt) capped at _MAX_BACKOFF_SECONDS."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(int(retry_after)), _MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    return min(2.0**attempt, _MAX_BACKOFF_SECONDS)

async def _get_access_token(credentials: Dict[str, str]) -> str:
    """Exchange refresh token for access token."""
    refresh_token = credentials.get("microsoft_refresh_token")
    if not refresh_token:
        raise GraphAuthError("Missing X-Microsoft-Refresh-Token header")

    client_id = credentials.get("microsoft_client_id")
    client_secret = credentials.get("microsoft_client_secret")
    tenant_id = credentials.get("microsoft_tenant_id") or "common"

    if not client_id or not client_secret:
        raise GraphAuthError(
            "Missing OAuth credentials. Pass client_id and client_secret via headers."
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL.format(tenant=tenant_id),
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "https://graph.microsoft.com/.default offline_access",
            },
        )

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except Exception:
                error_data = {}
            error_desc = auth_reason(error_data.get("error_description"))
            raise GraphAuthError(f"Token refresh failed ({response.status_code}): {error_desc}")

        token_data = response.json()
        if "access_token" not in token_data:
            raise ValueError("Token endpoint returned unexpected response (no access_token)")
        return token_data["access_token"]

async def _graph_request(credentials: Dict[str, str],
    method: str,
    endpoint: str,
    json_data: Optional[Dict] = None,
    params: Optional[Dict] = None,
    access_token: Optional[str] = None,
    prefer: Optional[str] = None,
) -> Dict[str, Any]:
    """Make authenticated request to Microsoft Graph API.

    Pass `access_token` to reuse one already fetched (e.g. across several
    calls in the same request) instead of refreshing it per call.
    Pass `prefer` to add a Graph `Prefer` header value, e.g.
    IdType="ImmutableId" so a created message's id survives folder moves."""
    if access_token is None:
        access_token = await _get_access_token(credentials)

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Collect Prefer values: $search needs the non-indexed-query hint, and
    # callers may request more (e.g. IdType="ImmutableId" on message create).
    prefer_values: List[str] = []
    if params and "$search" in params:
        headers["ConsistencyLevel"] = "eventual"
        prefer_values.append("HonorNonIndexedQueriesWarning=true")
    if prefer:
        prefer_values.append(prefer)
    if prefer_values:
        headers["Prefer"] = ", ".join(prefer_values)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method=method,
            url=f"{GRAPH_API_URL}{endpoint}",
            headers=headers,
            json=json_data,
            params=params,
        )

        if response.status_code == 401:
            raise GraphAuthError("Authentication failed. Please re-authenticate.")
        elif response.status_code == 403:
            raise GraphAuthError(
                "Access denied. The connected account lacks the permission or "
                "consent this operation needs (e.g. Mail.Read or Mail.Send)."
            )
        elif response.status_code == 404:
            raise GraphNotFoundError("Resource not found.")
        elif response.status_code == 429:
            raise ValueError("Rate limit exceeded. Try again later.")
        elif response.status_code >= 400:
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            error_msg = graph_reason(error_data)
            raise ValueError(f"Graph API error ({response.status_code}): {error_msg}")

        if response.status_code in (204, 202):
            return {}
        return response.json()

async def _upload_large_attachment(credentials: Dict[str, str],
    message_id: str,
    filename: str,
    content: bytes,
    content_type: str,
    access_token: Optional[str] = None,
    request_fn: Optional[Callable] = None,
    base_path: str = "/me",
) -> Dict[str, Any]:
    """Attach a file too large to base64-inline into a create/send request.

    Streams it into an existing draft message via Graph's chunked
    upload-session API (supports up to 150MB, vs. a few MB inline).

    Pass `access_token` to reuse one already fetched — useful when
    uploading several large attachments in one call, so each doesn't
    trigger its own token refresh.

    Pass `request_fn` and `base_path` to create the upload session in a shared
    mailbox via app-only auth (`/users/{mailbox}`) instead of the delegated
    `/me`. `access_token` is not passed in that case: the app-only request
    manages its own token."""
    create_kwargs: Dict[str, Any] = {
        "json_data": {
            "AttachmentItem": {
                "attachmentType": "file",
                "name": filename,
                "size": len(content),
                "contentType": content_type,
            }
        }
    }
    if request_fn is None:
        request_fn = _graph_request
        create_kwargs["access_token"] = access_token
    session = await request_fn(
        credentials,
        "POST",
        f"{base_path}/messages/{message_id}/attachments/createUploadSession",
        **create_kwargs,
    )
    upload_url = session["uploadUrl"]
    total = len(content)

    # The upload URL is pre-authorized by Graph; it must NOT receive our
    # bearer token, so this uses a bare httpx client, not _graph_request.
    async with httpx.AsyncClient(timeout=60.0) as client:
        attachment: Dict[str, Any] = {}
        for start in range(0, total, _UPLOAD_CHUNK_SIZE):
            end = min(start + _UPLOAD_CHUNK_SIZE, total) - 1
            chunk = content[start : end + 1]
            response = await client.put(
                upload_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
                content=chunk,
            )
            response.raise_for_status()
            # Graph completes an attachment upload session with 201 Created
            # and an empty body (just a Location header). Only parse when a
            # body is actually present, otherwise response.json() raises
            # JSONDecodeError on the empty completion response.
            if response.status_code == 201 and response.content:
                attachment = response.json()
        return attachment

async def _get_app_only_token(credentials: Dict[str, str]) -> str:
    """Acquire an app-only access token via client credentials grant.

    Used for service-to-service flows (e.g., the scheduled SharePoint
    sync use case) where no user is in the loop. Requires an Azure AD
    app with admin-consented application permissions on the target
    Graph scopes (e.g., Sites.Read.All, Files.Read.All).

    Tokens are cached per (tenant_id, client_id) until ~1 minute before
    their reported expiry, so a multi-thousand-call sync does not hammer
    Azure AD's token endpoint and trigger throttling.
    """
    client_id = credentials.get("microsoft_client_id")
    client_secret = credentials.get("microsoft_client_secret")
    tenant_id = credentials.get("microsoft_tenant_id")

    if not client_id or not client_secret:
        raise GraphAuthError(
            "Missing app-only credentials. Pass client_id and client_secret via headers."
        )
    if not tenant_id:
        raise GraphAuthError("Missing tenant_id. App-only OAuth requires an explicit tenant id.")

    cache_key = (tenant_id, client_id)
    now = time.monotonic()

    # Fast path: cache hit.
    cached = _app_only_token_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    # Slow path: refresh under the shared lock so concurrent callers collapse.
    async with _app_only_token_lock:
        # Re-check under the lock — another coroutine may have refreshed.
        cached = _app_only_token_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL.format(tenant=tenant_id),
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )

            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {}
                error_desc = auth_reason(error_data.get("error_description"))
                raise GraphAuthError(
                    f"App-only token acquisition failed ({response.status_code}): {error_desc}"
                )

            token_data = response.json()
            if "access_token" not in token_data:
                raise ValueError(
                    "Token endpoint returned unexpected response (no access_token)."
                )

            token = token_data["access_token"]
            # `expires_in` is seconds-from-now per OAuth2. Default to 3600
            # if Azure ever omits it (shouldn't, but be defensive). Refresh
            # _APP_ONLY_REFRESH_LEEWAY seconds early so we never present an
            # expired token mid-flight.
            expires_in = int(token_data.get("expires_in", 3600))
            expires_at = time.monotonic() + max(0, expires_in - _APP_ONLY_REFRESH_LEEWAY)
            _app_only_token_cache[cache_key] = (token, expires_at)
            return token

async def _graph_request_app_only(credentials: Dict[str, str],
    method: str,
    endpoint: str,
    json_data: Optional[Dict] = None,
    params: Optional[Dict] = None,
    prefer: Optional[str] = None,
) -> Dict[str, Any]:
    """Make a Graph API request using app-only OAuth.

    Pass `prefer` to add a Graph `Prefer` header value, as `_graph_request`
    does, e.g. IdType="ImmutableId" so a created message's id survives the
    move to Sent Items.

    `endpoint` accepts either a relative path (e.g., "/drives/{id}/root/delta")
    or an absolute URL (e.g., a returned @odata.deltaLink). Both are supported
    so that delta pagination works naturally.

    Returns {"_error": "delta_expired", "_status": 410} on HTTP 410 — callers
    should reset their deltaLink and re-list from scratch rather than treat it
    as a hard failure.
    """
    # SSRF guard: pin absolute URLs to graph.microsoft.com before the
    # bearer token is acquired. A poisoned delta_link must not be able to
    # cause the app-only Graph token to be sent to an attacker-controlled
    # host. Relative paths always join GRAPH_API_URL and are safe.
    if endpoint.startswith("http"):
        host = (urlparse(endpoint).hostname or "").lower()
        if host not in _ALLOWED_GRAPH_HOSTS:
            raise ValueError(
                f"App-only Graph request rejected: absolute URL must target "
                f"graph.microsoft.com (got host={host!r})."
            )
        url = endpoint
    else:
        url = f"{GRAPH_API_URL}{endpoint}"

    access_token = await _get_app_only_token(credentials)

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    async with httpx.AsyncClient(timeout=60.0) as client:
        attempt = 0
        while True:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
            )
            # Retry throttling / transient errors with backoff before
            # surfacing them. Token stays valid across retries (~1h life).
            if response.status_code in (429, 503) and attempt < _MAX_THROTTLE_RETRIES:
                await asyncio.sleep(_throttle_delay_seconds(response, attempt))
                attempt += 1
                continue
            break

        if response.status_code in (401, 403):
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            error_obj = error_data.get("error") if isinstance(error_data, dict) else None
            detail = auth_reason(error_obj.get("message") if isinstance(error_obj, dict) else None)
            if response.status_code == 401:
                raise GraphAuthError(
                    f"Authentication failed. Check app credentials and admin consent. ({detail})"
                )
            raise GraphAuthError(
                "Access denied. Verify the app has the required admin-consented "
                "application permission for this resource (e.g. Mail.Read for mailbox "
                "access, or a Sites.Selected per-site grant for SharePoint). "
                f"({detail})"
            )
        elif response.status_code == 404:
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            raise GraphNotFoundError(f"Resource not found. ({graph_reason(error_data)})")
        elif response.status_code == 410:
            # deltaLink expired — caller should reset and re-list.
            return {"_error": "delta_expired", "_status": 410}
        elif response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise ValueError(
                f"Rate limited after {attempt} retries. Retry-After: {retry_after}s"
            )
        elif response.status_code >= 400:
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            error_msg = graph_reason(error_data)
            raise ValueError(f"Graph API error ({response.status_code}): {error_msg}")

        if response.status_code in (204, 202):
            return {}
        return response.json()



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /me/messages
@server.tool(
    name='create_draft',
    description="Create a draft email (saved, not sent). Pass `to` (comma-separated), `subject`, and `body` (required). Optional: `cc`, `is_html` (true for HTML body), `importance` (low/normal/high), `attachment_urls` (array of file URLs, max 10, up to 25MB each, 50MB combined), `attachment_filename` (custom name for first URL attachment), and `attachments` (max 10; each item is a platform file_id/storage-key string, {\"file_id\": \"...\"}, or a pre-fetched {filename, content_base64, content_type} - pass a file_id as-is, the platform inlines the bytes before this tool sees them). `attachment_urls` and `attachments` share the same max-10-count and 50MB-combined limits. Returns the draft_id for use with send_draft.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'to': {'type': 'string', 'description': 'Recipients, comma-separated'},
                    'subject': {'type': 'string', 'description': 'Email subject'},
                    'body': {'type': 'string', 'description': 'Email body'},
                    'cc': {'type': 'string',
                           'description': 'CC recipients, comma-separated'},
                    'is_html': {'type': 'boolean',
                                'description': 'Body is HTML',
                                'default': False},
                    'importance': {'type': 'string',
                                   'description': 'Email importance level',
                                   'enum': ['low', 'normal', 'high'],
                                   'default': 'normal'},
                    'attachment_urls': {'type': 'array',
                                        'items': {'type': 'string'},
                                        'maxItems': 10,
                                        'description': 'File URLs to attach (max 10)'},
                    'attachment_filename': {'type': 'string',
                                            'description': 'Custom filename for the first '
                                                           "URL's downloaded file"},
                    'attachments': {'type': 'array',
                                    'items': {'anyOf': [{'type': 'string',
                                                         'description': 'Platform file_id '
                                                                        '(UUID) or storage '
                                                                        'key'},
                                                        {'type': 'object',
                                                         'properties': {'file_id': {'type': 'string'}},
                                                         'required': ['file_id']},
                                                        {'type': 'object',
                                                         'properties': {'filename': {'type': 'string'},
                                                                        'content_base64': {'type': 'string'},
                                                                        'content_type': {'type': 'string'}},
                                                         'required': ['content_base64']}]},
                                    'maxItems': 10,
                                    'description': 'Files to attach (max 10). Three '
                                                   'accepted forms per item: (1) a '
                                                   'platform file_id UUID or storage key '
                                                   'as a plain string, (2) {"file_id": '
                                                   '"<uuid-or-key>"}, or (3) a pre-fetched '
                                                   '{filename, content_base64, '
                                                   'content_type}. Forms 1 and 2 are '
                                                   'replaced with form 3 by the platform '
                                                   'before this tool runs, so pass the '
                                                   'identifier as-is: never put it in '
                                                   '`filename`, and never send an empty '
                                                   '`content_base64`. DO NOT ENCODE '
                                                   '`content_base64` YOURSELF. Form 3 '
                                                   'exists for bytes the platform has '
                                                   'already fetched, not for base64 you '
                                                   'write: a hand-encoded attachment '
                                                   'arrives as a file that opens and is '
                                                   'silently corrupt. If you hold no '
                                                   'file_id or storage key, send the mail '
                                                   'without an attachment and say so - do '
                                                   'not improvise one, and do not retype '
                                                   'data from the conversation into a '
                                                   'file.'}},
     'required': ['to', 'subject', 'body']})
async def create_draft(credentials: Dict[str, str],
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    is_html: bool = False,
    importance: str = "normal",
    attachment_urls: Optional[List[str]] = None,
    attachment_filename: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Create a draft email."""
    try:
        if len(subject) > MAX_SUBJECT_LEN:
            return {"success": False, "error": f"Subject exceeds {MAX_SUBJECT_LEN} characters"}
        if len(body) > MAX_BODY_SIZE:
            return {"success": False, "error": "Body exceeds 5MB limit"}
        if importance not in ("low", "normal", "high"):
            return {"success": False, "error": "Importance must be low, normal, or high"}

        err, to_recipients = parse_recipients(to, "email")
        if err:
            return {"success": False, "error": err}

        message: Dict[str, Any] = {
            "subject": subject,
            "body": {
                "contentType": "HTML" if is_html else "Text",
                "content": body,
            },
            "toRecipients": to_recipients,
            "importance": importance,
        }

        if cc:
            err, cc_recipients = parse_recipients(cc, "CC")
            if err:
                return {"success": False, "error": err}
            message["ccRecipients"] = cc_recipients

        (
            inline_attachments,
            large_attachments,
            attachments_info,
            attachment_error,
        ) = await gather_attachments(attachment_urls, attachment_filename, attachments)
        if attachment_error:
            return {"success": False, "error": attachment_error}

        if inline_attachments:
            message["attachments"] = inline_attachments

        # Fetch the access token once and reuse it for every Graph call
        # below (create + one per large attachment) instead of refreshing
        # it on each call.
        access_token = await _get_access_token(credentials)

        result = await _graph_request(
            credentials,
            "POST",
            "/me/messages",
            json_data=message,
            access_token=access_token,
        )
        draft_id = result.get("id")

        # The draft already exists at this point. If a large attachment
        # fails to upload, don't fail the whole call and hide draft_id —
        # report the draft as created and flag which attachment(s) didn't
        # make it, so the caller isn't left with an orphaned draft it
        # doesn't know about.
        failed_attachments: List[str] = []
        for large in large_attachments:
            try:
                await _upload_large_attachment(
                    credentials,
                    draft_id,
                    large["filename"],
                    large["content"],
                    large["content_type"],
                    access_token=access_token,
                )
            except ValueError as e:
                logger.warning(
                    "create_draft_attachment_upload_failed",
                    draft_id=draft_id,
                    filename=large["filename"],
                    error=str(e),
                )
                failed_attachments.append(large["filename"])

        draft_result: Dict[str, Any] = {
            "success": True,
            "draft_id": draft_id,
            "subject": subject,
            "message": "Draft created",
        }
        if attachments_info:
            draft_result["attachments"] = attachments_info
            draft_result["message"] = (
                f"Draft created with {len(attachments_info)} attachment(s)"
            )
        if failed_attachments:
            draft_result["failed_attachments"] = failed_attachments
            draft_result["message"] = (
                f"Draft created, but {len(failed_attachments)} attachment(s) "
                f"failed to upload: {', '.join(failed_attachments)}"
            )

        return draft_result
    except Exception as e:
        logger.exception("create_draft_error", error=str(e))
        return graph_failure("Failed to create draft", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /me/messages/{email_id}
@server.tool(
    name='delete_email',
    description="Delete an email (moves to Deleted Items). Pass `email_id` (from list_emails or search_emails). No other parameters.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'}},
     'required': ['email_id']})
async def delete_email(credentials: Dict[str, str],
    email_id: str,
) -> Dict[str, Any]:
    """Delete an email."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}

        await _graph_request(credentials, "DELETE", f"/me/messages/{email_id}")

        return {
            "success": True,
            "email_id": email_id,
            "message": "Email deleted",
        }
    except Exception as e:
        logger.exception("delete_email_error", error=str(e))
        return graph_failure("Failed to delete email", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='download_attachment',
    description="Download attachment content as base64 (25MB limit, set via MAX_ATTACHMENT_DOWNLOAD_SIZE_MB). Pass `email_id` and `attachment_id` (from list_attachments). Pass `mailbox` (an address) to download from a shared mailbox via app-only auth instead of the signed-in user's own mailbox - same as get_email's mailbox param. Returns name, content_type, size, and content_base64.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'attachment_id': {'type': 'string',
                                      'description': 'Attachment ID returned by '
                                                     'list_attachments'},
                    'mailbox': {'type': 'string',
                                'description': 'Shared mailbox address to download from '
                                               'via app-only auth, instead of /me. Omit to '
                                               "use the signed-in user's own mailbox."}},
     'required': ['email_id', 'attachment_id']})
async def download_attachment(credentials: Dict[str, str],
    email_id: str,
    attachment_id: str,
    mailbox: Optional[str] = None,
) -> Dict[str, Any]:
    """Download attachment content as base64."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}
        err = validate_id(attachment_id, "attachment_id")
        if err:
            return {"success": False, "error": err}
        if mailbox and not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "mailbox must be a valid email address"}
        if mailbox:
            err = check_mailbox_allowed(credentials, mailbox)
            if err:
                return {"success": False, "error": err}

        request, base = resolve_graph_scope(_GRAPH_SCOPE_OWNER, mailbox)

        # Step 1: Fetch metadata only to check size before downloading content
        meta = await request(
            credentials,
            "GET",
            f"{base}/messages/{email_id}/attachments/{attachment_id}",
            params={"$select": "id,name,contentType,size"},
        )

        name = meta.get("name", "attachment")
        size = meta.get("size", 0)

        if size > MAX_ATTACHMENT_SIZE:
            return {
                "success": False,
                "error": (
                    f"Attachment '{name}' is {size} bytes, exceeds "
                    f"{MAX_ATTACHMENT_SIZE // (1024 * 1024)}MB limit"
                ),
                "name": name,
                "size": size,
            }

        # Step 2: Fetch full content
        result = await request(
            credentials,
            "GET",
            f"{base}/messages/{email_id}/attachments/{attachment_id}",
        )

        content_type = result.get("contentType", "application/octet-stream")
        content_bytes = result.get("contentBytes", "")

        return {
            "success": True,
            "name": name,
            "content_type": content_type,
            "size": size,
            "_mcp_content": {
                "type": "resource",
                "resource": {
                    "uri": f"attachment://{name}",
                    "mimeType": content_type,
                    "blob": content_bytes,
                },
            },
        }
    except Exception as e:
        logger.exception("download_attachment_error", error=str(e))
        return graph_failure("Failed to download attachment", e)






# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /me/messages/{email_id}/forward
@server.tool(
    name='forward_email',
    description="Forward an email. Pass `email_id` (from list_emails or search_emails) and `to` (comma-separated recipients). Optional: `comment` (text added above forwarded content).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'to': {'type': 'string', 'description': 'Recipients, comma-separated'},
                    'comment': {'type': 'string',
                                'description': 'Comment above forwarded content',
                                'default': ''}},
     'required': ['email_id', 'to']})
async def forward_email(credentials: Dict[str, str],
    email_id: str,
    to: str,
    comment: str = "",
) -> Dict[str, Any]:
    """Forward an email to specified recipients."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}

        if len(comment) > MAX_BODY_SIZE:
            return {"success": False, "error": "Comment exceeds 5MB limit"}

        err, to_recipients = parse_recipients(to, "email")
        if err:
            return {"success": False, "error": err}

        json_data = {
            "toRecipients": to_recipients,
            "comment": comment,
        }

        await _graph_request(
            credentials,
            "POST",
            f"/me/messages/{email_id}/forward",
            json_data=json_data,
        )

        return {
            "success": True,
            "email_id": email_id,
            "forwarded_to": to,
            "message": "Email forwarded",
        }
    except Exception as e:
        logger.exception("forward_email_error", error=str(e))
        return graph_failure("Failed to forward email", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to False - takes `mark_as_read` and PATCHes isRead; a read-only deployment must not let a model mutate mailbox state.
# Destructive: that same PATCH overwrites the message's prior unread
# state, so the tool removes information as well as adding it.
@server.tool(
    name='get_email',
    description="Get full email content. Pass `email_id` (from list_emails or search_emails) and optionally `mark_as_read` (true to mark read after fetching). Pass `mailbox` (an address) to read a shared mailbox via app-only auth instead of the signed-in user's own mailbox - requires the org's Azure app to have the application permission Mail.Read/Mail.ReadWrite admin-consented, same as watch_mailbox. Returns full body, attachments list, importance, and web_link.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'mark_as_read': {'type': 'boolean',
                                     'description': 'Mark as read after fetching',
                                     'default': False},
                    'mailbox': {'type': 'string',
                                'description': 'Shared mailbox address to read via '
                                               'app-only auth, instead of /me. Omit to '
                                               "read the signed-in user's own mailbox."},
                    'strip_html': {'type': 'boolean',
                                   'description': 'Strip HTML markup from the body before '
                                                  'returning it (same cleanup '
                                                  'get_email_thread already applies), so '
                                                  'the 10000-char truncation budget covers '
                                                  'actual message content instead of '
                                                  "Outlook's boilerplate <style>/<div> "
                                                  'markup. Default False preserves the '
                                                  'original raw body for callers that '
                                                  'render it.',
                                   'default': False},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}},
     'required': ['email_id']})
async def get_email(credentials: Dict[str, str],
    email_id: str,
    mark_as_read: bool = False,
    mailbox: Optional[str] = None,
    strip_html: bool = False,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Get full email content."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}
        if mailbox and not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "mailbox must be a valid email address"}
        if mailbox:
            err = check_mailbox_allowed(credentials, mailbox)
            if err:
                return {"success": False, "error": err}

        params = {
            "$select": "id,internetMessageId,conversationId,internetMessageHeaders,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,body,hasAttachments,importance,webLink,attachments,categories",
            "$expand": "attachments($select=id,name,contentType,size)",
        }

        # Shared-mailbox reads (webhook refetch after a change notification) use
        # app-only auth against /users/{mailbox}/..., same as watch_mailbox in
        # subscriptions.py - there's no signed-in user in that flow. Everything
        # else keeps using the delegated /me/... path.
        request, base = resolve_graph_scope(_GRAPH_SCOPE_OWNER, mailbox)

        result = await request(credentials, "GET", f"{base}/messages/{email_id}", params=params)

        if mark_as_read and not result.get("isRead"):
            await request(
                credentials,
                "PATCH",
                f"{base}/messages/{email_id}",
                json_data={"isRead": True},
            )

        body = result.get("body", {})
        body_content = body.get("content", "")
        # Strip BEFORE truncating - otherwise the 10000-char budget mostly
        # goes to Outlook's boilerplate <style>/<div> wrapper markup, and
        # real content past it silently gets cut.
        #
        # The quoted trail goes whenever strip_html is requested, regardless of
        # the original contentType: a plain-text reply carries its own inline
        # quote header just as an HTML one does. Also before truncation, for the
        # same reason as above - a long quoted trail would otherwise eat the
        # budget that should go to this message's actual new content.
        if strip_html:
            if body.get("contentType") == "html":
                body_content = _strip_html(body_content)
            body_content = _strip_quoted_reply(body_content)
        if len(body_content) > 10000:
            body_content = body_content[:10000] + "\n\n... (truncated)"

        return {
            "success": True,
            "id": result.get("id"),
            # Graph's RFC-822 Message-ID - stable across folder moves, unlike "id"
            # above. This is the dedup key downstream consumers (an email
            # intake) key tickets on, not "id".
            "internet_message_id": result.get("internetMessageId"),
            # Thread id - stable across all messages in the same conversation,
            # used by an email intake to detect a customer reply on an
            # existing ticket rather than creating a duplicate. NOT always
            # reliable across a relay/client boundary (confirmed live - a Gmail
            # reply landed with a different conversationId than its own thread's
            # original message) - in_reply_to/references below are the
            # standards-based (RFC 5322) fallback for exactly that case.
            "conversation_id": result.get("conversationId"),
            "in_reply_to": _header_value(result, "In-Reply-To"),
            "references": _header_value(result, "References"),
            "subject": result.get("subject", "(No subject)"),
            "from": _fmt_addr(result.get("from", {})),
            "to": _fmt_addr_list(result.get("toRecipients")),
            "cc": _fmt_addr_list(result.get("ccRecipients")),
            "date": _fmt_date(result.get("receivedDateTime"), timezone),
            "is_read": result.get("isRead"),
            "body_type": body.get("contentType"),
            "body": body_content,
            "has_attachments": result.get("hasAttachments"),
            "attachments": [
                {
                    "name": a.get("name"),
                    "content_type": a.get("contentType"),
                    "size": a.get("size"),
                }
                for a in result.get("attachments", [])
            ],
            "importance": result.get("importance"),
            "web_link": result.get("webLink"),
            # Outlook's colored "Categorize" tags. An empty list means none is
            # applied, not that the field was not fetched. set_email_categories
            # adds and removes them.
            "categories": result.get("categories") or [],
        }
    except Exception as e:
        logger.exception("get_email_error", error=str(e))
        return graph_failure("Failed to get email", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/messages/{eid}
@server.tool(
    name='get_email_bodies',
    description="Fetch truncated plain-text bodies for multiple emails in one call. Pass `email_ids` (array of IDs from list_emails, search_emails, or match_emails, max 10) and `max_body_length` (default: 3000 chars). Returns compact results with id, subject, from, date, and body text (HTML stripped). Use this for bulk summarization or reply preparation.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_ids': {'type': 'array',
                                  'items': {'type': 'string'},
                                  'description': 'Message IDs to fetch (max 10)'},
                    'max_body_length': {'type': 'integer',
                                        'description': 'Max body characters per email '
                                                       '(default: 3000)',
                                        'default': 3000},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}},
     'required': ['email_ids']})
async def get_email_bodies(credentials: Dict[str, str],
    email_ids: List[str],
    max_body_length: int = 3000,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch truncated plain-text bodies for multiple emails."""
    try:
        if not email_ids:
            return {"success": False, "error": "email_ids cannot be empty"}
        if len(email_ids) > 10:
            return {"success": False, "error": "Max 10 emails per call"}

        max_body_length = min(max(max_body_length, 100), 10000)

        results = []
        failed = []
        for eid in email_ids:
            err = validate_id(eid, "email_id")
            if err:
                failed.append({"id": eid, "error": err})
                continue
            try:
                msg = await _graph_request(
                    credentials,
                    "GET",
                    f"/me/messages/{eid}",
                    params={"$select": "id,subject,from,body,receivedDateTime"},
                )

                body_content = msg.get("body", {}).get("content", "")
                body_type = msg.get("body", {}).get("contentType", "text")

                # Strip HTML to plain text
                if body_type.lower() == "html":
                    body_text = _strip_html(body_content)
                else:
                    body_text = body_content
                body_text = _strip_quoted_reply(body_text)

                if len(body_text) > max_body_length:
                    body_text = body_text[:max_body_length] + "..."

                results.append(
                    {
                        "id": msg.get("id"),
                        "subject": msg.get("subject", "(No subject)"),
                        "from": _fmt_addr(msg.get("from", {})),
                        "date": _fmt_date(msg.get("receivedDateTime"), timezone),
                        "body": body_text,
                    }
                )
            except GraphAuthError:
                raise  # not connected: the whole call fails, never a per-ID entry
            except Exception:
                failed.append({"id": eid, "error": "Failed to fetch"})

        response: Dict[str, Any] = {
            "success": True,
            "count": len(results),
            "emails": results,
        }
        if failed:
            response["failed"] = failed
        return response
    except Exception as e:
        logger.exception("get_email_bodies_error", error=str(e))
        return graph_failure("Failed to fetch email bodies", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/messages/{email_id}
@server.tool(
    name='get_email_thread',
    description="Get all emails in a conversation thread. Pass `email_id` of any message in the thread and `max_results` (default: 25, max: 50). Returns all messages in the thread ordered oldest-first, with plain-text bodies (HTML stripped). Use this to see the full back-and-forth of a conversation.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID of any email in the thread '
                                                '(from list_emails, search_emails, etc.)'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max messages to return (default: 25, '
                                                   'max: 50)',
                                    'default': 25},
                    'max_body_length': {'type': 'integer',
                                        'description': 'Max body characters per message '
                                                       '(default: 3000)',
                                        'default': 3000},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}},
     'required': ['email_id']})
@declare_output_schema({'properties': {'conversation_id': {'description': 'Thread ID (same for all emails in this '
                                                                          'conversation)',
                                                           'type': 'string'},
                                       'message_count': {'description': 'Number of messages in thread',
                                                         'type': 'integer'},
                                       'messages': {'items': {'properties': {'body': {'description': 'Plain-text body '
                                                                                                     '(HTML stripped)',
                                                                                      'type': 'string'},
                                                                             'date': {'description': 'Date '
                                                                                                     '(YYYY-MM-DD '
                                                                                                     'HH:MM)',
                                                                                      'type': 'string'},
                                                                             'from': {'description': "Sender as 'Name "
                                                                                                     "<email>'",
                                                                                      'type': 'string'},
                                                                             'has_attachments': {'type': 'boolean'},
                                                                             'id': {'description': 'Email ID',
                                                                                    'type': 'string'},
                                                                             'is_read': {'type': 'boolean'},
                                                                             'subject': {'type': 'string'},
                                                                             'to': {'description': 'Recipients '
                                                                                                   'comma-separated',
                                                                                    'type': 'string'}},
                                                              'type': 'object'},
                                                    'type': 'array'},
                                       'subject': {'description': 'Thread subject line', 'type': 'string'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def get_email_thread(credentials: Dict[str, str],
    email_id: str,
    max_results: int = 25,
    max_body_length: int = 3000,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Get all emails in a conversation thread by conversationId."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}

        max_results = min(max(max_results, 1), 50)
        max_body_length = min(max(max_body_length, 100), 10000)

        # Step 1: fetch the conversationId from the given email
        source = await _graph_request(
            credentials,
            "GET",
            f"/me/messages/{email_id}",
            params={"$select": "conversationId,subject"},
        )
        conversation_id = source.get("conversationId")
        if not conversation_id:
            return {
                "success": False,
                "error": "Email has no conversationId — cannot retrieve thread",
            }

        # Step 2: fetch all messages with the same conversationId
        # Note: Graph API doesn't support $orderby combined with $filter on conversationId,
        # so we sort in Python after fetching.
        result = await _graph_request(
            credentials,
            "GET",
            "/me/messages",
            params={
                "$filter": f"conversationId eq '{conversation_id}'",
                "$top": max_results,
                "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,body,hasAttachments",
            },
        )
        messages = sorted(
            result.get("value", []),
            key=lambda m: m.get("receivedDateTime", ""),
        )

        thread = []
        for m in messages:
            body = m.get("body", {})
            body_content = body.get("content", "")
            if body.get("contentType", "text").lower() == "html":
                body_text = _strip_html(body_content)
            else:
                body_text = body_content
            body_text = _strip_quoted_reply(body_text)
            if len(body_text) > max_body_length:
                body_text = body_text[:max_body_length] + "..."

            # Every field here is already in the $select above, so surfacing
            # them costs nothing on the wire. `id` is what lets a follow-up
            # act on one message in the thread, and `has_attachments` is how
            # the agent knows to reach for list_attachments/read_attachment.
            thread.append(
                {
                    "id": m.get("id"),
                    "subject": m.get("subject", "(No subject)"),
                    "from": _fmt_addr(m.get("from", {})),
                    "to": _fmt_addr_list(m.get("toRecipients")),
                    "date": _fmt_date(m.get("receivedDateTime"), timezone),
                    "is_read": m.get("isRead"),
                    "body": body_text,
                    "has_attachments": m.get("hasAttachments"),
                }
            )

        return {
            "success": True,
            "conversation_id": conversation_id,
            "subject": source.get("subject", "(No subject)"),
            "message_count": len(thread),
            "messages": thread,
        }
    except Exception as e:
        logger.exception("get_email_thread_error", error=str(e))
        return graph_failure("Failed to get email thread", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/manager
@server.tool(
    name='get_my_manager',
    description="Get the SIGNED-IN user's manager/boss. Use for 'who is my manager', 'my boss'. Takes NO parameters - never pass 'me'. Returns the manager's display_name, email, job_title. Returns not-found if no manager is set in the directory.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
async def get_my_manager(credentials: Dict[str, str]) -> Dict[str, Any]:
    """Resolve the connected user's manager via Graph /me/manager."""
    try:
        r = await _graph_request(
            credentials,
            "GET",
            "/me/manager",
            params={"$select": "displayName,mail,userPrincipalName,jobTitle"},
        )
        display_name = r.get("displayName")
        email = r.get("mail") or r.get("userPrincipalName")

        # /me/manager needs the User.Read.All DELEGATED scope; User.Read is
        # NOT sufficient, and User.ReadBasic.All is not accepted either
        # (unlike /me/directReports - the two are not symmetric). Without it
        # Graph does not fail: it applies "limited information returned for
        # inaccessible member objects" and hands back the object with only
        # @odata.type and id populated. Reporting that as success would show
        # the user an empty manager card and read as missing HR data, so
        # name the real cause instead.
        if not display_name and not email:
            return {
                "success": False,
                "error": (
                    "Your manager could not be read. The connected Microsoft 365 "
                    "app is missing the User.Read.All permission, which Graph "
                    "requires for manager lookups."
                ),
            }

        return {
            "success": True,
            "display_name": display_name,
            "email": email,
            "job_title": r.get("jobTitle"),
        }
    except GraphAuthError as e:
        return graph_failure("Failed to get manager", e)
    except Exception:
        return {
            "success": False,
            "error": "No manager is set for your account in the directory.",
        }



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me
@server.tool(
    name='get_my_profile',
    description="Get the SIGNED-IN user's OWN identity profile. Use for 'what is my email', 'who am I', 'my job title', 'my department'. Takes NO parameters - never pass 'me', 'my', or any user id (identity comes from the connected account). Returns display_name, email, job_title, department, office_location.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
async def get_my_profile(credentials: Dict[str, str]) -> Dict[str, Any]:
    """Resolve the connected user's own profile via Graph /me."""
    try:
        r = await _graph_request(
            credentials,
            "GET",
            "/me",
            params={
                "$select": "displayName,mail,userPrincipalName,jobTitle,department,officeLocation"
            },
        )
        return {
            "success": True,
            "display_name": r.get("displayName"),
            "email": r.get("mail") or r.get("userPrincipalName"),
            "job_title": r.get("jobTitle"),
            "department": r.get("department"),
            "office_location": r.get("officeLocation"),
        }
    except Exception as e:
        logger.exception("get_my_profile_error", error=str(e))
        return {
            "success": False,
            "error": "Could not resolve your Microsoft 365 profile. The mailbox may not be connected.",
        }



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/mailFolders/{folder}
@server.tool(
    name='get_unread_count',
    description="Get unread and total email count for a folder. Pass `folder` (default: inbox). No other parameters. Returns unread_count and total_count.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'folder': {'type': 'string',
                               'description': 'Mail folder to check',
                               'enum': ['inbox',
                                        'drafts',
                                        'sentitems',
                                        'deleteditems',
                                        'junkemail',
                                        'archive',
                                        'outbox'],
                               'default': 'inbox'}}})
async def get_unread_count(credentials: Dict[str, str],
    folder: str = "inbox",
) -> Dict[str, Any]:
    """Get unread count for a folder."""
    try:
        if not folder or not folder.strip():
            return {"success": False, "error": "Folder name is required"}
        folder = folder.strip().lower()
        if folder not in VALID_FOLDERS:
            folder = "inbox"
        result = await _graph_request(
            credentials,
            "GET",
            f"/me/mailFolders/{folder}",
            params={"$select": "displayName,unreadItemCount,totalItemCount"},
        )

        return {
            "success": True,
            "folder": result.get("displayName", folder),
            "unread_count": result.get("unreadItemCount", 0),
            "total_count": result.get("totalItemCount", 0),
        }
    except Exception as e:
        logger.exception("get_unread_count_error", error=str(e))
        return graph_failure("Failed to get unread count", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_attachments',
    description="List attachments on an email. Pass `email_id` (from list_emails or search_emails). Pass `mailbox` (an address) to list attachments on a shared mailbox via app-only auth instead of the signed-in user's own mailbox - same as get_email's mailbox param. Returns attachment id, name, content_type, size, and is_inline. Use the attachment id with download_attachment.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'mailbox': {'type': 'string',
                                'description': 'Shared mailbox address to list attachments '
                                               'via app-only auth, instead of /me. Omit to '
                                               "use the signed-in user's own mailbox."}},
     'required': ['email_id']})
async def list_attachments(credentials: Dict[str, str],
    email_id: str,
    mailbox: Optional[str] = None,
) -> Dict[str, Any]:
    """List attachments on an email."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}
        if mailbox and not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "mailbox must be a valid email address"}
        if mailbox:
            err = check_mailbox_allowed(credentials, mailbox)
            if err:
                return {"success": False, "error": err}

        request, base = resolve_graph_scope(_GRAPH_SCOPE_OWNER, mailbox)
        result = await request(
            credentials,
            "GET",
            f"{base}/messages/{email_id}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        )
        attachments = result.get("value", [])

        return {
            "success": True,
            "email_id": email_id,
            "count": len(attachments),
            "attachments": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "content_type": a.get("contentType"),
                    "size": a.get("size"),
                    "is_inline": a.get("isInline", False),
                }
                for a in attachments
            ],
        }
    except Exception as e:
        logger.exception("list_attachments_error", error=str(e))
        return graph_failure("Failed to list attachments", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/mailFolders/drafts/messages
@server.tool(
    name='list_drafts',
    description="List draft emails. Pass `max_results` (default: 20, max: 100) and `skip` for pagination. No required parameters. Returns drafts with id, subject, recipients, last_modified, and `has_more` flag.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'max_results': {'type': 'integer',
                                    'description': 'Max drafts to return (default: 20, '
                                                   'max: 100)',
                                    'default': 20},
                    'skip': {'type': 'integer',
                             'description': 'Number of items to skip for pagination '
                                            '(default: 0)',
                             'default': 0}}})
async def list_drafts(credentials: Dict[str, str],
    max_results: int = 20,
    skip: int = 0,
) -> Dict[str, Any]:
    """List draft emails."""
    try:
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        skip = max(skip, 0)

        params: Dict[str, Any] = {
            "$top": max_results,
            "$orderby": "lastModifiedDateTime desc",
            "$select": "id,subject,toRecipients,ccRecipients,lastModifiedDateTime,bodyPreview,hasAttachments",
        }
        if skip > 0:
            params["$skip"] = skip

        result = await _graph_request(
            credentials,
            "GET",
            "/me/mailFolders/drafts/messages",
            params=params,
        )
        messages = result.get("value", [])

        return {
            "success": True,
            "count": len(messages),
            "has_more": "@odata.nextLink" in result,
            "drafts": [
                {
                    "id": m.get("id"),
                    "subject": m.get("subject", "(No subject)"),
                    "to": _fmt_addr_list(m.get("toRecipients")),
                    "cc": _fmt_addr_list(m.get("ccRecipients")),
                    "last_modified": (m.get("lastModifiedDateTime") or "")[:16].replace(
                        "T", " "
                    ),
                    "snippet": (m.get("bodyPreview") or "")[:150],
                    "has_attachments": m.get("hasAttachments"),
                }
                for m in messages
            ],
        }
    except Exception as e:
        logger.exception("list_drafts_error", error=str(e))
        return graph_failure("Failed to list drafts", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/mailFolders/{folder_clean}/messages
@server.tool(
    name='list_email_headers',
    description="Compact email listing — returns only id, subject, from, and date. Much lighter than list_emails (no body preview, CC, or attachment info). Pass `folder` (default: inbox), `max_results` (default: 100, max: 100), `unread_only`, and `skip` for pagination. Use this when you need to scan many emails without consuming tokens on body previews.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'folder': {'type': 'string',
                               'description': 'Mail folder to list from',
                               'enum': ['inbox',
                                        'drafts',
                                        'sentitems',
                                        'deleteditems',
                                        'junkemail',
                                        'archive',
                                        'outbox'],
                               'default': 'inbox'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max emails to return (default: 100, '
                                                   'max: 100)',
                                    'default': 100},
                    'unread_only': {'type': 'boolean',
                                    'description': 'Only unread emails',
                                    'default': False},
                    'skip': {'type': 'integer',
                             'description': 'Number of items to skip for pagination '
                                            '(default: 0)',
                             'default': 0},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}}})
async def list_email_headers(credentials: Dict[str, str],
    folder: str = "inbox",
    max_results: int = 100,
    unread_only: bool = False,
    skip: int = 0,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """List email headers only — compact, token-efficient."""
    try:
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        skip = max(skip, 0)
        params: Dict[str, Any] = {
            "$top": max_results,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,isRead",
        }
        if unread_only:
            params["$filter"] = "isRead eq false"
        if skip > 0:
            params["$skip"] = skip

        folder_clean = (folder or "inbox").strip().lower()
        if folder_clean not in VALID_FOLDERS:
            folder_clean = "inbox"
        result = await _graph_request(
            credentials, "GET", f"/me/mailFolders/{folder_clean}/messages", params=params
        )
        messages = result.get("value", [])

        return {
            "success": True,
            "count": len(messages),
            "folder": folder_clean,
            "has_more": "@odata.nextLink" in result,
            "emails": [
                {
                    "id": m.get("id"),
                    "subject": m.get("subject", "(No subject)"),
                    "from": _fmt_addr(m.get("from", {})),
                    "date": _fmt_date(m.get("receivedDateTime"), timezone),
                    "is_read": m.get("isRead"),
                }
                for m in messages
            ],
        }
    except Exception as e:
        logger.exception("list_email_headers_error", error=str(e))
        return graph_failure("Failed to list email headers", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/mailFolders/{folder_clean}/messages
@server.tool(
    name='list_emails',
    description="List recent emails from a folder. Pass `folder` (default: inbox), `max_results` (default: 20, max: 100), `unread_only` (true to filter unread), and `skip` for pagination. Returns emails with id, subject, from, date, snippet, and `has_more` flag. Use search_emails for keyword filtering.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'folder': {'type': 'string',
                               'description': 'Mail folder to list from',
                               'enum': ['inbox',
                                        'drafts',
                                        'sentitems',
                                        'deleteditems',
                                        'junkemail',
                                        'archive',
                                        'outbox'],
                               'default': 'inbox'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max emails to return (default: 20, '
                                                   'max: 100)',
                                    'default': 20},
                    'unread_only': {'type': 'boolean',
                                    'description': 'Only unread emails',
                                    'default': False},
                    'skip': {'type': 'integer',
                             'description': 'Number of items to skip for pagination '
                                            '(default: 0)',
                             'default': 0},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}}})
@declare_output_schema({'properties': {'count': {'description': 'Number of emails returned', 'type': 'integer'},
                                       'emails': {'items': {'properties': {'cc': {'description': 'CC recipients '
                                                                                                 'comma-separated',
                                                                                  'type': 'string'},
                                                                           'date': {'description': 'Received date '
                                                                                                   '(YYYY-MM-DD '
                                                                                                   'HH:MM)',
                                                                                    'type': 'string'},
                                                                           'from': {'description': "Sender as 'Name "
                                                                                                   "<email>'",
                                                                                    'type': 'string'},
                                                                           'has_attachments': {'type': 'boolean'},
                                                                           'id': {'description': 'Email ID (use in '
                                                                                                 'get_email, '
                                                                                                 'get_email_thread, '
                                                                                                 'reply_email)',
                                                                                  'type': 'string'},
                                                                           'importance': {'type': 'string'},
                                                                           'is_read': {'type': 'boolean'},
                                                                           'snippet': {'description': 'Body preview '
                                                                                                      '(first 150 '
                                                                                                      'chars)',
                                                                                       'type': 'string'},
                                                                           'subject': {'type': 'string'},
                                                                           'to': {'description': 'Recipients '
                                                                                                 'comma-separated',
                                                                                  'type': 'string'}},
                                                            'type': 'object'},
                                                  'type': 'array'},
                                       'folder': {'type': 'string'},
                                       'has_more': {'description': 'True if more emails available', 'type': 'boolean'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def list_emails(credentials: Dict[str, str],
    folder: str = "inbox",
    max_results: int = 20,
    unread_only: bool = False,
    skip: int = 0,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """List emails from specified folder."""
    try:
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        skip = max(skip, 0)
        params: Dict[str, Any] = {
            "$top": max_results,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,bodyPreview,hasAttachments,importance",
        }
        if unread_only:
            params["$filter"] = "isRead eq false"
        if skip > 0:
            params["$skip"] = skip

        # Normalize folder name to avoid "Id is malformed" from Graph API
        folder_clean = (folder or "inbox").strip().lower()
        if folder_clean not in VALID_FOLDERS:
            folder_clean = "inbox"
        result = await _graph_request(
            credentials, "GET", f"/me/mailFolders/{folder_clean}/messages", params=params
        )
        messages = result.get("value", [])

        return {
            "success": True,
            "count": len(messages),
            "folder": folder_clean,
            "has_more": "@odata.nextLink" in result,
            "emails": [
                {
                    "id": m.get("id"),
                    "subject": m.get("subject", "(No subject)"),
                    "from": _fmt_addr(m.get("from", {})),
                    "to": _fmt_addr_list(m.get("toRecipients")),
                    "cc": _fmt_addr_list(m.get("ccRecipients")),
                    "date": _fmt_date(m.get("receivedDateTime"), timezone),
                    "is_read": m.get("isRead"),
                    "snippet": (m.get("bodyPreview") or "")[:150],
                    "has_attachments": m.get("hasAttachments"),
                    "importance": m.get("importance"),
                }
                for m in messages
            ],
        }
    except Exception as e:
        logger.exception("list_emails_error", error=str(e))
        return graph_failure("Failed to list emails", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/mailFolders
@server.tool(
    name='list_folders',
    description="List mail folders with unread counts. Pass `max_results` (default: 50) and `skip` for pagination. No required parameters. Returns folder id, name, unread_count, total_count, and `has_more` flag. Use folder IDs with move_email.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'max_results': {'type': 'integer',
                                    'description': 'Max folders to return (default: 50, '
                                                   'max: 100)',
                                    'default': 50},
                    'skip': {'type': 'integer',
                             'description': 'Number of items to skip for pagination '
                                            '(default: 0)',
                             'default': 0}}})
async def list_folders(credentials: Dict[str, str],
    max_results: int = 50,
    skip: int = 0,
) -> Dict[str, Any]:
    """List mail folders."""
    try:
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        skip = max(skip, 0)

        params: Dict[str, Any] = {
            "$top": max_results,
            "$select": "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount",
        }
        if skip > 0:
            params["$skip"] = skip

        result = await _graph_request(
            credentials,
            "GET",
            "/me/mailFolders",
            params=params,
        )
        folders = result.get("value", [])

        return {
            "success": True,
            "count": len(folders),
            "has_more": "@odata.nextLink" in result,
            "folders": [
                {
                    "id": f.get("id"),
                    "name": f.get("displayName"),
                    "parent_folder_id": f.get("parentFolderId"),
                    "child_folder_count": f.get("childFolderCount", 0),
                    "unread_count": f.get("unreadItemCount", 0),
                    "total_count": f.get("totalItemCount", 0),
                }
                for f in folders
            ],
        }
    except Exception as e:
        logger.exception("list_folders_error", error=str(e))
        return graph_failure("Failed to list folders", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /subscriptions
@server.tool(
    name='list_mailbox_subscriptions',
    description="List this Azure AD app's active Microsoft Graph change-notification subscriptions (read-only), filtered to mailboxes in this connection's allowed_mailboxes. Useful for finding orphaned subscriptions after a server restart, since this server does not persist subscription state locally.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}, 'additionalProperties': False})
async def list_mailbox_subscriptions(credentials: Dict[str, str]) -> Dict[str, Any]:
    """List active Graph subscriptions, filtered to this connection's allowlist."""
    try:
        allowed = parse_allowed_mailboxes(credentials)
        result = await _graph_request_app_only(credentials, "GET", "/subscriptions")
        subs = result.get("value", [])
        visible = [s for s in subs if _mailbox_from_resource(s.get("resource", "")) in allowed]

        return {
            "success": True,
            "count": len(visible),
            "subscriptions": [
                {
                    "subscription_id": s.get("id"),
                    "resource": s.get("resource"),
                    "expiration_date_time": s.get("expirationDateTime"),
                    "change_type": s.get("changeType"),
                }
                for s in visible
            ],
        }
    except Exception as e:
        logger.exception("list_mailbox_subscriptions_error", error=str(e))
        return {"success": False, "error": str(e)}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH .../messages/{id} {"isRead": ...} over up to 50 ids.
# Evidence: PATCH /me/messages/{eid}
@server.tool(
    name='mark_emails',
    description="Bulk mark emails as read or unread. Pass `email_ids` (array of IDs from list_emails or search_emails, max 50) and `is_read` (true = mark read, false = mark unread). No other parameters. Returns marked_count and any failed IDs.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_ids': {'type': 'array',
                                  'items': {'type': 'string'},
                                  'description': 'List of message IDs from list_emails or '
                                                 'search_emails (max 50)'},
                    'is_read': {'type': 'boolean',
                                'description': 'true=mark read, false=mark unread'}},
     'required': ['email_ids', 'is_read']})
@declare_output_schema({'properties': {'failed': {'description': 'Failed email IDs with errors', 'type': 'array'},
                                       'is_read': {'type': 'boolean'},
                                       'marked_count': {'description': 'Number of emails marked', 'type': 'integer'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def mark_emails(credentials: Dict[str, str],
    email_ids: List[str],
    is_read: bool,
) -> Dict[str, Any]:
    """Bulk mark emails as read or unread."""
    try:
        if not email_ids:
            return {"success": False, "error": "No email IDs provided"}
        if len(email_ids) > MAX_BATCH_SIZE:
            return {"success": False, "error": f"Too many IDs, max {MAX_BATCH_SIZE}"}

        marked = 0
        failed: List[Dict[str, str]] = []
        rate_limited = False
        for eid in email_ids:
            err = validate_id(eid, "email_id")
            if err:
                failed.append({"id": eid, "error": err})
                continue
            try:
                await _graph_request(
                    credentials,
                    "PATCH",
                    f"/me/messages/{eid}",
                    json_data={"isRead": is_read},
                )
                marked += 1
            except GraphAuthError:
                raise  # not connected: the whole call fails, never a per-ID entry
            except ValueError as e:
                if "Rate limit" in str(e):
                    rate_limited = True
                    failed.append({"id": eid, "error": "Rate limited"})
                    break
                failed.append({"id": eid, "error": "Operation failed"})
            except Exception:
                failed.append({"id": eid, "error": "Operation failed"})

        result: Dict[str, Any] = {
            "success": True,
            "marked_count": marked,
            "is_read": is_read,
            "failed": failed,
        }
        if rate_limited:
            result["warning"] = "Stopped early due to rate limiting"
        return result
    except Exception as e:
        logger.exception("mark_emails_error", error=str(e))
        return graph_failure("Failed to mark emails", e)


# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH .../messages/{id} {"categories": [...]} replaces the whole
# array, so a category named in `remove` is gone afterwards.
# Evidence: GET then PATCH {base}/messages/{email_id}
@server.tool(
    name='set_email_categories',
    description="Add and/or remove Outlook categories on an email - the colored tags shown under a message's subject in Outlook's own UI (e.g. a shared-inbox team's 'Unassigned'/'Assigned to Jane' triage labels). Pass `email_id` and at least one of `add` (category names to add) or `remove` (category names to remove, case-insensitive). Graph's categories field is replace-only, so this reads the message's current categories first and merges - any category not named in `remove` is preserved. A category name that doesn't already exist in the mailbox's master category list is still applied; Outlook just shows it in a default color until someone assigns one. Pass `mailbox` for app-only shared-mailbox access, same as get_email - requires the org's Azure app to have the application permission Mail.ReadWrite admin-consented.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'add': {'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Category names to add',
                            'default': []},
                    'remove': {'type': 'array',
                               'items': {'type': 'string'},
                               'description': 'Category names to remove (case-insensitive '
                                              'match)',
                               'default': []},
                    'mailbox': {'type': 'string',
                                'description': 'Shared mailbox address to update via '
                                               'app-only auth, instead of /me. Omit to '
                                               "update the signed-in user's own mailbox."}},
     'required': ['email_id']})
@declare_output_schema({'properties': {'categories': {'description': "The email's full category list after this "
                                                                     'change',
                                                      'items': {'type': 'string'},
                                                      'type': 'array'},
                                       'email_id': {'type': 'string'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def set_email_categories(credentials: Dict[str, str],
    email_id: str,
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
    mailbox: Optional[str] = None,
) -> Dict[str, Any]:
    """Add/remove Outlook categories on a message, merging with whatever is already set."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}

        add_clean = [c.strip() for c in (add or []) if c and c.strip()]
        remove_clean = {c.strip().lower() for c in (remove or []) if c and c.strip()}
        if not add_clean and not remove_clean:
            return {"success": False, "error": "Provide at least one of add or remove"}

        if mailbox and not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "mailbox must be a valid email address"}
        if mailbox:
            err = check_mailbox_allowed(credentials, mailbox)
            if err:
                return {"success": False, "error": err}

        request, base = resolve_graph_scope(_GRAPH_SCOPE_OWNER, mailbox)

        current = await request(
            credentials,
            "GET",
            f"{base}/messages/{email_id}",
            params={"$select": "categories"},
        )
        existing = current.get("categories") or []

        # Keep the existing order, drop anything named in `remove`
        # (case-insensitively), then append each `add` not already present, so
        # a repeated call with the same `add` changes nothing the second time.
        new_categories = [c for c in existing if c.lower() not in remove_clean]
        for c in add_clean:
            if c not in new_categories:
                new_categories.append(c)

        await request(
            credentials,
            "PATCH",
            f"{base}/messages/{email_id}",
            json_data={"categories": new_categories},
        )

        return {"success": True, "email_id": email_id, "categories": new_categories}
    except Exception as e:
        logger.exception("set_email_categories_error", error=str(e))
        return graph_failure("Failed to update email categories", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False.
# _graph_request(credentials, "GET", ...) then local fuzzy scoring. Its
# own description says no LLM tokens are consumed for matching, and there
# is no PATCH on any path, unlike get_email in the same module.
@server.tool(
    name='match_emails',
    description="Fuzzy match emails against search terms using local scoring — no LLM tokens consumed for matching. Pass `search_terms` (array of strings to match against subject and sender), `folder` (default: inbox), `max_results` (emails to scan, default: 100), and `min_score` (minimum fuzzy score 0-100, default: 50). Returns scored matches sorted by relevance with matched_terms.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'search_terms': {'type': 'array',
                                     'items': {'type': 'string'},
                                     'description': 'Terms to fuzzy-match against email '
                                                    'subject and sender'},
                    'folder': {'type': 'string',
                               'description': 'Mail folder to search',
                               'enum': ['inbox',
                                        'drafts',
                                        'sentitems',
                                        'deleteditems',
                                        'junkemail',
                                        'archive',
                                        'outbox'],
                               'default': 'inbox'},
                    'max_results': {'type': 'integer',
                                    'description': 'Number of recent emails to scan '
                                                   '(default: 100, max: 100)',
                                    'default': 100},
                    'min_score': {'type': 'integer',
                                  'description': 'Minimum fuzzy match score 0-100 '
                                                 '(default: 50)',
                                  'default': 50},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}},
     'required': ['search_terms']})
async def match_emails(credentials: Dict[str, str],
    search_terms: List[str],
    folder: str = "inbox",
    max_results: int = 100,
    min_score: int = 50,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Fuzzy match emails against search terms using RapidFuzz."""
    try:
        from rapidfuzz import fuzz

        if not search_terms:
            return {"success": False, "error": "search_terms cannot be empty"}

        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        min_score = max(min(min_score, 100), 0)

        folder_clean = (folder or "inbox").strip().lower()
        if folder_clean not in VALID_FOLDERS:
            folder_clean = "inbox"

        result = await _graph_request(
            credentials,
            "GET",
            f"/me/mailFolders/{folder_clean}/messages",
            params={
                "$select": "id,subject,from,receivedDateTime",
                "$top": max_results,
                "$orderby": "receivedDateTime desc",
            },
        )
        messages = result.get("value", [])

        scored = []
        for m in messages:
            subject = (m.get("subject") or "").lower()
            from_addr = m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
            from_name = m.get("from", {}).get("emailAddress", {}).get("name", "").lower()

            best_score = 0
            matched_terms = []
            for term in search_terms:
                term_lower = term.lower()

                # Exact substring match (highest priority)
                if term_lower in subject or term_lower in from_addr or term_lower in from_name:
                    score = 100
                else:
                    # Fuzzy token match
                    score = max(
                        fuzz.partial_ratio(term_lower, subject),
                        fuzz.partial_ratio(term_lower, from_addr),
                        fuzz.token_set_ratio(term_lower, subject),
                    )

                if score >= min_score:
                    matched_terms.append(term)
                    best_score = max(best_score, score)

            if matched_terms:
                scored.append(
                    {
                        "id": m.get("id"),
                        "subject": m.get("subject", "(No subject)"),
                        "from": from_addr,
                        "from_name": m.get("from", {}).get("emailAddress", {}).get("name", ""),
                        "date": _fmt_date(m.get("receivedDateTime"), timezone, date_only=True),
                        "score": best_score,
                        "matched_terms": matched_terms,
                    }
                )

        scored.sort(key=lambda x: x["score"], reverse=True)

        return {
            "success": True,
            "match_count": len(scored),
            "scanned": len(messages),
            "folder": folder_clean,
            "matches": scored[:50],
        }
    except Exception as e:
        logger.exception("match_emails_error", error=str(e))
        return graph_failure("Failed to match emails", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: POST .../move removes the message from its source folder and
# returns a new id, invalidating the old one.
# Evidence: POST /me/messages/{email_id}/move
@server.tool(
    name='move_email',
    description="Move an email to another folder. Pass `email_id` (from list_emails or search_emails) and `destination_folder` (well-known name like inbox, archive, deleteditems, junkemail, or a folder ID from list_folders).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'destination_folder': {'type': 'string',
                                           'description': 'Well-known folder name (inbox, '
                                                          'archive, deleteditems, '
                                                          'junkemail) or folder ID from '
                                                          'list_folders'}},
     'required': ['email_id', 'destination_folder']})
async def move_email(credentials: Dict[str, str],
    email_id: str,
    destination_folder: str,
) -> Dict[str, Any]:
    """Move an email to another folder."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}

        if not destination_folder or not destination_folder.strip():
            return {"success": False, "error": "destination_folder is required"}

        result = await _graph_request(
            credentials,
            "POST",
            f"/me/messages/{email_id}/move",
            json_data={"destinationId": destination_folder.strip()},
        )

        return {
            "success": True,
            "email_id": email_id,
            "new_id": result.get("id", email_id),
            "destination": destination_folder,
            "message": "Email moved",
        }
    except Exception as e:
        logger.exception("move_email_error", error=str(e))
        return graph_failure("Failed to move email", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='read_attachment',
    description="Read the TEXT CONTENT of an email attachment (PDF, Word .docx, Excel .xlsx, text/csv/json/html/xml, or a .zip archive). Pass `email_id` and `attachment_id` (from list_attachments). Returns the extracted text so you can summarize or answer questions about the attachment. A .zip returns a list of what it contains plus the text of the readable members. Images and other binary types return a short note instead of text. 25MB limit, set via MAX_ATTACHMENT_DOWNLOAD_SIZE_MB.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'attachment_id': {'type': 'string',
                                      'description': 'Attachment ID returned by '
                                                     'list_attachments'},
                    'mailbox': {'type': 'string',
                                'description': 'Shared mailbox address to read from '
                                               "(app-only). Omit for the signed-in user's "
                                               'own mailbox.'}},
     'required': ['email_id', 'attachment_id']})
async def read_attachment(credentials: Dict[str, str],
    email_id: str,
    attachment_id: str,
    mailbox: Optional[str] = None,
) -> Dict[str, Any]:
    """Download an attachment and return its extracted text content."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}
        err = validate_id(attachment_id, "attachment_id")
        if err:
            return {"success": False, "error": err}
        # Same allowlist gate as download_attachment: the credential decides
        # which shared mailboxes this connection may touch, never the caller.
        if mailbox and not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "mailbox must be a valid email address"}
        if mailbox:
            err = check_mailbox_allowed(credentials, mailbox)
            if err:
                return {"success": False, "error": err}
        request, base = resolve_graph_scope(_GRAPH_SCOPE_OWNER, mailbox)

        meta = await request(
            credentials,
            "GET",
            f"{base}/messages/{email_id}/attachments/{attachment_id}",
            params={"$select": "id,name,contentType,size"},
        )
        name = meta.get("name", "attachment")
        size = meta.get("size", 0)
        if size > MAX_ATTACHMENT_SIZE:
            return {
                "success": False,
                "error": (
                    f"Attachment '{name}' is {size} bytes, exceeds "
                    f"{MAX_ATTACHMENT_SIZE // (1024 * 1024)}MB limit"
                ),
                "name": name,
                "size": size,
            }

        result = await request(
            credentials,
            "GET",
            f"{base}/messages/{email_id}/attachments/{attachment_id}",
        )
        content_type = result.get("contentType", "application/octet-stream")
        content_b64 = result.get("contentBytes", "")
        raw = base64.b64decode(content_b64) if content_b64 else b""
        text = extract_text(name, raw, content_type)

        return {
            "success": True,
            "name": name,
            "content_type": content_type,
            "size": size,
            "text": text,
        }
    except Exception as e:
        logger.exception("read_attachment_error", error=str(e))
        return graph_failure("Failed to read attachment", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /subscriptions/{id} overwrites
# expirationDateTime on an existing record.
# Evidence: PATCH, path built at runtime
@server.tool(
    name='renew_mailbox_subscription',
    description="Extend an existing mailbox subscription before it expires. Pass `subscription_id`. Returns the new expiration_date_time (Graph may cap it below what was requested).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'subscription_id': {'type': 'string',
                                        'description': 'Subscription id to renew'},
                    'expiration_hours': {'type': 'integer',
                                         'description': 'New lifetime in hours from now '
                                                        '(default: 70)',
                                         'default': 70}},
     'required': ['subscription_id'],
     'additionalProperties': False})
async def renew_mailbox_subscription(credentials: Dict[str, str],
    subscription_id: str,
    expiration_hours: int = _DEFAULT_EXPIRATION_HOURS,
) -> Dict[str, Any]:
    """Renew a mailbox subscription."""
    try:
        err = validate_id(subscription_id, "subscription_id")
        if err:
            return {"success": False, "error": err}
        err = _validate_expiration_hours(expiration_hours)
        if err:
            return {"success": False, "error": err}

        result = await _graph_request_app_only(
            credentials,
            "PATCH",
            f"/subscriptions/{subscription_id.strip()}",
            json_data={"expirationDateTime": _expiration_iso(expiration_hours)},
        )

        return {"success": True, "expiration_date_time": result.get("expirationDateTime")}
    except Exception as e:
        logger.exception("renew_mailbox_subscription_error", error=str(e))
        return {"success": False, "error": str(e)}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST {base}/messages/{email_id}/{action}, base /me or /users/{mailbox}
@server.tool(
    name='reply_email',
    description="Reply to an email, keeping the thread. Pass `email_id` (from list_emails or search_emails) and `body` (reply content). Optional: `reply_all` (true to reply to all recipients), `cc` (comma-separated extra CC recipients), `bcc` (comma-separated extra BCC recipients), `is_html` (true for HTML body), `attachments` (same three forms as send_email: a platform file_id/storage-key string, {\"file_id\": ...}, or a pre-fetched {filename, content_base64, content_type} - never encode content_base64 yourself), `attachment_urls`, and `mailbox` (see its own description). Returns `attachments` listing what was actually attached; if that list is absent or empty, nothing was attached and you must not claim otherwise.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'email_id': {'type': 'string',
                                 'description': 'Message ID returned by list_emails or '
                                                'search_emails'},
                    'body': {'type': 'string', 'description': 'Reply body content'},
                    'reply_all': {'type': 'boolean',
                                  'description': 'Reply to all recipients',
                                  'default': False},
                    'cc': {'type': 'string',
                           'description': 'Extra CC recipients, comma-separated',
                           'default': ''},
                    'bcc': {'type': 'string',
                            'description': 'Extra BCC recipients, comma-separated',
                            'default': ''},
                    'is_html': {'type': 'boolean',
                                'description': 'Body is HTML',
                                'default': False},
                    'attachments': {'type': 'array',
                                    'items': {'anyOf': [{'type': 'string',
                                                         'description': 'Platform file_id '
                                                                        '(UUID) or storage '
                                                                        'key'},
                                                        {'type': 'object',
                                                         'properties': {'file_id': {'type': 'string'}},
                                                         'required': ['file_id']},
                                                        {'type': 'object',
                                                         'properties': {'filename': {'type': 'string'},
                                                                        'content_base64': {'type': 'string'},
                                                                        'content_type': {'type': 'string'}},
                                                         'required': ['content_base64']}]},
                                    'maxItems': 10,
                                    'description': 'Files to attach (max 10). Three '
                                                   'accepted forms per item: (1) a '
                                                   'platform file_id UUID or storage key '
                                                   'as a plain string, (2) {"file_id": '
                                                   '"<uuid-or-key>"}, or (3) a pre-fetched '
                                                   '{filename, content_base64, '
                                                   'content_type}. Forms 1 and 2 are '
                                                   'replaced with form 3 by the platform '
                                                   'before this tool runs, so pass the '
                                                   'identifier as-is: never put it in '
                                                   '`filename`, and never send an empty '
                                                   '`content_base64`. DO NOT ENCODE '
                                                   '`content_base64` YOURSELF. Form 3 '
                                                   'exists for bytes the platform has '
                                                   'already fetched, not for base64 you '
                                                   'write: a hand-encoded attachment '
                                                   'arrives as a file that opens and is '
                                                   'silently corrupt. If you hold no '
                                                   'file_id or storage key, send the mail '
                                                   'without an attachment and say so - do '
                                                   'not improvise one, and do not retype '
                                                   'data from the conversation into a '
                                                   'file.'},
                    'attachment_urls': {'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'URLs to fetch and attach (max 10, '
                                                       '25MB each)'},
                    'attachment_filename': {'type': 'string',
                                            'description': 'Custom filename for the first '
                                                           "URL's downloaded file"},
                    'mailbox': {'type': 'string',
                                'description': 'Shared mailbox address to reply from via '
                                               'app-only auth, instead of /me - for both '
                                               'the plain and attachment reply paths. '
                                               "Requires the org's Azure app to have the "
                                               'application permission '
                                               'Mail.Send/Mail.ReadWrite admin-consented, '
                                               'same as get_email, and the mailbox must be '
                                               "in this connection's allowed_mailboxes."}},
     'required': ['email_id', 'body']})
@declare_output_schema({'properties': {'attachments': {'description': 'What was actually attached. Absent means '
                                                                      'nothing was.',
                                                       'items': {'type': 'object'},
                                                       'type': 'array'},
                                       'email_id': {'description': 'ID of the email replied to', 'type': 'string'},
                                       'message': {'type': 'string'},
                                       'message_id': {'description': 'Immutable id of the sent reply (attachment path '
                                                                     'only)',
                                                      'type': 'string'},
                                       'reply_all': {'type': 'boolean'},
                                       'success': {'type': 'boolean'}},
                        'type': 'object'})
async def reply_email(credentials: Dict[str, str],
    email_id: str,
    body: str,
    reply_all: bool = False,
    cc: str = "",
    bcc: str = "",
    is_html: bool = False,
    attachments: Optional[List[Dict[str, Any]]] = None,
    attachment_urls: Optional[List[str]] = None,
    attachment_filename: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> Dict[str, Any]:
    """Reply or reply-all to an email, optionally adding extra CC/BCC recipients."""
    try:
        err = validate_id(email_id, "email_id")
        if err:
            return {"success": False, "error": err}

        if len(body) > MAX_BODY_SIZE:
            return {"success": False, "error": "Reply body exceeds 5MB limit"}

        # Checked before either reply path: a Graph message id belongs to one
        # mailbox, so the plain reply must go to the same mailbox as the
        # attachment reply. "" is treated as omitted, like everywhere else.
        if mailbox and not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "mailbox must be a valid email address"}
        if mailbox:
            err = check_mailbox_allowed(credentials, mailbox)
            if err:
                return {"success": False, "error": err}

        action = "replyAll" if reply_all else "reply"

        message: Dict[str, Any] = {}
        if cc:
            err, cc_recipients = parse_recipients(cc, "cc")
            if err:
                return {"success": False, "error": err}
            message["ccRecipients"] = cc_recipients
        if bcc:
            err, bcc_recipients = parse_recipients(bcc, "bcc")
            if err:
                return {"success": False, "error": err}
            message["bccRecipients"] = bcc_recipients

        # With attachments the immediate /reply action cannot be used: it
        # has nowhere to carry a file. Build the reply as a draft, attach,
        # then send. Without attachments the original one-call path stays,
        # so the common case keeps its single round trip.
        if attachments or attachment_urls:
            inline, large, info, err = await gather_attachments(
                attachment_urls, attachment_filename, attachments
            )
            if err:
                return {"success": False, "error": err}
            patch: Dict[str, Any] = dict(message)
            patch["body"] = {
                "contentType": "HTML" if is_html else "Text",
                "content": body,
            }
            sent_id = await reply_via_draft(
                _GRAPH_SCOPE_OWNER,
                credentials,
                email_id,
                reply_all,
                patch,
                inline,
                large,
                mailbox=mailbox,
            )
            return {
                "success": True,
                "email_id": email_id,
                "reply_all": reply_all,
                "message": f"Reply sent with {len(info)} attachment(s)",
                "message_id": sent_id,
                "attachments": info,
            }

        # Use comment for plain text, message.body for HTML — not both.
        # Extra cc/bcc go on the message object regardless.
        if is_html:
            message["body"] = {"contentType": "HTML", "content": body}
            json_data: Dict[str, Any] = {"message": message}
        else:
            json_data = {"comment": body}
            if message:
                json_data["message"] = message

        request, base = resolve_graph_scope(_GRAPH_SCOPE_OWNER, mailbox)
        await request(
            credentials,
            "POST",
            f"{base}/messages/{email_id}/{action}",
            json_data=json_data,
        )

        return {
            "success": True,
            "email_id": email_id,
            "reply_all": reply_all,
            "message": "Reply sent",
        }
    except Exception as e:
        logger.exception("reply_email_error", error=str(e))
        return graph_failure("Failed to reply to email", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me
@server.tool(
    name='search_emails',
    description="Search emails using KQL query syntax. Pass `query` (e.g. 'from:user@example.com', 'subject:report', 'hasAttachments:true'), optional `folder` to scope search, `max_results` (default: 20), and `skip` for pagination. Returns matching emails and `has_more` flag. If `from:`/`to:` is given a name instead of an address (no directory record for that person), the qualifier is automatically dropped and the name is searched as plain text instead - Graph's from:/to: only ever matches real addresses, so passing a name there directly would otherwise silently return nothing. A `note` field says when this happened.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string', 'description': 'KQL search query'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max results (default: 20, max: 100)',
                                    'default': 20},
                    'folder': {'type': 'string',
                               'description': 'Folder to search in (default: all folders)',
                               'enum': ['inbox',
                                        'drafts',
                                        'sentitems',
                                        'deleteditems',
                                        'junkemail',
                                        'archive',
                                        'outbox']},
                    'skip': {'type': 'integer',
                             'description': 'Number of items to skip for pagination '
                                            '(default: 0)',
                             'default': 0},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Email dates are returned in "
                                                "it. Pass the user's own zone; it falls "
                                                'back to UTC when unknown or invalid.'}},
     'required': ['query']})
async def search_emails(credentials: Dict[str, str],
    query: str,
    max_results: int = 20,
    folder: Optional[str] = None,
    skip: int = 0,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Search emails by query."""
    try:
        if not query or not query.strip():
            return {"success": False, "error": "Search query cannot be empty"}

        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        skip = max(skip, 0)
        # Sanitize query to prevent OData injection
        query = query.replace('"', "").replace("\\", "")

        if _BRACKET_PLACEHOLDER_RE.search(query):
            return {
                "success": False,
                "error": (
                    "Search query contains a bracketed placeholder like "
                    "'[last 90 days]'. Microsoft Graph does not understand "
                    "this as a date range - it silently matches nothing. "
                    "Resolve it to a real date first (e.g. 'received>=2026-06-05') "
                    "and search again."
                ),
            }

        if _ME_TOKEN_RE.search(query):
            my_address = ""
            try:
                me = await _graph_request(
                    credentials, "GET", "/me", params={"$select": "mail,userPrincipalName"}
                )
                my_address = (me.get("mail") or me.get("userPrincipalName") or "").strip()
            except GraphAuthError:
                raise  # not connected, not a 'me' problem: graph_failure below says so
            except Exception:
                pass
            if my_address:
                query = _ME_TOKEN_RE.sub(lambda m: f"{m.group(1)}:{my_address}", query)
            else:
                return {
                    "success": False,
                    "error": (
                        "Search query uses 'me' (e.g. 'from:me'). Microsoft Graph "
                        "does not recognize 'me' as the signed-in user - it silently "
                        "matches nothing. Call get_my_profile first and use the "
                        "returned email address instead of 'me', then search again."
                    ),
                }

        name_search_note = None
        stripped_query = _NAME_QUALIFIER_RE.sub(lambda m: m.group(1), query)
        if stripped_query != query:
            name_search_note = (
                "The query targeted a name rather than an email address, so it "
                "ran as a plain text search (matches sender/recipient display "
                "names, subject and body) instead of an exact from:/to: match."
            )
            query = stripped_query

        params: Dict[str, Any] = {
            "$search": f'"{query}"',
            "$top": max_results,
            "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview,hasAttachments",
        }
        if skip > 0:
            params["$skip"] = skip
        # Only use folder-scoped endpoint for valid well-known folder names
        folder_clean = (folder or "").strip().lower()
        endpoint = (
            f"/me/mailFolders/{folder_clean}/messages"
            if folder_clean in VALID_FOLDERS
            else "/me/messages"
        )

        result = await _graph_request(credentials, "GET", endpoint, params=params)
        messages = result.get("value", [])

        response: Dict[str, Any] = {
            "success": True,
            "count": len(messages),
            "query": query,
            "folder": folder or "all",
            "has_more": "@odata.nextLink" in result,
            "emails": [
                {
                    "id": m.get("id"),
                    "subject": m.get("subject", "(No subject)"),
                    "from": _fmt_addr(m.get("from", {})),
                    "to": _fmt_addr_list(m.get("toRecipients")),
                    "date": _fmt_date(m.get("receivedDateTime"), timezone),
                    "is_read": m.get("isRead"),
                    "snippet": (m.get("bodyPreview") or "")[:150],
                    "has_attachments": m.get("hasAttachments"),
                }
                for m in messages
            ],
        }
        if name_search_note:
            response["note"] = name_search_note
        return response
    except Exception as e:
        logger.exception("search_emails_error", error=str(e))
        return graph_failure("Failed to search emails", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /me/messages/{draft_id}/send
@server.tool(
    name='send_draft',
    description="Send an existing draft email. Pass `draft_id` (from create_draft or list_drafts). No other parameters.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'draft_id': {'type': 'string',
                                 'description': 'Draft message ID returned by create_draft '
                                                'or list_drafts'}},
     'required': ['draft_id']})
async def send_draft(credentials: Dict[str, str],
    draft_id: str,
) -> Dict[str, Any]:
    """Send an existing draft."""
    try:
        err = validate_id(draft_id, "draft_id")
        if err:
            return {"success": False, "error": err}

        await _graph_request(
            credentials,
            "POST",
            f"/me/messages/{draft_id}/send",
        )

        return {
            "success": True,
            "draft_id": draft_id,
            "message": "Draft sent",
        }
    except Exception as e:
        logger.exception("send_draft_error", error=str(e))
        return graph_failure("Failed to send draft", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /me/sendMail (via send_via_sendmail())
@server.tool(
    name='send_email',
    description="Send an email. Pass `to` (comma-separated), `subject`, and `body` (required). Optional: `cc`, `bcc`, `is_html` (true for HTML body), `importance` (low/normal/high), `save_to_sent` (default: true), `attachment_urls` (array of file URLs, max 10, up to 25MB each, 50MB combined), `attachment_filename` (custom name for first URL attachment), and `attachments` (max 10; each item is a platform file_id/storage-key string, {\"file_id\": \"...\"}, or a pre-fetched {filename, content_base64, content_type} - pass a file_id as-is, the platform inlines the bytes before this tool sees them). `attachment_urls` and `attachments` share the same max-10-count and 50MB-combined limits. Attachments over 3MB require save_to_sent=true. Returns `message_id` (an immutable id for the sent message) when save_to_sent=true; it is null when save_to_sent=false (Graph provides no id for that path).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'to': {'type': 'string',
                           'description': 'Recipient(s), comma-separated'},
                    'subject': {'type': 'string', 'description': 'Email subject'},
                    'body': {'type': 'string',
                             'description': 'Email body (HTML supported)'},
                    'cc': {'type': 'string',
                           'description': 'CC recipients, comma-separated'},
                    'bcc': {'type': 'string',
                            'description': 'BCC recipients, comma-separated'},
                    'is_html': {'type': 'boolean',
                                'description': 'Body is HTML',
                                'default': False},
                    'importance': {'type': 'string',
                                   'description': 'Email importance level',
                                   'enum': ['low', 'normal', 'high'],
                                   'default': 'normal'},
                    'save_to_sent': {'type': 'boolean',
                                     'description': 'Save to Sent Items',
                                     'default': True},
                    'attachment_urls': {'type': 'array',
                                        'items': {'type': 'string'},
                                        'maxItems': 10,
                                        'description': 'File URLs to attach (max 10)'},
                    'attachment_filename': {'type': 'string',
                                            'description': 'Custom filename for the first '
                                                           "URL's downloaded file"},
                    'attachments': {'type': 'array',
                                    'items': {'anyOf': [{'type': 'string',
                                                         'description': 'Platform file_id '
                                                                        '(UUID) or storage '
                                                                        'key'},
                                                        {'type': 'object',
                                                         'properties': {'file_id': {'type': 'string'}},
                                                         'required': ['file_id']},
                                                        {'type': 'object',
                                                         'properties': {'filename': {'type': 'string'},
                                                                        'content_base64': {'type': 'string'},
                                                                        'content_type': {'type': 'string'}},
                                                         'required': ['content_base64']}]},
                                    'maxItems': 10,
                                    'description': 'Files to attach (max 10). Three '
                                                   'accepted forms per item: (1) a '
                                                   'platform file_id UUID or storage key '
                                                   'as a plain string, (2) {"file_id": '
                                                   '"<uuid-or-key>"}, or (3) a pre-fetched '
                                                   '{filename, content_base64, '
                                                   'content_type}. Forms 1 and 2 are '
                                                   'replaced with form 3 by the platform '
                                                   'before this tool runs, so pass the '
                                                   'identifier as-is: never put it in '
                                                   '`filename`, and never send an empty '
                                                   '`content_base64`. DO NOT ENCODE '
                                                   '`content_base64` YOURSELF. Form 3 '
                                                   'exists for bytes the platform has '
                                                   'already fetched, not for base64 you '
                                                   'write: a hand-encoded attachment '
                                                   'arrives as a file that opens and is '
                                                   'silently corrupt. If you hold no '
                                                   'file_id or storage key, send the mail '
                                                   'without an attachment and say so - do '
                                                   'not improvise one, and do not retype '
                                                   'data from the conversation into a '
                                                   'file.'}},
     'required': ['to', 'subject', 'body']})
async def send_email(credentials: Dict[str, str],
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    is_html: bool = False,
    importance: str = "normal",
    save_to_sent: bool = True,
    attachment_urls: Optional[List[str]] = None,
    attachment_filename: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Send an email with optional attachments.

    When save_to_sent is True (default) the message is created as a draft
    (with an immutable id) and then sent, so a durable `message_id` can be
    returned. When save_to_sent is False it goes via /me/sendMail and no id
    is available, so `message_id` is null.
    """
    try:
        error = validate_send_inputs(subject, body, importance)
        if error:
            return {"success": False, "error": error}

        error, message_body = build_message_body(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "is_html": is_html,
                "importance": importance,
                "cc": cc,
                "bcc": bcc,
            }
        )
        if error or message_body is None:
            return {"success": False, "error": error or "Invalid message"}

        (
            inline_attachments,
            large_attachments,
            attachments_info,
            attach_error,
        ) = await gather_attachments(attachment_urls, attachment_filename, attachments)
        if attach_error:
            return {"success": False, "error": attach_error}

        if inline_attachments:
            message_body["attachments"] = inline_attachments

        # Attachments over the inline limit require a draft-then-send flow,
        # which always saves to Sent Items - so save_to_sent=false can't be
        # honored alongside them.
        if large_attachments and not save_to_sent:
            return {
                "success": False,
                "error": (
                    "save_to_sent=false is not supported with attachments over "
                    f"{INLINE_ATTACHMENT_LIMIT // (1024 * 1024)}MB"
                ),
            }

        try:
            if save_to_sent:
                message_id = await send_via_draft(
                    _GRAPH_SCOPE_OWNER, credentials, message_body, large_attachments
                )
            else:
                message_id = await send_via_sendmail(_GRAPH_SCOPE_OWNER, credentials, message_body)
        except (ValueError, httpx.HTTPError) as e:
            return {"success": False, "error": f"Failed to send email: {e}"}

        result: Dict[str, Any] = {
            "success": True,
            "to": to,
            "subject": subject,
            "message_id": message_id,
            "message": "Email sent successfully",
        }
        if attachments_info:
            result["attachments"] = attachments_info
            result["message"] = f"Email sent with {len(attachments_info)} attachment(s)"

        logger.info("send_email_sent", message_id=message_id, save_to_sent=save_to_sent)
        return result
    except Exception as e:
        logger.exception("send_email_error", error=str(e))
        return graph_failure("Failed to send email", e)



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: DELETE /subscriptions/{id}. A plain DELETE marked
# non-destructive, because the value was proposed from a name that says
# "stop" rather than "delete".
# Evidence: DELETE, path built at runtime
@server.tool(
    name='stop_watching_mailbox',
    description="Delete a mailbox subscription. Pass `subscription_id`. Already-deleted subscriptions are treated as success.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'subscription_id': {'type': 'string',
                                        'description': 'Subscription id to delete'}},
     'required': ['subscription_id'],
     'additionalProperties': False})
async def stop_watching_mailbox(credentials: Dict[str, str], subscription_id: str
) -> Dict[str, Any]:
    """Delete a mailbox subscription."""
    try:
        err = validate_id(subscription_id, "subscription_id")
        if err:
            return {"success": False, "error": err}

        await _graph_request_app_only(
            credentials, "DELETE", f"/subscriptions/{subscription_id.strip()}"
        )
        return {"success": True}
    except GraphNotFoundError:
        return {"success": True, "already_removed": True}
    except Exception as e:
        logger.exception("stop_watching_mailbox_error", error=str(e))
        return {"success": False, "error": str(e)}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /subscriptions
@server.tool(
    name='watch_mailbox',
    description="Create a Microsoft Graph change-notification subscription on a mailbox's inbox, so new mail triggers `notification_url`. Pass `mailbox` (must be in this connection's allowed_mailboxes), `notification_url` (must be publicly reachable HTTPS), and `client_state` (a secret the caller generates and later verifies on incoming notifications). Returns subscription_id and expiration_date_time.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Allowed-Mailboxes'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'mailbox': {'type': 'string',
                                'description': 'Mailbox address to watch'},
                    'notification_url': {'type': 'string',
                                         'description': 'Public HTTPS URL Graph sends '
                                                        'notifications to'},
                    'client_state': {'type': 'string',
                                     'description': 'Anti-forgery secret Graph echoes back '
                                                    'on every notification'},
                    'expiration_hours': {'type': 'integer',
                                         'description': 'Subscription lifetime in hours '
                                                        '(default: 70, Graph caps at '
                                                        '~70.5h for mail)',
                                         'default': 70}},
     'required': ['mailbox', 'notification_url', 'client_state'],
     'additionalProperties': False})
async def watch_mailbox(credentials: Dict[str, str],
    mailbox: str,
    notification_url: str,
    client_state: str,
    expiration_hours: int = _DEFAULT_EXPIRATION_HOURS,
) -> Dict[str, Any]:
    """Create a subscription watching a mailbox's inbox."""
    try:
        if not mailbox or not EMAIL_RE.match(mailbox.strip()):
            return {"success": False, "error": "A valid mailbox address is required"}
        err = check_mailbox_allowed(credentials, mailbox)
        if err:
            return {"success": False, "error": err}
        if not notification_url.startswith("https://"):
            return {"success": False, "error": "notification_url must be HTTPS"}
        err = _validate_expiration_hours(expiration_hours)
        if err:
            return {"success": False, "error": err}

        resource = f"users/{mailbox.strip()}/mailFolders('inbox')/messages"
        result = await _graph_request_app_only(
            credentials,
            "POST",
            "/subscriptions",
            json_data={
                "changeType": "created",
                "notificationUrl": notification_url,
                "resource": resource,
                "expirationDateTime": _expiration_iso(expiration_hours),
                "clientState": client_state,
            },
        )

        return {
            "success": True,
            "subscription_id": result.get("id"),
            "expiration_date_time": result.get("expirationDateTime"),
            "resource": resource,
            "notification_url": notification_url,
        }
    except Exception as e:
        logger.exception("watch_mailbox_error", error=str(e))
        return {"success": False, "error": str(e)}
