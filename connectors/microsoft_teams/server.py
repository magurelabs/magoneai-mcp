"""The microsoft-teams connector.

Ported from the v1 mixin form by a codemod.

15 tools, 11 of them read-only. The 12 ported tools carry their input schema
across verbatim via `declare_schema` rather than derived from type hints, so
the wire contract is byte-identical to v1 and no live agent sees a changed
parameter schema. The three paged read tools added since
(`list_chat_messages`, `list_channel_messages`, `list_channel_message_replies`)
declare theirs the same way, so the golden gate covers them too.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

import base64
import json
from datetime import timedelta, timezone
from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

from ._shared import (
    GRAPH_API_URL,
    GraphAuthError,
    GraphForbiddenError,
    GraphNotFoundError,
    GraphRetryLaterError,
    _get_access_token,
    _graph_request,
    graph_link_matches,
)
from .tools._constants import (
    DEFAULT_PAGE_SIZE,
    INCLUDE_SYSTEM_PREFER,
    MAX_CURSOR_LENGTH,
    decode_cursor,
    encode_cursor,
    graph_utc,
    is_listed,
    message_time,
    parse_utc,
)

SERVER_NAME = "microsoft-teams"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

from .tools._constants import CREDENTIAL_HEADERS
import html as html_lib
import re
from typing import Any, Dict, List, Optional, Tuple
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from .tools._constants import CREDENTIAL_HEADERS, MAX_RESULTS_CAP, MENTIONS_SCHEMA, VALID_CONTENT_TYPES
from datetime import datetime
from .tools._constants import CREDENTIAL_HEADERS, EMAIL_RE, MAX_RESULTS_CAP, MENTIONS_SCHEMA, VALID_CONTENT_TYPES
from typing import Any, Dict
from .tools._constants import CREDENTIAL_HEADERS, MAX_FLATTENED_MESSAGES, MAX_RESULTS_CAP
from .tools._constants import CREDENTIAL_HEADERS, MAX_SUBJECT_LEN


CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

MAX_RESULTS_CAP = 50

MAX_MESSAGE_SIZE = 25 * 1024

MAX_SUBJECT_LEN = 255

MAX_ID_LENGTH = 500

MAX_MENTIONS = 10

VALID_CONTENT_TYPES = ["text", "html"]

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

def parse_attendees(raw: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Parse comma-separated emails into a Graph event attendee list.

    Returns (error_message, attendees). error_message is None on success.
    """
    addresses = [a.strip() for a in raw.split(",") if a.strip()]
    for addr in addresses:
        if not EMAIL_RE.match(addr):
            return f"Invalid attendee address: {addr}", []
    return None, [{"emailAddress": {"address": a}, "type": "required"} for a in addresses]

_MEMBER_BIND = "https://graph.microsoft.com/v1.0/users('{user_id}')"

_MAX_UNREAD_CHATS = 20

