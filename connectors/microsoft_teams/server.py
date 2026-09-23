"""The microsoft-teams connector.

Ported from the v1 mixin form by a codemod.

12 tools, 8 of them proposed read-only. Every input schema
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

from magone_mcp_core import ConnectorServer, declare_schema

from ._shared import GRAPH_API_URL, GraphNotFoundError, _get_access_token, _graph_request

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
