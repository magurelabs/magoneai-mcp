"""The google-calendar connector.

Ported from the v1 mixin form by a codemod.

5 tools, 2 of them proposed read-only. Every input schema
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

SERVER_NAME = "google-calendar"
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

from typing import Any, Dict, Optional
from ._shared import GOOGLE_CREDENTIAL_HEADERS, _google_request


CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"

CALENDAR_SCOPE = (
    "https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/calendar.events"
)



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /calendars/{calendar_id}/events
@server.tool(
    name='google_calendar_create_event',
    description="Create an event in the selected Google Calendar.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'calendar_id': {'type': 'string', 'default': 'primary'},
                    'summary': {'type': 'string'},
                    'description': {'type': 'string'},
                    'start_datetime': {'type': 'string',
                                       'description': 'Event start in RFC3339 format.'},
                    'end_datetime': {'type': 'string',
                                     'description': 'Event end in RFC3339 format.'},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone (e.g. Asia/Kolkata).',
                                 'default': 'UTC'}},
     'required': ['summary', 'start_datetime', 'end_datetime']})
@declare_output_schema({'properties': {'htmlLink': {'type': 'string'},
                                       'id': {'type': 'string'},
                                       'status': {'type': 'string'}},
                        'required': ['id'],
                        'type': 'object'})
async def google_calendar_create_event(credentials: Dict[str, Any],
    summary: str,
    start_datetime: str,
    end_datetime: str,
    calendar_id: str = "primary",
    description: str = "",
    timezone: str = "UTC",
) -> Dict[str, Any]:
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": timezone},
        "end": {"dateTime": end_datetime, "timeZone": timezone},
    }
    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="POST",
        base_url=CALENDAR_API_BASE_URL,
        path=f"/calendars/{calendar_id}/events",
        json_data=body,
        scopes=CALENDAR_SCOPE,
    )
    return data



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /calendars/{calendar_id}/events/{event_id}
@server.tool(
    name='google_calendar_delete_event',
    description="Delete an event from the selected Google Calendar.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'calendar_id': {'type': 'string', 'default': 'primary'},
                    'event_id': {'type': 'string',
                                 'description': 'ID of the event to delete.'}},
     'required': ['event_id']})
@declare_output_schema({'properties': {'success': {'type': 'boolean'}}, 'required': ['success'], 'type': 'object'})
async def google_calendar_delete_event(credentials: Dict[str, Any],
    event_id: str,
    calendar_id: str = "primary",
) -> Dict[str, Any]:
    await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="DELETE",
        base_url=CALENDAR_API_BASE_URL,
        path=f"/calendars/{calendar_id}/events/{event_id}",
        scopes=CALENDAR_SCOPE,
    )
    return {"success": True}



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET, path built at runtime
@server.tool(
    name='google_calendar_list_calendars',
    description="List calendars accessible to the authenticated user.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'items': {'items': {'type': 'object'}, 'type': 'array'}},
                        'required': ['items'],
                        'type': 'object'})
async def google_calendar_list_calendars(credentials: Dict[str, Any]) -> Dict[str, Any]:
    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="GET",
        base_url=CALENDAR_API_BASE_URL,
        path="/users/me/calendarList",
        scopes=CALENDAR_SCOPE,
    )
    return {"items": data.get("items", [])}



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /calendars/{calendar_id}/events
@server.tool(
    name='google_calendar_list_events',
    description="List events in a calendar for a time range.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'calendar_id': {'type': 'string',
                                    'description': 'Calendar ID (default: primary).',
                                    'default': 'primary'},
                    'time_min': {'type': 'string',
                                 'description': 'RFC3339 start time filter.'},
                    'time_max': {'type': 'string',
                                 'description': 'RFC3339 end time filter.'},
                    'max_results': {'type': 'integer',
                                    'minimum': 1,
                                    'maximum': 250,
                                    'default': 50}}})
@declare_output_schema({'properties': {'items': {'items': {'type': 'object'}, 'type': 'array'}},
                        'required': ['items'],
                        'type': 'object'})
async def google_calendar_list_events(credentials: Dict[str, Any],
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 50,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
    }
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max

    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="GET",
        base_url=CALENDAR_API_BASE_URL,
        path=f"/calendars/{calendar_id}/events",
        params=params,
        scopes=CALENDAR_SCOPE,
    )
    return {"items": data.get("items", [])}



# credential_headers: ['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /calendars/{cid}/events/{eid},
# the same shape as the two Outlook update tools.
# Evidence: PATCH /calendars/{calendar_id}/events/{event_id}
@server.tool(
    name='google_calendar_update_event',
    description="Update fields of an existing event in the selected Google Calendar.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Google-Refresh-Token', 'X-Google-Client-Id', 'X-Google-Client-Secret'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'calendar_id': {'type': 'string', 'default': 'primary'},
                    'event_id': {'type': 'string',
                                 'description': 'ID of the event to update.'},
                    'summary': {'type': 'string'},
                    'description': {'type': 'string'},
                    'start_datetime': {'type': 'string',
                                       'description': 'Event start in RFC3339 format.'},
                    'end_datetime': {'type': 'string',
                                     'description': 'Event end in RFC3339 format.'},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone (e.g. Asia/Kolkata).'}},
     'required': ['event_id']})
@declare_output_schema({'properties': {'htmlLink': {'type': 'string'},
                                       'id': {'type': 'string'},
                                       'status': {'type': 'string'}},
                        'required': ['id'],
                        'type': 'object'})
async def google_calendar_update_event(credentials: Dict[str, Any],
    event_id: str,
    calendar_id: str = "primary",
    summary: Optional[str] = None,
    description: Optional[str] = None,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    def _event_time(dt: Optional[str], tz: Optional[str]) -> Optional[Dict[str, Any]]:
        # Google's PATCH requires dateTime whenever start/end is present, so a
        # timezone with no accompanying datetime can't be sent on its own.
        if dt is None:
            return None
        return {"dateTime": dt, "timeZone": tz or "UTC"}

    body: Dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description

    # timezone is applied to both start and end regardless of which
    # datetime(s) are being changed, since a real event's two sides should
    # never end up with mismatched timezones.
    start = _event_time(start_datetime, timezone)
    if start:
        body["start"] = start
    end = _event_time(end_datetime, timezone)
    if end:
        body["end"] = end

    data = await _google_request(  # type: ignore[attr-defined]
        credentials=credentials,
        method="PATCH",
        base_url=CALENDAR_API_BASE_URL,
        path=f"/calendars/{calendar_id}/events/{event_id}",
        json_data=body,
        scopes=CALENDAR_SCOPE,
    )
    return data