def _parse_graph_dt(value: str) -> Optional[datetime]:
    """Parse a Graph ISO-8601 timestamp; None if missing/unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

async def _resolve_one_on_one_chat(credentials: Dict[str, str],
    user_email: str,
    access_token: str,
) -> str:
    """Find or create the 1:1 chat with `user_email`, returning its chat id.

    Graph's POST /chats with chatType oneOnOne is idempotent: it returns
    the existing chat if one already exists between the two users.
    """
    target = await _graph_request(
        credentials,
        "GET",
        f"/users/{user_email}",
        params={"$select": "id,displayName"},
        access_token=access_token,
    )
    me = await _graph_request(
        credentials,
        "GET",
        "/me",
        params={"$select": "id"},
        access_token=access_token,
    )
    chat = await _graph_request(
        credentials,
        "POST",
        "/chats",
        json_data={
            "chatType": "oneOnOne",
            "members": [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": _MEMBER_BIND.format(user_id=me["id"]),
                },
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": _MEMBER_BIND.format(user_id=target["id"]),
                },
            ],
        },
        access_token=access_token,
    )
    return chat["id"]

_MAX_PEOPLE_RESULTS = 25

def _validate_datetime(value: str, label: str) -> str:
    """Return an error string if `value` is not an ISO 8601 datetime, else ''."""
    try:
        datetime.fromisoformat(value)
        return ""
    except (TypeError, ValueError):
        return f"{label} must be an ISO 8601 datetime (e.g. 2026-07-24T15:00:00)"


def _paged_refusal(action: str, exc: Exception) -> Dict[str, Any]:
    """The refusal a paged read tool returns, with a stable `error_code`.

    A caller paging a whole history has to tell "wait and retry this page"
    from "stop, the token is dead" without parsing prose, and the code is how.
    """
    if isinstance(exc, GraphRetryLaterError):
        code = "throttled" if exc.status == 429 else "unavailable"
        out: Dict[str, Any] = {"success": False, "error": f"{action}: {exc}", "error_code": code}
        if exc.retry_after is not None:
            out["retry_after_seconds"] = exc.retry_after
        return out
    if isinstance(exc, GraphAuthError):
        code = "auth_expired"
    elif isinstance(exc, GraphForbiddenError):
        code = "forbidden"
    elif isinstance(exc, GraphNotFoundError):
        code = "not_found"
    elif isinstance(exc, httpx.HTTPError):
        code = "network_error"
    else:
        code = "upstream_error"
    return {"success": False, "error": f"{action}: {exc}", "error_code": code}


def _invalid(message: str, code: str = "invalid_argument") -> Dict[str, Any]:
    return {"success": False, "error": message, "error_code": code}


def _page_size(value: int) -> int:
    return min(max(int(value), 1), MAX_RESULTS_CAP)


def _next_cursor(result: Dict[str, Any], resource: str, **state: Any) -> Optional[str]:
    """The cursor for Graph's next page, or None on the last one.

    A link Graph returns for some other path is refused rather than dropped:
    dropping it would end the history early and report it complete.
    """
    link = result.get("@odata.nextLink")
    if not link:
        return None
    if not graph_link_matches(link, resource):
        raise ValueError("Graph returned a next-page link for a different resource")
    return encode_cursor(link, **state)


# Shared output-schema fragments for the paged read tools.
_PAGED_COMMON_OUTPUT = {
    "success": {"type": "boolean"},
    "count": {"type": "integer", "description": "Items on this page."},
    "next_cursor": {
        "type": ["string", "null"],
        "description": "Pass as `cursor` for the next page; null when there is none.",
    },
    "complete": {"type": "boolean", "description": "True when there are no more pages."},
    "error": {"type": "string"},
    "error_code": {
        "type": "string",
        "description": (
            "On failure: invalid_argument, invalid_cursor, auth_expired, forbidden, "
            "not_found, throttled, unavailable, network_error or upstream_error."
        ),
    },
    "retry_after_seconds": {
        "type": "number",
        "description": "On throttled or unavailable: seconds Microsoft Graph asked to wait.",
    },
}
_GRAPH_MESSAGE_ITEMS = {
    "type": "array",
    "items": {"type": "object"},
    "description": "Microsoft Graph chatMessage objects as Graph returned them.",
}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /me/events
@server.tool(
    name='create_meeting',
    description="Create a Teams meeting as a calendar event on the connected user's calendar, sending invites to attendees. Pass `subject`, `start`, and `end` (ISO 8601 datetimes, required). Optional: `timezone` (IANA or Windows name, default: UTC), `attendees` (comma-separated emails), `body` (agenda text). Returns event_id, join_url, and web_link.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'subject': {'type': 'string', 'description': 'Meeting title'},
                    'start': {'type': 'string',
                              'description': 'Start datetime, ISO 8601 (e.g. '
                                             '2026-07-24T15:00:00)'},
                    'end': {'type': 'string', 'description': 'End datetime, ISO 8601'},
                    'timezone': {'type': 'string',
                                 'description': 'Timezone for start/end (default: UTC)',
                                 'default': 'UTC'},
                    'attendees': {'type': 'string',
                                  'description': 'Attendee emails, comma-separated'},
                    'body': {'type': 'string',
                             'description': 'Agenda / meeting description'}},
     'required': ['subject', 'start', 'end']})
async def create_meeting(credentials: Dict[str, str],
    subject: str,
    start: str,
    end: str,
    timezone: str = "UTC",
    attendees: str = "",
    body: str = "",
) -> Dict[str, Any]:
    """Create an online Teams meeting via a calendar event."""
    try:
        if not subject or not subject.strip():
            return {"success": False, "error": "Subject is required"}
        if len(subject) > MAX_SUBJECT_LEN:
            return {
                "success": False,
                "error": f"Subject is too long (max {MAX_SUBJECT_LEN} chars)",
            }
        for value, label in ((start, "start"), (end, "end")):
            error = _validate_datetime(value, label)
            if error:
                return {"success": False, "error": error}

        attendee_list = []
        if attendees.strip():
            error, attendee_list = parse_attendees(attendees)
            if error:
                return {"success": False, "error": error}

        event: Dict[str, Any] = {
            "subject": subject.strip(),
            "start": {"dateTime": start, "timeZone": timezone or "UTC"},
            "end": {"dateTime": end, "timeZone": timezone or "UTC"},
            "isOnlineMeeting": True,
            "onlineMeetingProvider": "teamsForBusiness",
        }
        if attendee_list:
            event["attendees"] = attendee_list
        if body.strip():
            event["body"] = {"contentType": "text", "content": body}

        result = await _graph_request(
            credentials,
            "POST",
            "/me/events",
            json_data=event,
        )
        online_meeting = result.get("onlineMeeting") or {}
        return {
            "success": True,
            "event_id": result.get("id"),
            "join_url": online_meeting.get("joinUrl"),
            "web_link": result.get("webLink"),
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to create meeting: {e}"}
    except Exception as e:
        logger.exception("create_meeting_error", error=str(e))
        return {"success": False, "error": "Failed to create meeting"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/chats
@server.tool(
    name='get_unread_messages',
    description="Get unread messages across the connected user's 1:1 and group Teams chats (channel messages have no unread tracking in Microsoft Graph). Optional: `max_chats` (default: 10, max: 20), `max_messages_per_chat` (default: 20, max: 50). Returns unread chats with members as {id, name} objects (the id is the Entra user id needed to @-mention that person), last_read_at, and the unread messages oldest-first. Reading via this tool does NOT mark chats as read.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'max_chats': {'type': 'integer',
                                  'description': 'Max unread chats to return (default: 10, '
                                                 'max: 20)',
                                  'default': 10},
                    'max_messages_per_chat': {'type': 'integer',
                                              'description': 'Max unread messages per chat '
                                                             '(default: 20, max: 50)',
                                              'default': 20}}})
async def get_unread_messages(credentials: Dict[str, str],
    max_chats: int = 10,
    max_messages_per_chat: int = 20,
) -> Dict[str, Any]:
    """Find chats with unread activity and return the unread messages in each."""
    try:
        max_chats = min(max(max_chats, 1), _MAX_UNREAD_CHATS)
        max_messages_per_chat = min(max(max_messages_per_chat, 1), MAX_RESULTS_CAP)

        access_token = await _get_access_token(credentials)

        result = await _graph_request(
            credentials,
            "GET",
            "/me/chats",
            params={
                "$top": MAX_RESULTS_CAP,
                "$expand": "members,lastMessagePreview",
                "$orderby": "lastMessagePreview/createdDateTime desc",
            },
            access_token=access_token,
        )

        unread_chats = []
        for chat in result.get("value", []):
            last_read = _parse_graph_dt(
                (chat.get("viewpoint") or {}).get("lastMessageReadDateTime", "")
            )
            last_message = _parse_graph_dt(
                (chat.get("lastMessagePreview") or {}).get("createdDateTime", "")
            )
            if last_read and last_message and last_message > last_read:
                unread_chats.append((chat, last_read))
            if len(unread_chats) >= max_chats:
                break

        me = await _graph_request(
            credentials,
            "GET",
            "/me",
            params={"$select": "id"},
            access_token=access_token,
        )
        my_id = me.get("id")

        chats = []
        for chat, last_read in unread_chats:
            page = await _graph_request(
                credentials,
                "GET",
                f"/chats/{chat['id']}/messages",
                params={"$top": MAX_RESULTS_CAP},
                access_token=access_token,
            )
            page_messages = page.get("value", [])
            unread = []
            reached_watermark = False
            for msg in page_messages:  # newest first
                created = _parse_graph_dt(msg.get("createdDateTime", ""))
                if created and created <= last_read:
                    reached_watermark = True
                    break
                if not is_user_message(msg):
                    continue
                sender_id = ((msg.get("from") or {}).get("user") or {}).get("id")
                if sender_id and sender_id == my_id:
                    continue
                unread.append(msg)
            if not unread:
                continue
            unread = unread[:max_messages_per_chat]
            chats.append(
                {
                    "chat_id": chat.get("id"),
                    "topic": chat.get("topic"),
                    "chat_type": chat.get("chatType"),
                    # Same shape as list_chats. These were bare strings
                    # while list_chats returned objects, so one field
                    # meant two things in one server: read-unread then
                    # reply-and-tag-the-sender had no id to use, and any
                    # caller that had learned the string form broke
                    # silently on whichever call it happened to make.
                    "members": [
                        {"id": m.get("userId"), "name": m.get("displayName")}
                        for m in chat.get("members", [])
                        if m.get("displayName")
                    ],
                    "last_read_at": (chat.get("viewpoint") or {}).get(
                        "lastMessageReadDateTime"
                    ),
                    "unread_count": len(unread),
                    "may_have_more": not reached_watermark
                    and len(page_messages) >= MAX_RESULTS_CAP,
                    "messages": [simplify_message(m) for m in reversed(unread)],
                }
            )

        return {
            "success": True,
            "unread_chat_count": len(chats),
            "chats": chats,
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to get unread messages: {e}"}
    except Exception as e:
        logger.exception("get_unread_messages_error", error=str(e))
        return {"success": False, "error": "Failed to get unread messages"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /teams/{team_id}/channels/{channel_id}/members
@server.tool(
    name='list_channel_members',
    description="List the people in a Teams channel, as {id, name, email} objects: the id is the Entra user id needed to @-mention that person, and the email is their address, for inviting them to a meeting or writing to them. Pass `team_id` and `channel_id` (both from list_teams/list_channels). For a standard channel this is the whole team roster, which is what a channel mention resolves against anyway.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string', 'description': 'Team ID from list_teams'},
                    'channel_id': {'type': 'string',
                                   'description': 'Channel ID from list_channels'}},
     'required': ['team_id', 'channel_id']})
async def list_channel_members(credentials: Dict[str, str],
    team_id: str,
    channel_id: str,
) -> Dict[str, Any]:
    """List a channel's members with the ids a mention binds to.

    Without this, `send_channel_message` and `reply_to_channel_message`
    advertised a `mentions` parameter with nowhere to get an id from:
    `list_teams` returns id/name/description and `list_channels` adds only
    membership_type, so a model could see the parameter, want to use it,
    and have no way to fill it. A parameter that cannot be satisfied is
    worse than an absent one, because the model tries.
    """
    try:
        for value, label in ((team_id, "team_id"), (channel_id, "channel_id")):
            error = validate_id(value, label)
            if error:
                return {"success": False, "error": error}

        result = await _graph_request(
            credentials,
            "GET",
            f"/teams/{team_id}/channels/{channel_id}/members",
        )
        members = result.get("value", [])
        return {
            "success": True,
            "count": len(members),
            # Same shape as list_chats and get_unread_messages: one field,
            # one meaning, everywhere in this server.
            # `email` comes back on aadUserConversationMember and was
            # being dropped here. Without it a caller can find a person
            # and still not be able to invite them: an assistant reported
            # "found, but no email address available there" for two
            # colleagues it had just located, because the address arrived from Graph and
            # was discarded three lines before it was needed.
            "members": [
                {
                    "id": m.get("userId"),
                    "name": m.get("displayName"),
                    "email": m.get("email"),
                }
                for m in members
                if m.get("displayName")
            ],
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to list channel members: {e}"}
    except Exception as e:
        logger.exception("list_channel_members_error", error=str(e))
        return {"success": False, "error": "Failed to list channel members"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies
@server.tool(
    name='list_channel_message_replies',
    description="Page through every reply to one Teams channel message, one page per call. Use it for a thread whose replies list_channel_messages could not return inline (it then gives a `replies_cursor`), or to read a whole thread. Pass `team_id`, `channel_id` and `message_id` (the root message id, required). Optional: `page_size` (default: 20, max: 50), `include_system_messages` (default: false), `cursor` (a `replies_cursor` from list_channel_messages or a `next_cursor` from this tool). Returns raw Microsoft Graph chatMessage objects in the order Graph returns them; deleted replies are kept with deletedDateTime set. Pass `next_cursor` back as `cursor` until `complete` is true.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_output_schema({'type': 'object',
     'properties': {**_PAGED_COMMON_OUTPUT, 'replies': _GRAPH_MESSAGE_ITEMS}})
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string', 'description': 'Team ID from list_teams'},
                    'channel_id': {'type': 'string',
                                   'description': 'Channel ID from list_channels'},
                    'message_id': {'type': 'string',
                                   'description': 'Root message ID from list_channel_messages '
                                                  'or read_channel_messages'},
                    'page_size': {'type': 'integer',
                                  'description': 'Replies per page (default: 20, max: 50)',
                                  'default': 20},
                    'include_system_messages': {'type': 'boolean',
                                                'description': 'Also return system events '
                                                               '(default: false)',
                                                'default': False},
                    'cursor': {'type': 'string',
                               'description': 'replies_cursor from list_channel_messages, or '
                                              'next_cursor from this tool. Its settings carry '
                                              'over.'}},
     'required': ['team_id', 'channel_id', 'message_id']})
async def list_channel_message_replies(credentials: Dict[str, str],
    team_id: str,
    channel_id: str,
    message_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    include_system_messages: bool = False,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """One page of a channel message's replies."""
    for value, label in ((team_id, "team_id"), (channel_id, "channel_id"), (message_id, "message_id")):
        error = validate_id(value, label)
        if error:
            return _invalid(error)
    resource = f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies"
    include_system = include_system_messages
    link = None
    if cursor:
        try:
            state = decode_cursor(cursor, resource)
        except ValueError as e:
            return _invalid(str(e), "invalid_cursor")
        link = state["link"]
        include_system = bool(state.get("system", include_system))

    try:
        prefer = INCLUDE_SYSTEM_PREFER if include_system else None
        if link:
            result = await _graph_request(credentials, "GET", "", url=link, prefer=prefer)
        else:
            # Replies support ONLY $top (max 50): no $filter/$orderby/$select.
            result = await _graph_request(
                credentials, "GET", resource,
                params={"$top": _page_size(page_size)}, prefer=prefer,
            )
        replies = [m for m in result.get("value", []) if is_listed(m, include_system)]
        next_cursor = _next_cursor(result, resource, system=include_system)
        return {
            "success": True,
            "count": len(replies),
            "replies": replies,
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
        }
    except (ValueError, httpx.HTTPError) as e:
        return _paged_refusal("Failed to list message replies", e)
    except Exception as e:
        logger.exception("list_channel_message_replies_error", error=str(e))
        return {"success": False, "error": "Failed to list message replies", "error_code": "upstream_error"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /teams/{team_id}/channels/{channel_id}/messages
@server.tool(
    name='list_channel_messages',
    description="Page through the complete history of a Teams channel, one Microsoft Graph request per call. For a quick look at recent messages use read_channel_messages instead. Pass `team_id` and `channel_id` (required). Optional: `created_after` and `created_before` (ISO 8601; only messages created in [created_after, created_before) are returned, thread roots and replies alike), `page_size` (threads per Graph page, default: 20, max: 50), `include_system_messages` (default: false). Returns `messages`, a flat list of raw Microsoft Graph chatMessage objects (deleted messages kept with deletedDateTime set; a reply's replyToId names its thread root). One cursor walks every thread and every long thread's remaining replies, then re-reads the threads that changed after the first call, so a message can be returned more than once: the later copy is the newer, so keep the last copy of each `id`. Pass `next_cursor` back as `cursor` until `complete` is true; the settings of the first call carry over.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_output_schema({'type': 'object',
     'properties': {**_PAGED_COMMON_OUTPUT,
                    'messages': {**_GRAPH_MESSAGE_ITEMS,
                                 'description': 'Thread roots and replies created in the '
                                                'window, as Microsoft Graph returned them. '
                                                'The same id can appear again on a later '
                                                'page; keep the last copy.'}}})
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string', 'description': 'Team ID from list_teams'},
                    'channel_id': {'type': 'string',
                                   'description': 'Channel ID from list_channels'},
                    'created_after': {'type': 'string',
                                      'description': 'ISO 8601 datetime (no offset means '
                                                     'UTC). Only messages created at or '
                                                     'after it.'},
                    'created_before': {'type': 'string',
                                       'description': 'ISO 8601 datetime (no offset means '
                                                      'UTC). Only messages created before '
                                                      'it.'},
                    'page_size': {'type': 'integer',
                                  'description': 'Threads per page (default: 20, max: 50)',
                                  'default': 20},
                    'include_system_messages': {'type': 'boolean',
                                                'description': 'Also return system events '
                                                               '(default: false)',
                                                'default': False},
                    'cursor': {'type': 'string',
                               'description': 'next_cursor from a previous call for the same '
                                              'channel. Its settings carry over.'}},
     'required': ['team_id', 'channel_id']})
