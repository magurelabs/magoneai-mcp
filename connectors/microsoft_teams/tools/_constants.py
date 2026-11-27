"""Shared constants and helpers for Microsoft Teams tools."""

import base64
import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .._shared import graph_link_matches

CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# Graph caps $top at 50 on chat/channel message endpoints.
MAX_RESULTS_CAP = 50

# Hard ceiling on the flattened roots+replies list read_channel_messages
# returns. $top caps roots only; an expanded reply chain rides along on top of
# it, so without this one call can flood the agent's context.
MAX_FLATTENED_MESSAGES = 100
# Graph rejects chatMessage payloads around ~28KB; stay under with headroom.
MAX_MESSAGE_SIZE = 25 * 1024
MAX_SUBJECT_LEN = 255
MAX_ID_LENGTH = 500
# One call should never be able to notify a whole tenant.
MAX_MENTIONS = 10

VALID_CONTENT_TYPES = ["text", "html"]

# Shared input-schema fragment for the three message-sending tools. The description is
# the only documentation the calling model gets, so it must say where the id comes from.
MENTIONS_SCHEMA = {
    "type": "array",
    "description": (
        'People to @-mention. Each item is {"id": "<Entra user id>", "name": '
        '"Display Name"}. Get ids from list_chats members for a chat, or '
        "list_channel_members for a channel. Forces HTML formatting."
    ),
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["id", "name"],
    },
}

_TAG_RE = re.compile(r"<[^>]+>")
_AT_TAG_RE = re.compile(r'<at\s+id="(\d+)"', re.IGNORECASE)
# The whole element, so a caller-placed tag can be carried through escaping intact.
_AT_TAG_FULL_RE = re.compile(r'<at\s+id="\d+"\s*>.*?</at>', re.IGNORECASE | re.DOTALL)


def validate_id(value: str, label: str = "ID") -> Optional[str]:
    """Validate an ID used in URL paths. Returns error string or None on success."""
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if "/" in value or ".." in value:
        return f"{label} contains invalid characters"
    if len(value) > MAX_ID_LENGTH:
        return f"{label} is too long (max {MAX_ID_LENGTH} chars)"
    return None


def validate_message_content(content: str, content_type: str) -> Optional[str]:
    """Validate message content/type. Returns error string or None on success."""
    if not content or not content.strip():
        return "Message content cannot be empty"
    if len(content.encode("utf-8")) > MAX_MESSAGE_SIZE:
        return f"Message content exceeds {MAX_MESSAGE_SIZE // 1024}KB limit"
    if content_type not in VALID_CONTENT_TYPES:
        return f"content_type must be one of: {', '.join(VALID_CONTENT_TYPES)}"
    return None


def validate_mentions(mentions: Optional[List[Dict[str, str]]]) -> Optional[str]:
    """Validate an @-mention list. Returns error string or None on success.

    A mention that cannot be built is an error rather than a silent drop: a message
    that was meant to ping somebody and did not is a failure worth reporting.
    """
    if not mentions:
        return None
    if not isinstance(mentions, list):
        return "mentions must be a list of {id, name} objects"
    if len(mentions) > MAX_MENTIONS:
        return f"Too many mentions (max {MAX_MENTIONS})"
    for i, mention in enumerate(mentions):
        if not isinstance(mention, dict):
            return f"mentions[{i}] must be an object with id and name"
        if not str(mention.get("id") or "").strip():
            return f"mentions[{i}] is missing id (the Entra user id from list_chats members)"
        if not str(mention.get("name") or "").strip():
            return f"mentions[{i}] is missing name (the display name to render)"
    return None


def _text_to_html(content: str) -> str:
    """Promote a plain-text body to HTML without losing it.

    Two things have to survive the promotion, and the first version lost both.

    Newlines: HTML collapses them, so a multi-line message ("Standup:\n- one\n
    - two") arrived as one run-on line the moment a mention was added. A message
    is reformatted by adding a mention to it, which is not something the sender
    asked for.

    Caller-placed <at> tags: everything else must be escaped, but those are
    markup the caller meant. Splitting on them escapes the text around each tag
    and leaves the tag itself alone, rather than escaping the whole body and
    hoping.
    """
    out = []
    last = 0
    for match in _AT_TAG_FULL_RE.finditer(content):
        out.append(html_lib.escape(content[last : match.start()], quote=False))
        out.append(match.group(0))
        last = match.end()
    out.append(html_lib.escape(content[last:], quote=False))
    return "".join(out).replace("\r\n", "\n").replace("\n", "<br>")