async def list_channel_messages(credentials: Dict[str, str],
    team_id: str,
    channel_id: str,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    include_system_messages: bool = False,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """One Graph page of a channel's history: a page of threads, or one thread's replies."""
    for value, label in ((team_id, "team_id"), (channel_id, "channel_id")):
        error = validate_id(value, label)
        if error:
            return _invalid(error)
    resource = f"/teams/{team_id}/channels/{channel_id}/messages"
    if cursor:
        try:
            state = decode_channel_cursor(cursor, resource)
        except ValueError as e:
            return _invalid(str(e), "invalid_cursor")
    else:
        state = {
            "phase": CHANNEL_MAIN, "link": None, "pending": [], "reply": None,
            "after": created_after, "before": created_before,
            # Threads changed after this are read again in the catch-up pass.
            # A minute early, so a clock a little ahead of Graph's misses none.
            "since": graph_utc(datetime.now(timezone.utc) - CATCHUP_MARGIN),
            "size": _page_size(page_size), "system": include_system_messages,
        }
    try:
        after = parse_utc(state["after"], "created_after") if state.get("after") else None
        before = parse_utc(state["before"], "created_before") if state.get("before") else None
        since = parse_utc(state["since"], "cursor")
    except (KeyError, ValueError) as e:
        return _invalid(str(e), "invalid_cursor" if cursor else "invalid_argument")
    if after is not None and before is not None and after >= before:
        return _invalid("created_after must be earlier than created_before")
    include_system = bool(state.get("system"))

    def wanted(msg: Dict[str, Any]) -> bool:
        if not is_listed(msg, include_system):
            return False
        created = message_time(msg, "createdDateTime")
        if created is None:
            return True  # Graph always sends it; never drop a message silently
        return (after is None or created >= after) and (before is None or created < before)

    try:
        prefer = INCLUDE_SYSTEM_PREFER if include_system else None
        if state["reply"] is not None or state["pending"]:
            messages = await _channel_replies_page(credentials, resource, state, prefer, wanted)
        else:
            messages = await _channel_threads_page(
                credentials, resource, state, prefer, wanted,
                bound=after if state["phase"] == CHANNEL_MAIN else since,
            )
        complete = state["phase"] == CHANNEL_DONE and not state["pending"] and state["reply"] is None
        next_cursor = None if complete else encode_channel_cursor(state)
        return {
            "success": True,
            "count": len(messages),
            "messages": messages,
            "next_cursor": next_cursor,
            "complete": complete,
        }
    except (ValueError, httpx.HTTPError) as e:
        return _paged_refusal("Failed to list channel messages", e)
    except Exception as e:
        logger.exception("list_channel_messages_error", error=str(e))
        return {"success": False, "error": "Failed to list channel messages", "error_code": "upstream_error"}


# -- list_channel_messages: one cursor over a whole channel ---------------------
#
# Graph cannot filter channel messages by date. It lists threads by the latest
# activity of their reply chain, with at most a page of replies inline, so a
# full window takes three kinds of request, all behind one cursor:
#   main     pages of threads, until a whole page has had no activity since
#            created_after;
#   replies  the rest of any thread whose replies did not fit inline, read
#            before the next page of threads;
#   catchup  the threads active since the first call, read again: a thread
#            that gained a reply while paging moved to the front, behind the
#            main pass, which would otherwise miss it.

CHANNEL_MAIN = "main"
CHANNEL_CATCHUP = "catchup"
CHANNEL_DONE = "done"
CATCHUP_MARGIN = timedelta(minutes=1)
# Graph's own maximum for replies.
_REPLIES_PAGE_SIZE = 50


def encode_channel_cursor(state: Dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_channel_cursor(cursor: str, resource: str) -> Dict[str, Any]:
    """Unwrap a channel cursor, refusing any Graph link in it for another resource.

    Every link is sent with the bearer token, so each one is checked, not
    just the first, and a queued thread id is held to the id rules.
    """
    refused = ValueError(
        "cursor does not belong to this channel; start again without a cursor"
    )
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("cursor is not a cursor this tool returned")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        state = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError):
        raise ValueError("cursor is not a cursor this tool returned") from None
    if not isinstance(state, dict) or state.get("phase") not in (CHANNEL_MAIN, CHANNEL_CATCHUP, CHANNEL_DONE):
        raise ValueError("cursor is not a cursor this tool returned")
    link = state.get("link")
    if link is not None and (not isinstance(link, str) or not graph_link_matches(link, resource)):
        raise refused
    pending = state.get("pending")
    if not isinstance(pending, list) or any(
        not isinstance(i, str) or validate_id(i, "message_id") for i in pending
    ):
        raise refused
    reply = state.get("reply")
    if reply is not None:
        if not isinstance(reply, dict) or not isinstance(reply.get("id"), str) or validate_id(reply["id"], "message_id"):
            raise refused
        reply_link = reply.get("link")
        if reply_link is not None and (
            not isinstance(reply_link, str)
            or not graph_link_matches(reply_link, f"{resource}/{reply['id']}/replies")
        ):
            raise refused
    state["size"] = _page_size(state.get("size") or DEFAULT_PAGE_SIZE)
    return state


async def _channel_threads_page(
    credentials: Dict[str, str],
    resource: str,
    state: Dict[str, Any],
    prefer: Optional[str],
    wanted: Any,
    bound: Optional[datetime],
) -> List[Dict[str, Any]]:
    """Read one page of threads; queue long threads; advance the pass."""
    if state["link"]:
        result = await _graph_request(credentials, "GET", "", url=state["link"], prefer=prefer)
    else:
        # Channel messages support ONLY $top (max 50) and $expand=replies.
        result = await _graph_request(
            credentials, "GET", resource,
            params={"$top": state["size"], "$expand": "replies"}, prefer=prefer,
        )

    roots = result.get("value", [])
    messages: List[Dict[str, Any]] = []
    any_active = False
    for root in roots:
        root = dict(root)
        inline = root.pop("replies", None) or []
        replies_link = root.pop("replies@odata.nextLink", None)
        root.pop("replies@odata.context", None)
        times = [t for t in (message_time(m, "lastModifiedDateTime") for m in [root, *inline]) if t]
        last = max(times) if times else None
        if bound is None or last is None or last >= bound:
            any_active = True
        messages.extend(m for m in [root, *inline] if wanted(m))
        if replies_link:
            root_id = str(root.get("id") or "")
            if validate_id(root_id, "message_id") or not graph_link_matches(
                replies_link, f"{resource}/{root_id}/replies"
            ):
                raise ValueError("Graph returned a replies link for a different message")
            # Read from the start: the inline replies are not always the
            # oldest ones, and a copy read twice is kept once by the caller.
            state["pending"].append(root_id)

    # Stop only on a page where no thread is active since the bound, not at
    # the first old thread: the documented order is by reply-chain activity
    # but its direction is not, so one page of margin costs one request.
    reached = bound is not None and bool(roots) and not any_active
    link = None if reached else result.get("@odata.nextLink")
    if link and not graph_link_matches(link, resource):
        raise ValueError("Graph returned a next-page link for a different resource")
    if link:
        state["link"] = link
    else:
        state["link"] = None
        state["phase"] = CHANNEL_CATCHUP if state["phase"] == CHANNEL_MAIN else CHANNEL_DONE
    return messages


async def _channel_replies_page(
    credentials: Dict[str, str],
    resource: str,
    state: Dict[str, Any],
    prefer: Optional[str],
    wanted: Any,
) -> List[Dict[str, Any]]:
    """Read one page of the current long thread's replies."""
    reply = state["reply"] or {"id": state["pending"].pop(0), "link": None}
    replies_resource = f"{resource}/{reply['id']}/replies"
    if reply["link"]:
        result = await _graph_request(credentials, "GET", "", url=reply["link"], prefer=prefer)
    else:
        # Replies support ONLY $top (max 50): no $filter/$orderby/$select.
        result = await _graph_request(
            credentials, "GET", replies_resource,
            params={"$top": _REPLIES_PAGE_SIZE}, prefer=prefer,
        )
    link = result.get("@odata.nextLink")
    if link and not graph_link_matches(link, replies_resource):
        raise ValueError("Graph returned a next-page link for a different resource")
    state["reply"] = {"id": reply["id"], "link": link} if link else None
    return [m for m in result.get("value", []) if wanted(m)]



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /teams/{team_id}/channels
@server.tool(
    name='list_channels',
    description="List channels in a team. Pass `team_id` (required, from list_teams). Returns channel id, name, description, and membership_type (standard/private/shared). Use channel IDs with channel message tools.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string',
                                'description': 'Team ID from list_teams'}},
     'required': ['team_id']})
async def list_channels(credentials: Dict[str, str],
    team_id: str,
) -> Dict[str, Any]:
    """List channels in a team."""
    try:
        error = validate_id(team_id, "team_id")
        if error:
            return {"success": False, "error": error}

        result = await _graph_request(
            credentials,
            "GET",
            f"/teams/{team_id}/channels",
            params={"$select": "id,displayName,description,membershipType"},
        )
        channels = result.get("value", [])
        return {
            "success": True,
            "count": len(channels),
            "channels": [
                {
                    "id": c.get("id"),
                    "name": c.get("displayName"),
                    "description": c.get("description"),
                    "membership_type": c.get("membershipType"),
                }
                for c in channels
            ],
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to list channels: {e}"}
    except Exception as e:
        logger.exception("list_channels_error", error=str(e))
        return {"success": False, "error": "Failed to list channels"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /chats/{chat_id}/messages
@server.tool(
    name='list_chat_messages',
    description="Page through the complete message history of a Teams chat, newest first, one page per call. For a quick look at recent messages use read_chat_messages instead. Pass `chat_id` (required, from list_chats). Optional: `created_after` and `created_before` (ISO 8601, no offset means UTC; messages created at or after / before them), `page_size` (default: 20, max: 50), `include_system_messages` (default: false). Returns raw Microsoft Graph chatMessage objects (HTML body, attachments, mentions, reactions, edit and delete times; deleted messages are kept with deletedDateTime set). Pass `next_cursor` back as `cursor` until `complete` is true; the dates of the first call carry over.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_output_schema({'type': 'object',
     'properties': {**_PAGED_COMMON_OUTPUT,
                    'messages': _GRAPH_MESSAGE_ITEMS,
                    'stopped_reason': {'type': ['string', 'null'],
                                       'description': 'end_of_data, or reached_created_after '
                                                      'when created_after ended the paging.'}}})
@declare_schema({'type': 'object',
     'properties': {'chat_id': {'type': 'string', 'description': 'Chat ID from list_chats'},
                    'created_after': {'type': 'string',
                                      'description': 'ISO 8601 datetime (no offset means UTC). '
                                                     'Only messages created at or after it.'},
                    'created_before': {'type': 'string',
                                       'description': 'ISO 8601 datetime (no offset means UTC). '
                                                      'Only messages created before it.'},
                    'page_size': {'type': 'integer',
                                  'description': 'Messages per page (default: 20, max: 50)',
                                  'default': 20},
                    'include_system_messages': {'type': 'boolean',
                                                'description': 'Also return system events '
                                                               '(default: false)',
                                                'default': False},
                    'cursor': {'type': 'string',
                               'description': 'next_cursor from a previous call for the same '
                                              'chat. Its dates and settings carry over.'}},
     'required': ['chat_id']})
async def list_chat_messages(credentials: Dict[str, str],
    chat_id: str,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    include_system_messages: bool = False,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """One page of a chat's messages, newest first by creation time."""
    error = validate_id(chat_id, "chat_id")
    if error:
        return _invalid(error)
    resource = f"/chats/{chat_id}/messages"
    link = None
    if cursor:
        try:
            state = decode_cursor(cursor, resource)
        except ValueError as e:
            return _invalid(str(e), "invalid_cursor")
        link = state["link"]
        after_raw, before_raw = state.get("after"), state.get("before")
        include_system = bool(state.get("system", include_system_messages))
    else:
        after_raw, before_raw = created_after, created_before
        include_system = include_system_messages
    try:
        after = parse_utc(after_raw, "created_after") if after_raw else None
        before = parse_utc(before_raw, "created_before") if before_raw else None
    except ValueError as e:
        return _invalid(str(e), "invalid_cursor" if cursor else "invalid_argument")
    if after is not None and before is not None and after >= before:
        return _invalid("created_after must be earlier than created_before")

    try:
        prefer = INCLUDE_SYSTEM_PREFER if include_system else None
        if link:
            result = await _graph_request(credentials, "GET", "", url=link, prefer=prefer)
        else:
            # Chat messages: $orderby createdDateTime supports desc only, and
            # $filter on createdDateTime supports only `lt`, and only with
            # $orderby on the same property (otherwise Graph ignores it). So the
            # upper bound is applied by Graph and the lower bound by stopping.
            params: Dict[str, Any] = {
                "$top": _page_size(page_size),
                "$orderby": "createdDateTime desc",
            }
            if before is not None:
                params["$filter"] = f"createdDateTime lt {graph_utc(before)}"
            result = await _graph_request(credentials, "GET", resource, params=params, prefer=prefer)

        messages: List[Dict[str, Any]] = []
        reached = False
        for msg in result.get("value", []):
            created = message_time(msg, "createdDateTime")
            if after is not None and created is not None and created < after:
                reached = True  # newest first, so every later message is older too
                break
            # Checked here as well, in case Graph ignored the filter.
            if before is not None and created is not None and created >= before:
                continue
            if is_listed(msg, include_system):
                messages.append(msg)

        next_cursor = None if reached else _next_cursor(
            result, resource,
            after=graph_utc(after) if after else None,
            before=graph_utc(before) if before else None,
            system=include_system,
        )
        return {
            "success": True,
            "count": len(messages),
            "messages": messages,
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
            "stopped_reason": (
                "reached_created_after" if reached
                else "end_of_data" if next_cursor is None
                else None
            ),
        }
    except (ValueError, httpx.HTTPError) as e:
        return _paged_refusal("Failed to list chat messages", e)
    except Exception as e:
        logger.exception("list_chat_messages_error", error=str(e))
        return {"success": False, "error": "Failed to list chat messages", "error_code": "upstream_error"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_chats',
    description="List the connected user's recent 1:1 and group chats. Pass `max_results` (default: 20, max: 50). Returns chat id, topic, chat_type (oneOnOne/group/meeting), members as {id, name, email} objects (the id is the Entra user id needed to @-mention that person; the email is their address, for inviting them to a meeting or writing to them), and last_updated. Use chat IDs with send_chat_message and read_chat_messages.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'max_results': {'type': 'integer',
                                    'description': 'Max chats to return (default: 20, max: '
                                                   '50)',
                                    'default': 20}}})