def build_message_payload(
    content: str,
    content_type: str,
    mentions: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Build a Graph chatMessage payload from plain text or HTML content.

    `mentions` is [{"id": "<entra-object-id>", "name": "Display Name"}, ...]. Each one
    becomes an <at> tag in the body and an entry in the mentions array; Graph pairs them
    by index, and an <at> tag with no matching entry renders as plain text rather than
    failing, which is why the two are built together here and never separately.

    Raises ValueError if the built payload exceeds MAX_MESSAGE_SIZE — the <at> markup
    and the escaping both grow a body that was under the limit before.
    """
    if not mentions:
        return {"body": {"contentType": content_type, "content": content}}

    # Which tags the caller placed has to be read from the RAW body. Reading it
    # after escaping finds nothing, because the tag is no longer a tag by then,
    # so a caller-placed mention got a second one prepended and the original
    # went out as visible &lt;at&gt; text.
    placed = {int(m) for m in _AT_TAG_RE.findall(content)}

    # <at> in a text body is escaped and shown literally, so mentions force html.
    if content_type != "html":
        content = _text_to_html(content)
    entries = []
    leading = []
    for index, mention in enumerate(mentions):
        name = str(mention["name"]).strip()
        if index not in placed:
            leading.append(f'<at id="{index}">{html_lib.escape(name, quote=False)}</at>')
        entries.append(
            {
                "id": index,
                "mentionText": name,
                "mentioned": {
                    "user": {
                        "id": str(mention["id"]).strip(),
                        "displayName": name,
                        "userIdentityType": "aadUser",
                    }
                },
            }
        )

    if leading:
        content = " ".join(leading) + " " + content

    payload = {
        "body": {"contentType": "html", "content": content},
        "mentions": entries,
    }
    if len(content.encode("utf-8")) > MAX_MESSAGE_SIZE:
        raise ValueError(
            f"Message content exceeds {MAX_MESSAGE_SIZE // 1024}KB limit once mentions are added"
        )
    return payload


def strip_html(content: str) -> str:
    """Reduce a Teams HTML message body to readable plain text."""
    text = _TAG_RE.sub(" ", content)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def simplify_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a Graph chatMessage into the fields agents actually need."""
    body = msg.get("body") or {}
    sender = msg.get("from") or {}
    user = sender.get("user") or {}
    application = sender.get("application") or {}
    content = body.get("content") or ""
    if body.get("contentType") == "html":
        content = strip_html(content)

    return {
        "id": msg.get("id"),
        "from": user.get("displayName") or application.get("displayName"),
        "from_user_id": user.get("id"),
        "created_at": msg.get("createdDateTime"),
        "content": content,
        "importance": msg.get("importance"),
        "web_url": msg.get("webUrl"),
    }


def is_user_message(msg: Dict[str, Any]) -> bool:
    """True for real user/app messages; False for system events (member added, etc.)."""
    return msg.get("messageType") == "message" and not msg.get("deletedDateTime")


def is_listed(msg: Dict[str, Any], include_system: bool) -> bool:
    """Whether a paged tool returns `msg`.

    Unlike `is_user_message`, a deleted message is kept: a full history wants
    the tombstone (`deletedDateTime` set) rather than a silent gap. System
    events are kept only on request.
    """
    return include_system or msg.get("messageType") == "message"


# -- paging -------------------------------------------------------------

# Graph's own default page size for channel messages; its maximum is 50.
DEFAULT_PAGE_SIZE = 20
# A cursor wraps a Graph nextLink, which carries a $skiptoken of a few hundred
# characters. Anything far past that is not a cursor this server issued.
MAX_CURSOR_LENGTH = 8192
# Graph returns system events only when asked to include evolvable enum members:
# https://learn.microsoft.com/en-us/graph/api/resources/chatmessage
INCLUDE_SYSTEM_PREFER = "include-unknown-enum-members"


def encode_cursor(link: str, **state: Any) -> str:
    """Wrap a Graph `@odata.nextLink` (and any paging state) as an opaque cursor.

    Graph says to use the whole nextLink rather than lift its $skiptoken out
    (https://learn.microsoft.com/en-us/graph/paging), so the link itself is the
    cursor. The state rides along so a later page applies the same stop bound
    as the first without the caller having to resend it.
    """
    raw = json.dumps({"link": link, **state}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, resource_path: str) -> Dict[str, Any]:
    """Unwrap a cursor, refusing one that is not for `resource_path`.

    The link is sent with the bearer token, so it must be a Graph URL, and it
    must name the resource the rest of the call names (a cursor from one chat
    cannot page another). Raises ValueError for anything else.
    """
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("cursor is not a cursor this tool returned")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        state = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError):
        raise ValueError("cursor is not a cursor this tool returned") from None
    link = state.get("link") if isinstance(state, dict) else None
    if not isinstance(link, str) or not graph_link_matches(link, resource_path):
        raise ValueError(
            "cursor does not belong to this chat, channel or message; "
            "start again without a cursor"
        )
    return state


def parse_utc(value: str, label: str) -> datetime:
    """An ISO 8601 instant as an aware UTC datetime. No offset means UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an ISO 8601 datetime, e.g. 2025-11-01T00:00:00Z")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{label} must be an ISO 8601 datetime, e.g. 2025-11-01T00:00:00Z"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def graph_utc(value: datetime) -> str:
    """The literal Graph's $filter takes for a dateTimeOffset."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def message_time(msg: Dict[str, Any], field: str) -> Optional[datetime]:
    """A message's `createdDateTime` / `lastModifiedDateTime` as UTC, or None."""
    value = msg.get(field)
    if not value:
        return None
    try:
        return parse_utc(value, field)
    except ValueError:
        return None


def parse_attendees(raw: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Parse comma-separated emails into a Graph event attendee list.

    Returns (error_message, attendees). error_message is None on success.
    """
    addresses = [a.strip() for a in raw.split(",") if a.strip()]
    for addr in addresses:
        if not EMAIL_RE.match(addr):
            return f"Invalid attendee address: {addr}", []
    return None, [{"emailAddress": {"address": a}, "type": "required"} for a in addresses]