async def list_chats(credentials: Dict[str, str],
    max_results: int = 20,
) -> Dict[str, Any]:
    """List the user's chats, most recently active first."""
    try:
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)

        result = await _graph_request(
            credentials,
            "GET",
            # /me/chats: $orderby supports ONLY lastMessagePreview/createdDateTime
            # desc (asc rejected); $expand supports only members/lastMessagePreview
            # (members capped at 25 per chat); $top max 50.
            "/me/chats",
            params={
                "$top": max_results,
                "$expand": "members",
                "$orderby": "lastMessagePreview/createdDateTime desc",
            },
        )
        chats = result.get("value", [])
        return {
            "success": True,
            "count": len(chats),
            "has_more": "@odata.nextLink" in result,
            "chats": [
                {
                    "id": c.get("id"),
                    "topic": c.get("topic"),
                    "chat_type": c.get("chatType"),
                    # aadUserConversationMember.userId is the Entra object
                    # id a mention binds to; its own `id` is only the
                    # membership id. Falling back to that one looked
                    # harmless and was not: Graph accepts the message,
                    # binds nothing, and the mention renders as inert text
                    # that notifies the person it was written for exactly
                    # never. No id at all is the honest answer, and a
                    # caller can see there is nothing to mention with.
                    "members": [
                        {
                            "id": m.get("userId"),
                            "name": m.get("displayName"),
                            # Same reason as list_channel_members: an id
                            # mentions someone, an address invites them,
                            # and both are in the response already.
                            "email": m.get("email"),
                        }
                        for m in c.get("members", [])
                        if m.get("displayName")
                    ],
                    "last_updated": c.get("lastUpdatedDateTime"),
                }
                for c in chats
            ],
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to list chats: {e}"}
    except Exception as e:
        logger.exception("list_chats_error", error=str(e))
        return {"success": False, "error": "Failed to list chats"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='list_teams',
    description="List Microsoft Teams the connected user is a member of. No required parameters. Returns team id, name, and description. Use team IDs with list_channels and channel message tools.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
async def list_teams(credentials: Dict[str, str]) -> Dict[str, Any]:
    """List teams the user has joined."""
    try:
        result = await _graph_request(
            credentials,
            "GET",
            # /me/joinedTeams supports NO OData query params ($select/$filter/$top
            # all unsupported). Only id, displayName, description, isArchived,
            # tenantId come back populated; everything else is null.
            "/me/joinedTeams",
        )
        teams = result.get("value", [])
        return {
            "success": True,
            "count": len(teams),
            "teams": [
                {
                    "id": t.get("id"),
                    "name": t.get("displayName"),
                    "description": t.get("description"),
                }
                for t in teams
            ],
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to list teams: {e}"}
    except Exception as e:
        logger.exception("list_teams_error", error=str(e))
        return {"success": False, "error": "Failed to list teams"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /teams/{team_id}/channels/{channel_id}/messages
@server.tool(
    name='read_channel_messages',
    description="Read recent messages from a Teams channel, including the replies threaded under each message (roots newest first, replies oldest-first beneath their root; system events excluded). Pass `team_id` and `channel_id` (required). Optional: `max_results` (default: 20, max: 50) - this caps root messages; their replies are returned in addition. The total is capped at 100 items; if more exist `has_more` is true and the view is truncated. Each message has id, from, created_at, and plain-text content; roots carry `reply_count` (the true total, even if some replies were trimmed) and replies carry `reply_to` (the root id). A deleted root that still has replies appears with content '[deleted]' and `deleted: true` so its replies aren't lost. Use a root message id with reply_to_channel_message.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string', 'description': 'Team ID from list_teams'},
                    'channel_id': {'type': 'string',
                                   'description': 'Channel ID from list_channels'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max messages to return (default: 20, '
                                                   'max: 50)',
                                    'default': 20}},
     'required': ['team_id', 'channel_id']})
async def read_channel_messages(credentials: Dict[str, str],
    team_id: str,
    channel_id: str,
    max_results: int = 20,
) -> Dict[str, Any]:
    """Read recent channel messages."""
    try:
        for value, label in ((team_id, "team_id"), (channel_id, "channel_id")):
            error = validate_id(value, label)
            if error:
                return {"success": False, "error": error}
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)

        result = await _graph_request(
            credentials,
            "GET",
            # Channel messages support ONLY $top (max 50) and $expand=replies —
            # no $orderby/$filter/$select. Results come sorted by last modified
            # date of the reply chain, newest first. The /messages collection is
            # roots ONLY; a channel conversation's replies live in each root's
            # `replies` collection, so without $expand every threaded reply is
            # dropped. One expanded call returns roots + their replies inline.
            f"/teams/{team_id}/channels/{channel_id}/messages",
            params={"$top": max_results, "$expand": "replies"},
        )
        # Flatten: each root is followed by its replies. $top caps roots;
        # replies ride along in the same response. NOTE: $expand=replies
        # returns the recent replies per root, not a guaranteed-exhaustive
        # thread for very long conversations.
        flat: List[Dict[str, Any]] = []
        for root in result.get("value", []):
            replies = [r for r in (root.get("replies") or []) if is_user_message(r)]
            if is_user_message(root):
                root_out = simplify_message(root)
            elif root.get("messageType") == "message" and root.get("deletedDateTime") and replies:
                # A deleted root is tombstoned by Graph but its reply chain
                # survives. Skipping it would silently drop live replies, so
                # emit a placeholder root and hang the replies under it.
                root_out = simplify_message(root)
                root_out["content"] = "[deleted]"
                root_out["deleted"] = True
            else:
                continue  # system event, or deleted root with no live replies
            root_out["reply_count"] = len(replies)
            flat.append(root_out)
            # Graph returns replies newest-first; present them oldest-first so
            # the thread reads top-to-bottom under its root.
            for reply in sorted(replies, key=lambda m: m.get("createdDateTime") or ""):
                reply_out = simplify_message(reply)
                reply_out["reply_to"] = root.get("id")
                flat.append(reply_out)

        # Hard ceiling on the flattened list so one call can't flood the agent.
        # Trimming from the tail only drops trailing items, and a reply always
        # follows its root, so any surviving reply still has its root present.
        truncated = len(flat) > MAX_FLATTENED_MESSAGES
        messages = flat[:MAX_FLATTENED_MESSAGES]
        return {
            "success": True,
            "count": len(messages),
            "has_more": truncated or "@odata.nextLink" in result,
            "messages": messages,
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to read channel messages: {e}"}
    except Exception as e:
        logger.exception("read_channel_messages_error", error=str(e))
        return {"success": False, "error": "Failed to read channel messages"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /chats/{chat_id}/messages
@server.tool(
    name='read_chat_messages',
    description="Read recent messages from a Teams chat (newest first, system events excluded). Pass `chat_id` (required, from list_chats or send_chat_message). Optional: `max_results` (default: 20, max: 50). Returns messages with id, from, created_at, and plain-text content.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'chat_id': {'type': 'string', 'description': 'Chat ID from list_chats'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max messages to return (default: 20, '
                                                   'max: 50)',
                                    'default': 20}},
     'required': ['chat_id']})
async def read_chat_messages(credentials: Dict[str, str],
    chat_id: str,
    max_results: int = 20,
) -> Dict[str, Any]:
    """Read recent messages from a chat."""
    try:
        error = validate_id(chat_id, "chat_id")
        if error:
            return {"success": False, "error": error}
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)

        result = await _graph_request(
            credentials,
            "GET",
            # Chat messages: $top max 50; $orderby only lastModifiedDateTime
            # (default) or createdDateTime, desc only; $filter only date ranges
            # paired with $orderby on the same property. Default is newest first.
            f"/chats/{chat_id}/messages",
            params={"$top": max_results},
        )
        messages = [m for m in result.get("value", []) if is_user_message(m)]
        return {
            "success": True,
            "count": len(messages),
            "has_more": "@odata.nextLink" in result,
            "messages": [simplify_message(m) for m in messages],
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to read chat messages: {e}"}
    except Exception as e:
        logger.exception("read_chat_messages_error", error=str(e))
        return {"success": False, "error": "Failed to read chat messages"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST, path built at runtime
@server.tool(
    name='reply_to_channel_message',
    description="Reply to an existing channel message thread as the connected user. Pass `team_id`, `channel_id`, `message_id` (the root message from read_channel_messages), and `content` (required). Optional: `content_type` (text/html, default: text) and `mentions` to @-mention people. Returns reply_id.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string', 'description': 'Team ID from list_teams'},
                    'channel_id': {'type': 'string',
                                   'description': 'Channel ID from list_channels'},
                    'message_id': {'type': 'string',
                                   'description': 'Root message ID to reply to'},
                    'content': {'type': 'string', 'description': 'Reply content'},
                    'content_type': {'type': 'string',
                                     'description': 'Content format',
                                     'enum': ['text', 'html'],
                                     'default': 'text'},
                    'mentions': {'type': 'array',
                                 'description': 'People to @-mention. Each item is {"id": '
                                                '"<Entra user id>", "name": "Display '
                                                'Name"}. Get ids from list_chats members '
                                                'for a chat, or list_channel_members for a '
                                                'channel. Forces HTML formatting.',
                                 'items': {'type': 'object',
                                           'properties': {'id': {'type': 'string'},
                                                          'name': {'type': 'string'}},
                                           'required': ['id', 'name']}}},
     'required': ['team_id', 'channel_id', 'message_id', 'content']})
async def reply_to_channel_message(credentials: Dict[str, str],
    team_id: str,
    channel_id: str,
    message_id: str,
    content: str,
    content_type: str = "text",
    mentions: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Reply to a channel message thread."""
    try:
        for value, label in (
            (team_id, "team_id"),
            (channel_id, "channel_id"),
            (message_id, "message_id"),
        ):
            error = validate_id(value, label)
            if error:
                return {"success": False, "error": error}
        error = validate_message_content(content, content_type)
        if error:
            return {"success": False, "error": error}
        error = validate_mentions(mentions)
        if error:
            return {"success": False, "error": error}

        result = await _graph_request(
            credentials,
            "POST",
            f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
            json_data=build_message_payload(content, content_type, mentions),
        )
        return {
            "success": True,
            "reply_id": result.get("id"),
            "web_url": result.get("webUrl"),
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to reply to channel message: {e}"}
    except Exception as e:
        logger.exception("reply_to_channel_message_error", error=str(e))
        return {"success": False, "error": "Failed to reply to channel message"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /users
@server.tool(
    name='search_people',
    description="Search the company directory for a person by name, partial name, or email. Pass `query` (e.g. \"alex\", \"Jordan Lee\", \"j.lee\") and optional `max_results` (default: 10, max: 25). Returns matches as {id, name, email, job_title, department}: `id` is the Entra user id used to @-mention someone, `email` is the address used to invite them to a meeting or write to them. This searches the WHOLE organisation, including people the connected user has never messaged - unlike list_chats and list_channel_members, which only cover people already shared a conversation with. Use this to resolve a name to an address before proposing an invite or an email.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': 'Name, partial name, or email to search for'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max people to return (default: 10, '
                                                   'max: 25)',
                                    'default': 10}},
     'required': ['query']})
async def search_people(credentials: Dict[str, str],
    query: str,
    max_results: int = 10,
) -> Dict[str, Any]:
    """Search the organisation's directory.

    The directory that matters is Entra, not the set of people you happen
    to have chatted with. `list_chats` and `list_channel_members` can only
    describe an existing relationship, so "invite someone I have never
    messaged" had no answer at all: the person was found by name and
    reported with "no email address available", which reads as a broken
    product rather than a missing lookup.

    `User.ReadBasic.All` is already in this server's requested scopes, so
    this needs no new consent.
    """
    try:
        cleaned = (query or "").strip()
        if not cleaned:
            return {"success": False, "error": "query is required"}
        max_results = min(max(max_results, 1), _MAX_PEOPLE_RESULTS)

        # Quotes would terminate the $search string early. Dropped rather
        # than escaped: a name never legitimately contains one, and a
        # half-escaped search silently matches the wrong thing.
        safe = cleaned.replace('"', "").replace("\\", "")

        result = await _graph_request(
            credentials,
            "GET",
            "/users",
            params={
                # displayName covers "Jordan Lee", mail and
                # userPrincipalName cover "j.lee" - people search by
                # whichever fragment they happen to remember. The shared
                # helper adds ConsistencyLevel and the non-indexed-query
                # hint that $search on /users requires.
                "$search": (
                    f'"displayName:{safe}" OR "mail:{safe}" OR "userPrincipalName:{safe}"'
                ),
                "$select": "id,displayName,mail,userPrincipalName,jobTitle,department",
                "$top": max_results,
            },
        )

        people = []
        for u in result.get("value", []):
            # `mail` is the real address and is null on accounts without a
            # mailbox. userPrincipalName is usually the same string but is
            # an identifier, not a guaranteed deliverable address, so it is
            # a fallback and is reported separately rather than silently
            # presented as an email.
            mail = u.get("mail")
            people.append(
                {
                    "id": u.get("id"),
                    "name": u.get("displayName"),
                    "email": mail or u.get("userPrincipalName"),
                    "email_is_verified": bool(mail),
                    "job_title": u.get("jobTitle"),
                    "department": u.get("department"),
                }
            )

        return {"success": True, "count": len(people), "people": people}
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to search people: {e}"}
    except Exception as e:
        logger.exception("search_people_error", error=str(e))
        return {"success": False, "error": "Failed to search the directory"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /teams/{team_id}/channels/{channel_id}/messages
@server.tool(
    name='send_channel_message',
    description="Post a message to a Teams channel as the connected user. Pass `team_id`, `channel_id` (from list_teams/list_channels), and `content` (required). Optional: `content_type` (text/html, default: text) and `mentions` to @-mention people. Returns message_id and web_url.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team_id': {'type': 'string', 'description': 'Team ID from list_teams'},
                    'channel_id': {'type': 'string',
                                   'description': 'Channel ID from list_channels'},
                    'content': {'type': 'string', 'description': 'Message content'},
                    'content_type': {'type': 'string',
                                     'description': 'Content format',
                                     'enum': ['text', 'html'],
                                     'default': 'text'},
                    'mentions': {'type': 'array',
                                 'description': 'People to @-mention. Each item is {"id": '
                                                '"<Entra user id>", "name": "Display '
                                                'Name"}. Get ids from list_chats members '
                                                'for a chat, or list_channel_members for a '
                                                'channel. Forces HTML formatting.',
                                 'items': {'type': 'object',
                                           'properties': {'id': {'type': 'string'},
                                                          'name': {'type': 'string'}},
                                           'required': ['id', 'name']}}},
     'required': ['team_id', 'channel_id', 'content']})
async def send_channel_message(credentials: Dict[str, str],
    team_id: str,
    channel_id: str,
    content: str,
    content_type: str = "text",
    mentions: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Post a new message to a channel."""
    try:
        for value, label in ((team_id, "team_id"), (channel_id, "channel_id")):
            error = validate_id(value, label)
            if error:
                return {"success": False, "error": error}
        error = validate_message_content(content, content_type)
        if error:
            return {"success": False, "error": error}
        error = validate_mentions(mentions)
        if error:
            return {"success": False, "error": error}

        result = await _graph_request(
            credentials,
            "POST",
            f"/teams/{team_id}/channels/{channel_id}/messages",
            json_data=build_message_payload(content, content_type, mentions),
        )
        return {
            "success": True,
            "message_id": result.get("id"),
            "web_url": result.get("webUrl"),
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to send channel message: {e}"}
    except Exception as e:
        logger.exception("send_channel_message_error", error=str(e))
        return {"success": False, "error": "Failed to send channel message"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /chats/{chat_id}/messages
@server.tool(
    name='send_chat_message',
    description="Send a direct (1:1 or group) Teams chat message as the connected user. Pass `content` (required) plus either `chat_id` (from list_chats) or `user_email` (recipient's email — the 1:1 chat is found or created automatically). Optional: `content_type` (text/html, default: text) and `mentions` to @-mention people. Returns chat_id and message_id.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'content': {'type': 'string', 'description': 'Message content'},
                    'chat_id': {'type': 'string',
                                'description': 'Existing chat ID from list_chats'},
                    'user_email': {'type': 'string',
                                   'description': 'Recipient email for a 1:1 chat (used '
                                                  'when chat_id is omitted)'},
                    'content_type': {'type': 'string',
                                     'description': 'Content format',
                                     'enum': ['text', 'html'],
                                     'default': 'text'},
                    'mentions': {'type': 'array',
                                 'description': 'People to @-mention. Each item is {"id": '
                                                '"<Entra user id>", "name": "Display '
                                                'Name"}. Get ids from list_chats members '
                                                'for a chat, or list_channel_members for a '
                                                'channel. Forces HTML formatting.',
                                 'items': {'type': 'object',
                                           'properties': {'id': {'type': 'string'},
                                                          'name': {'type': 'string'}},
                                           'required': ['id', 'name']}}},
     'required': ['content']})
async def send_chat_message(credentials: Dict[str, str],
    content: str,
    chat_id: str = "",
    user_email: str = "",
    content_type: str = "text",
    mentions: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Send a chat message by chat id or recipient email."""
    try:
        error = validate_message_content(content, content_type)
        if error:
            return {"success": False, "error": error}
        error = validate_mentions(mentions)
        if error:
            return {"success": False, "error": error}

        chat_id = chat_id.strip()
        user_email = user_email.strip()
        if not chat_id and not user_email:
            return {
                "success": False,
                "error": "Pass either chat_id or user_email",
            }

        access_token = await _get_access_token(credentials)

        if not chat_id:
            if not EMAIL_RE.match(user_email):
                return {"success": False, "error": f"Invalid email address: {user_email}"}
            chat_id = await _resolve_one_on_one_chat(credentials, user_email, access_token)

        error = validate_id(chat_id, "chat_id")
        if error:
            return {"success": False, "error": error}

        result = await _graph_request(
            credentials,
            "POST",
            f"/chats/{chat_id}/messages",
            json_data=build_message_payload(content, content_type, mentions),
            access_token=access_token,
        )
        return {
            "success": True,
            "chat_id": chat_id,
            "message_id": result.get("id"),
        }
    except (ValueError, httpx.HTTPError) as e:
        return {"success": False, "error": f"Failed to send chat message: {e}"}
    except Exception as e:
        logger.exception("send_chat_message_error", error=str(e))
        return {"success": False, "error": "Failed to send chat message"}
