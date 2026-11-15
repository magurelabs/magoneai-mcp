"""The surveymonkey connector.

Ported from the v1 mixin form by a codemod.

40 tools, 19 of them proposed read-only. Every input schema
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

SERVER_NAME = "surveymonkey"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

# One pattern across connectors: the connector, its version, and where to read
# about it, in the `Mozilla/5.0 (compatible; ...)` form crawlers use, which many
# hosts accept where they refuse an unfamiliar string.
USER_AGENT = (
    f"Mozilla/5.0 (compatible; magoneai-{SERVER_NAME}/{SERVER_VERSION}; +https://github.com/magurelabs/magoneai-mcp)"
)

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
import httpx
from .tools.client import CREDENTIAL_HEADERS
from .tools.collector_settings import COLLECTOR_SETTINGS_SCHEMA
from typing import Any, Dict, List, Optional
from .tools.client import CREDENTIAL_HEADERS
from typing import Any, Dict
from .tools.collector_settings import COLLECTOR_SETTINGS_SCHEMA, COLLECTOR_SORT_FIELDS, COLLECTOR_TYPES, SORT_ORDERS
from .tools.filters import DATE_FILTER_PROPS, RESPONSE_LIST_EXTRA_PROPS
from .tools.filters import DATE_FILTER_PROPS


BASE_URL = "https://api.surveymonkey.com/v3"

CREDENTIAL_HEADERS = ["X-Surveymonkey-Access-Token"]

def _token(credentials: Dict[str, str]) -> str:
    token = credentials.get("surveymonkey_access_token") or credentials.get("access_token")
    if not token:
        raise ValueError("SurveyMonkey access token is required")
    return token

def compact(params: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}

def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        err = body.get("error") or {}
        name = err.get("name") or response.reason_phrase
        message = err.get("message") or ""
        code = err.get("id")
        return f"{name} (code {code}): {message}".strip() if code is not None else str(name)
    except Exception:
        return response.text[:500]

async def request(
    credentials: Dict[str, str],
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    """Call the SurveyMonkey v3 API and raise ValueError with a clear message on failure.

    Connection-level failures (DNS, firewall/proxy blocking, timeout) are
    caught explicitly here rather than left to propagate as a raw httpx
    exception - unhandled, protocol.py's generic exception handler reports
    them as an opaque "Internal error (ConnectTimeout)" with no indication
    it's a network problem, not a bug (this is exactly what happened
    debugging a real deployment - the fix belongs here, not in a resend).
    """
    token = _token(credentials)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
            response = await client.request(
                method,
                path,
                params=compact(params or {}),
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
    except httpx.ConnectError as exc:
        raise ValueError(
            "Could not connect to SurveyMonkey's API (api.surveymonkey.com). This is a "
            "network problem, not a bad request - check for a firewall, proxy, or VPN "
            "blocking outbound HTTPS from wherever this server is running."
        ) from exc
    except httpx.TimeoutException as exc:
        raise ValueError(
            "SurveyMonkey API request timed out. If this keeps happening, the same "
            "network path is likely blocking or throttling the connection - check "
            "firewall/proxy/VPN settings rather than retrying."
        ) from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"SurveyMonkey API request failed: {exc}") from exc

    if response.status_code == 401:
        raise ValueError(f"SurveyMonkey authentication failed: {_error_message(response)}")
    if response.status_code == 403:
        raise ValueError(f"SurveyMonkey authorization failed: {_error_message(response)}")
    if response.status_code == 429:
        reset = response.headers.get("X-Ratelimit-App-Global-Minute-Reset") or response.headers.get(
            "X-Ratelimit-App-Global-Day-Reset"
        )
        suffix = f"; resets in {reset}s" if reset else ""
        raise ValueError(f"SurveyMonkey rate limit exceeded{suffix}")
    if response.status_code in (502, 503):
        raise ValueError(f"SurveyMonkey API temporarily unavailable: {_error_message(response)}")
    if response.status_code >= 400:
        raise ValueError(
            f"SurveyMonkey API error ({response.status_code}): {_error_message(response)}"
        )

    if not response.content:
        return None
    return response.json()

_STATUSES = ("open", "closed")

_MESSAGE_TYPES = ("invite", "reminder", "thank_you", "sms")

_INCLUDE_FIELDS = (
    "survey_response_status",
    "mail_status",
    "custom_fields",
    "remove_link",
    "extra_fields",
    "survey_link",
)

COLLECTOR_TYPES = ("weblink", "email", "sms", "popup_invitation", "embedded_survey", "popup_survey")

EDIT_RESPONSE_TYPES = ("until_complete", "never", "always")

ANONYMOUS_TYPES = ("not_anonymous", "partially_anonymous", "fully_anonymous")

REDIRECT_TYPES = ("url", "close", "loop")

COLLECTOR_SORT_FIELDS = ("id", "date_modified", "type", "status", "name")

SORT_ORDERS = ("ASC", "DESC")

COLLECTOR_SETTINGS_SCHEMA = {
    "name": {"type": "string", "description": "Collector display name."},
    "thank_you_message": {"type": "string"},
    "disqualification_message": {"type": "string"},
    "disqualification_url": {"type": "string", "description": "Redirect here on disqualification."},
    "close_date": {
        "type": "string",
        "description": "ISO 8601. Collector auto-closes at this time.",
    },
    "closed_page_message": {"type": "string", "description": "Shown to respondents once closed."},
    "redirect_url": {"type": "string", "description": "Redirect respondents here on completion."},
    "redirect_type": {
        "type": "string",
        "enum": list(REDIRECT_TYPES),
        "description": "'url' uses redirect_url, 'close' closes the tab, 'loop' restarts the survey (weblink + allow_multiple_responses only).",
    },
    "display_survey_results": {
        "type": "boolean",
        "description": "Show respondents instant results after they complete the survey.",
    },
    "edit_response_type": {
        "type": "string",
        "enum": list(EDIT_RESPONSE_TYPES),
        "description": "When respondents can edit their submitted response.",
    },
    "anonymous_type": {
        "type": "string",
        "enum": list(ANONYMOUS_TYPES),
        "description": "Turns off IP tracking; 'fully_anonymous' on an email collector also strips respondent identity.",
    },
    "allow_multiple_responses": {
        "type": "boolean",
        "description": "Let the same browser/device respond more than once. Not available for email collectors.",
    },
    "password": {"type": "string", "description": "Require a password to access the survey."},
    "sender_email": {"type": "string", "description": "Sender address for email collectors."},
    "response_limit": {"type": "integer", "description": "Auto-close after this many responses."},
}

def collector_settings_body(
    name: Optional[str] = None,
    thank_you_message: Optional[str] = None,
    disqualification_message: Optional[str] = None,
    disqualification_url: Optional[str] = None,
    close_date: Optional[str] = None,
    closed_page_message: Optional[str] = None,
    redirect_url: Optional[str] = None,
    redirect_type: Optional[str] = None,
    display_survey_results: Optional[bool] = None,
    edit_response_type: Optional[str] = None,
    anonymous_type: Optional[str] = None,
    allow_multiple_responses: Optional[bool] = None,
    password: Optional[str] = None,
    sender_email: Optional[str] = None,
    response_limit: Optional[int] = None,
) -> Dict[str, Any]:
    if redirect_type and redirect_type not in REDIRECT_TYPES:
        raise ValueError(f"redirect_type must be one of {REDIRECT_TYPES}")
    if edit_response_type and edit_response_type not in EDIT_RESPONSE_TYPES:
        raise ValueError(f"edit_response_type must be one of {EDIT_RESPONSE_TYPES}")
    if anonymous_type and anonymous_type not in ANONYMOUS_TYPES:
        raise ValueError(f"anonymous_type must be one of {ANONYMOUS_TYPES}")
    return compact(
        {
            "name": name,
            "thank_you_message": thank_you_message,
            "disqualification_message": disqualification_message,
            "disqualification_url": disqualification_url,
            "close_date": close_date,
            "closed_page_message": closed_page_message,
            "redirect_url": redirect_url,
            "redirect_type": redirect_type,
            "display_survey_results": display_survey_results,
            "edit_response_type": edit_response_type,
            "anonymous_type": anonymous_type,
            "allow_multiple_responses": allow_multiple_responses,
            "password": password,
            "sender_email": sender_email,
            "response_limit": response_limit,
        }
    )

# From v1 `tools/collectors.py`. v1 had two modules each with its own
# `_DEFAULT_INCLUDE`; flattened into one, the surveys value (defined later)
# overwrote this one, so list_collectors silently stopped asking for type,
# status, date_created and url.
_COLLECTOR_DEFAULT_INCLUDE = "type,status,response_count,date_created,date_modified,url"

_CONTACT_STATUSES = ("active", "optout", "bounced")

_CONTACT_SORT_SEARCH_FIELDS = ("email", "first_name", "last_name", "1", "2", "3", "4", "5", "6")

_SORT_ORDERS = ("ASC", "DESC")

STATUSES = ("completed", "partial", "overquota", "disqualified")

TIME_UNITS = ("second", "minute", "hour")

DATE_FILTER_PROPS = {
    "collector_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Only include responses from these collector ids.",
    },
    "status": {"type": "string", "enum": list(STATUSES)},
    "start_created_at": {"type": "string", "description": "ISO 8601. Response started after this."},
    "end_created_at": {"type": "string", "description": "ISO 8601. Response started before this."},
    "start_modified_at": {
        "type": "string",
        "description": "ISO 8601. Response last modified after this.",
    },
    "end_modified_at": {
        "type": "string",
        "description": "ISO 8601. Response last modified before this.",
    },
    "email": {
        "type": "string",
        "description": "Filter to one respondent's email (email collectors only).",
    },
    "first_name": {
        "type": "string",
        "description": "Filter by respondent first name (email collectors only).",
    },
    "last_name": {
        "type": "string",
        "description": "Filter by respondent last name (email collectors only).",
    },
    "ip": {"type": "string", "description": "Filter to responses from this IP address."},
    "custom": {
        "type": "string",
        "description": "Filter by SurveyMonkey's custom_value field on the response.",
    },
    "total_time_min": {
        "type": "integer",
        "description": "Only responses that took at least this long.",
    },
    "total_time_max": {
        "type": "integer",
        "description": "Only responses that took at most this long.",
    },
    "total_time_units": {
        "type": "string",
        "enum": list(TIME_UNITS),
        "description": "Unit for total_time_min/total_time_max. Default seconds.",
    },
}

def response_filters(
    collector_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    start_created_at: Optional[str] = None,
    end_created_at: Optional[str] = None,
    start_modified_at: Optional[str] = None,
    end_modified_at: Optional[str] = None,
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    ip: Optional[str] = None,
    custom: Optional[str] = None,
    total_time_min: Optional[int] = None,
    total_time_max: Optional[int] = None,
    total_time_units: Optional[str] = None,
) -> Dict[str, Any]:
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if total_time_units and total_time_units not in TIME_UNITS:
        raise ValueError(f"total_time_units must be one of {TIME_UNITS}")
    return compact(
        {
            "collector_ids": ",".join(collector_ids) if collector_ids else None,
            "status": status,
            "start_created_at": start_created_at,
            "end_created_at": end_created_at,
            "start_modified_at": start_modified_at,
            "end_modified_at": end_modified_at,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "ip": ip,
            "custom": custom,
            "total_time_min": total_time_min,
            "total_time_max": total_time_max,
            "total_time_units": total_time_units,
        }
    )

RESPONSE_SORT_FIELDS = ("date_modified",)

RESPONSE_LIST_EXTRA_PROPS = {
    "sort_by": {"type": "string", "enum": list(RESPONSE_SORT_FIELDS)},
    "sort_order": {"type": "string", "enum": ["ASC", "DESC"]},
    "page_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Only include answers from these survey pages. Default: all pages.",
    },
    "question_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Only include answers to these questions. Default: all questions.",
    },
}

def response_list_extra_params(
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page_ids: Optional[List[str]] = None,
    question_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if sort_by and sort_by not in RESPONSE_SORT_FIELDS:
        raise ValueError(f"sort_by must be one of {RESPONSE_SORT_FIELDS}")
    if sort_order and sort_order not in ("ASC", "DESC"):
        raise ValueError("sort_order must be one of ('ASC', 'DESC')")
    return compact(
        {
            "sort_by": sort_by,
            "sort_order": sort_order,
            "page_ids": ",".join(page_ids) if page_ids else None,
            "question_ids": ",".join(question_ids) if question_ids else None,
        }
    )

_QUESTION_TYPES = (
    "single_choice",
    "multiple_choice",
    "short_text",
    "long_text",
    "rating_scale",
    "advanced",
)

_EMOJI_STYLES = ("star", "smiley", "heart", "thumb")

_LIST_SCHEMA_PROPS = {**DATE_FILTER_PROPS, **RESPONSE_LIST_EXTRA_PROPS}

def _list_filters(
    collector_ids,
    status,
    start_created_at,
    end_created_at,
    start_modified_at,
    end_modified_at,
    email,
    first_name,
    last_name,
    ip,
    custom,
    total_time_min,
    total_time_max,
    total_time_units,
    sort_by,
    sort_order,
    page_ids,
    question_ids,
) -> Dict[str, Any]:
    return {
        **response_filters(
            collector_ids,
            status,
            start_created_at,
            end_created_at,
            start_modified_at,
            end_modified_at,
            email,
            first_name,
            last_name,
            ip,
            custom,
            total_time_min,
            total_time_max,
            total_time_units,
        ),
        **response_list_extra_params(sort_by, sort_order, page_ids, question_ids),
    }

# From v1 `tools/surveys.py`.
_SURVEY_DEFAULT_INCLUDE = "response_count,date_modified,question_count"

_EVENT_TYPES = (
    "response_completed",
    "response_updated",
    "response_disqualified",
    "response_created",
    "response_deleted",
    "response_overquota",
    "collector_created",
    "collector_updated",
    "collector_deleted",
    "survey_created",
    "survey_updated",
    "survey_deleted",
)

_OBJECT_TYPES = ("survey", "collector")



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: POST .../contacts/bulk with
# update_existing, which tells SurveyMonkey to overwrite the fields of
# contacts already in the list. The spec's wording is "may", and an
# opt-in overwrite still qualifies.
# Evidence: POST /contact_lists/{contact_list_id}/contacts/bulk
@server.tool(
    name='surveymonkey_add_contacts_bulk',
    description="Add multiple contacts to a contact list in one call. Each contact needs first_name, last_name, and either email or phone_number.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'contact_list_id': {'type': 'string'},
                    'contacts': {'type': 'array',
                                 'items': {'type': 'object',
                                           'properties': {'first_name': {'type': 'string'},
                                                          'last_name': {'type': 'string'},
                                                          'email': {'type': 'string'},
                                                          'phone_number': {'type': 'string'},
                                                          'custom_fields': {'type': 'object'}},
                                           'required': ['first_name', 'last_name']}},
                    'update_existing': {'type': 'boolean', 'default': False}},
     'required': ['contact_list_id', 'contacts']})
async def surveymonkey_add_contacts_bulk(credentials: Dict[str, str],
    contact_list_id: str,
    contacts: List[Dict[str, Any]],
    update_existing: bool = False,
) -> Dict[str, Any]:
    for c in contacts:
        if not c.get("email") and not c.get("phone_number"):
            raise ValueError("each contact needs an email or phone_number")
    body = {"contacts": contacts, "update_existing": update_existing}
    data = await request(
        credentials, "POST", f"/contact_lists/{contact_list_id}/contacts/bulk", json_body=body
    )
    return {
        "success": True,
        "succeeded": data.get("succeeded", []),
        "invalids": data.get("invalids", []),
        "existing": data.get("existing", []),
    }



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST, path built at runtime
@server.tool(
    name='surveymonkey_add_message_recipients',
    description="Add recipients to a not-yet-sent message, from existing contact ids, whole contact lists, or raw contact info (a new contact is created if no existing one matches the email/phone).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'},
                    'message_id': {'type': 'string'},
                    'contact_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'contact_list_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'contacts': {'type': 'array',
                                 'items': {'type': 'object',
                                           'properties': {'email': {'type': 'string'},
                                                          'phone_number': {'type': 'string'},
                                                          'first_name': {'type': 'string'},
                                                          'last_name': {'type': 'string'}}},
                                 'description': 'Raw contact info for people not already '
                                                'in the address book.'}},
     'required': ['collector_id', 'message_id']})
async def surveymonkey_add_message_recipients(credentials: Dict[str, str],
    collector_id: str,
    message_id: str,
    contact_ids: Optional[List[str]] = None,
    contact_list_ids: Optional[List[str]] = None,
    contacts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not contact_ids and not contact_list_ids and not contacts:
        raise ValueError(
            "at least one of contact_ids, contact_list_ids, or contacts is required"
        )
    body = compact(
        {"contact_ids": contact_ids, "contact_list_ids": contact_list_ids, "contacts": contacts}
    )
    data = await request(
        credentials,
        "POST",
        f"/collectors/{collector_id}/messages/{message_id}/recipients/bulk",
        json_body=body,
    )
    return {
        "success": True,
        "succeeded": data.get("succeeded", []),
        "invalids": data.get("invalids", []),
        "bounced": data.get("bounced", []),
        "opted_out": data.get("opted_out", []),
    }



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /surveys/{survey_id}/pages
@server.tool(
    name='surveymonkey_add_page',
    description="Add a new, empty page to a survey. Add questions to it afterwards with surveymonkey_add_question, using the returned page id.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'title': {'type': 'string'},
                    'description': {'type': 'string'}},
     'required': ['survey_id']})
async def surveymonkey_add_page(credentials: Dict[str, str],
    survey_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    body = compact({"title": title, "description": description})
    data = await request(credentials, "POST", f"/surveys/{survey_id}/pages", json_body=body)
    return {"success": True, "page": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /surveys/{survey_id}/pages/{page_id}/questions
@server.tool(
    name='surveymonkey_add_question',
    description="Add a question to a survey page - the same question types SurveyMonkey's own editor offers. Set `type` first, then fill in what's relevant to it: 'single_choice' (radio buttons - pick one; needs `choices`), 'multiple_choice' (checkboxes - pick any; needs `choices`), 'short_text' (one-line answer box), 'long_text' (paragraph/comment box), 'rating_scale' (a 1..scale_max scale, optionally captioned with low_label/high_label, optionally styled as star/emoji via `style`). `heading` (the question text) is required for all of those. Use type='advanced' with a raw `question` object only for something these five don't cover (matrix menu, demographic, datetime, file upload, slider, quiz scoring, ...) - surveymonkey_search_question_bank gives a ready-made example to adapt for that case.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'page_id': {'type': 'string'},
                    'type': {'type': 'string',
                             'enum': ['single_choice',
                                      'multiple_choice',
                                      'short_text',
                                      'long_text',
                                      'rating_scale',
                                      'advanced']},
                    'heading': {'type': 'string',
                                'description': 'The question text. Required for every type '
                                               "except 'advanced'."},
                    'choices': {'type': 'array',
                                'items': {'type': 'string'},
                                'description': 'Answer options in order. Required for '
                                               'single_choice/multiple_choice.'},
                    'scale_max': {'type': 'integer',
                                  'default': 5,
                                  'description': 'rating_scale only: number of points, '
                                                 'e.g. 5 or 10.'},
                    'low_label': {'type': 'string',
                                  'description': 'rating_scale only: caption for the '
                                                 'lowest point.'},
                    'high_label': {'type': 'string',
                                   'description': 'rating_scale only: caption for the '
                                                  'highest point.'},
                    'style': {'type': 'string',
                              'enum': ['star', 'smiley', 'heart', 'thumb'],
                              'description': 'rating_scale only: render as a star/emoji '
                                             'rating instead of numbers.'},
                    'required': {'type': 'boolean', 'default': False},
                    'question': {'type': 'object',
                                 'description': "type='advanced' only: raw SurveyMonkey "
                                                'question body (family, subtype, headings, '
                                                'answers, ...).'}},
     'required': ['survey_id', 'page_id', 'type']})
async def surveymonkey_add_question(credentials: Dict[str, str],
    survey_id: str,
    page_id: str,
    type: str,
    heading: Optional[str] = None,
    choices: Optional[List[str]] = None,
    scale_max: int = 5,
    low_label: Optional[str] = None,
    high_label: Optional[str] = None,
    style: Optional[str] = None,
    required: bool = False,
    question: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if type not in _QUESTION_TYPES:
        raise ValueError(f"type must be one of {_QUESTION_TYPES}")

    if type == "advanced":
        if not isinstance(question, dict):
            raise ValueError("question (an object) is required when type='advanced'")
        if not question.get("family") or not question.get("subtype"):
            raise ValueError("question.family and question.subtype are required")
        if not question.get("headings"):
            raise ValueError("question.headings is required: a list of {'heading': '...'}")
        body = question
    else:
        if not heading:
            raise ValueError("heading is required unless type='advanced'")
        body: Dict[str, Any] = {"headings": [{"heading": heading}]}

        if type in ("single_choice", "multiple_choice"):
            if not choices:
                raise ValueError("choices must have at least one option for this type")
            body["family"] = "multiple_choice" if type == "multiple_choice" else "single_choice"
            body["subtype"] = "vertical"
            body["answers"] = {"choices": [{"text": c} for c in choices]}
        elif type in ("short_text", "long_text"):
            body["family"] = "open_ended"
            body["subtype"] = "essay" if type == "long_text" else "single"
        elif type == "rating_scale":
            if scale_max < 2:
                raise ValueError("scale_max must be at least 2")
            if style and style not in _EMOJI_STYLES:
                raise ValueError(f"style must be one of {_EMOJI_STYLES}")
            rating_choices = [{"weight": i, "text": str(i)} for i in range(1, scale_max + 1)]
            if low_label:
                rating_choices[0]["text"] = low_label
            if high_label:
                rating_choices[-1]["text"] = high_label
            body["family"] = "matrix"
            body["subtype"] = "rating"
            body["forced_ranking"] = False
            body["answers"] = {"rows": [{"text": "", "position": 1}], "choices": rating_choices}
            if style:
                body["display_options"] = {"display_type": "emoji", "display_subtype": style}

        if required:
            # SurveyMonkey rejects `required` with only `text` for non-matrix
            # families (400 "not valid under any of the given schemas") -
            # `type`/`amount` are mandatory even for a plain yes/no requirement,
            # confirmed live against a real account, 2026-08.
            body["required"] = {
                "text": f"Please answer: {heading}",
                "type": "all",
                "amount": "0",
            }

    data = await request(
        credentials, "POST", f"/surveys/{survey_id}/pages/{page_id}/questions", json_body=body
    )
    return {"success": True, "question": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /surveys/{survey_id}/collectors
@server.tool(
    name='surveymonkey_create_collector',
    description="Create a new distribution collector for a survey. 'weblink' gives a shareable survey URL; 'email' and 'sms' send invites (compose/send via surveymonkey_create_message and friends); the popup_* types embed the survey on a website. The optional settings cover the same behavior toggles as SurveyMonkey's own collector settings screen (completion redirect, anonymity, password, response limit, ...).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'type': {'type': 'string',
                             'enum': ['weblink',
                                      'email',
                                      'sms',
                                      'popup_invitation',
                                      'embedded_survey',
                                      'popup_survey']},
                    'name': {'type': 'string', 'description': 'Collector display name.'},
                    'thank_you_message': {'type': 'string'},
                    'disqualification_message': {'type': 'string'},
                    'disqualification_url': {'type': 'string',
                                             'description': 'Redirect here on '
                                                            'disqualification.'},
                    'close_date': {'type': 'string',
                                   'description': 'ISO 8601. Collector auto-closes at this '
                                                  'time.'},
                    'closed_page_message': {'type': 'string',
                                            'description': 'Shown to respondents once '
                                                           'closed.'},
                    'redirect_url': {'type': 'string',
                                     'description': 'Redirect respondents here on '
                                                    'completion.'},
                    'redirect_type': {'type': 'string',
                                      'enum': ['url', 'close', 'loop'],
                                      'description': "'url' uses redirect_url, 'close' "
                                                     "closes the tab, 'loop' restarts the "
                                                     'survey (weblink + '
                                                     'allow_multiple_responses only).'},
                    'display_survey_results': {'type': 'boolean',
                                               'description': 'Show respondents instant '
                                                              'results after they complete '
                                                              'the survey.'},
                    'edit_response_type': {'type': 'string',
                                           'enum': ['until_complete', 'never', 'always'],
                                           'description': 'When respondents can edit their '
                                                          'submitted response.'},
                    'anonymous_type': {'type': 'string',
                                       'enum': ['not_anonymous',
                                                'partially_anonymous',
                                                'fully_anonymous'],
                                       'description': 'Turns off IP tracking; '
                                                      "'fully_anonymous' on an email "
                                                      'collector also strips respondent '
                                                      'identity.'},
                    'allow_multiple_responses': {'type': 'boolean',
                                                 'description': 'Let the same '
                                                                'browser/device respond '
                                                                'more than once. Not '
                                                                'available for email '
                                                                'collectors.'},
                    'password': {'type': 'string',
                                 'description': 'Require a password to access the survey.'},
                    'sender_email': {'type': 'string',
                                     'description': 'Sender address for email collectors.'},
                    'response_limit': {'type': 'integer',
                                       'description': 'Auto-close after this many '
                                                      'responses.'}},
     'required': ['survey_id', 'type']})
async def surveymonkey_create_collector(credentials: Dict[str, str],
    survey_id: str,
    type: str,
    name: Optional[str] = None,
    thank_you_message: Optional[str] = None,
    disqualification_message: Optional[str] = None,
    disqualification_url: Optional[str] = None,
    close_date: Optional[str] = None,
    closed_page_message: Optional[str] = None,
    redirect_url: Optional[str] = None,
    redirect_type: Optional[str] = None,
    display_survey_results: Optional[bool] = None,
    edit_response_type: Optional[str] = None,
    anonymous_type: Optional[str] = None,
    allow_multiple_responses: Optional[bool] = None,
    password: Optional[str] = None,
    sender_email: Optional[str] = None,
    response_limit: Optional[int] = None,
) -> Dict[str, Any]:
    if type not in COLLECTOR_TYPES:
        raise ValueError(f"type must be one of {COLLECTOR_TYPES}")
    body = {
        "type": type,
        **collector_settings_body(
            name=name,
            thank_you_message=thank_you_message,
            disqualification_message=disqualification_message,
            disqualification_url=disqualification_url,
            close_date=close_date,
            closed_page_message=closed_page_message,
            redirect_url=redirect_url,
            redirect_type=redirect_type,
            display_survey_results=display_survey_results,
            edit_response_type=edit_response_type,
            anonymous_type=anonymous_type,
            allow_multiple_responses=allow_multiple_responses,
            password=password,
            sender_email=sender_email,
            response_limit=response_limit,
        ),
    }
    data = await request(
        credentials, "POST", f"/surveys/{survey_id}/collectors", json_body=body
    )
    return {"success": True, "collector": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /contact_lists
@server.tool(
    name='surveymonkey_create_contact_list',
    description="Create a new contact list to hold recipients for email/sms invites.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {'name': {'type': 'string'}}})
async def surveymonkey_create_contact_list(credentials: Dict[str, str], name: Optional[str] = None
) -> Dict[str, Any]:
    data = await request(
        credentials, "POST", "/contact_lists", json_body=compact({"name": name})
    )
    return {"success": True, "contact_list": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /survey_folders
@server.tool(
    name='surveymonkey_create_folder',
    description="Create a new survey folder.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {'title': {'type': 'string'}}})
async def surveymonkey_create_folder(credentials: Dict[str, str], title: Optional[str] = None
) -> Dict[str, Any]:
    data = await request(
        credentials, "POST", "/survey_folders", json_body=compact({"title": title})
    )
    return {"success": True, "folder": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /collectors/{collector_id}/messages
@server.tool(
    name='surveymonkey_create_message',
    description="Create an invite/reminder/thank_you/sms message for an email or sms collector. Add recipients with surveymonkey_add_message_recipients, then send with surveymonkey_send_message. Custom body_text/body_html must include [SurveyLink], [OptOutLink], [PrivacyLink] and [FooterLink] placeholders - omit them to use SurveyMonkey's default template.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'},
                    'type': {'type': 'string',
                             'enum': ['invite', 'reminder', 'thank_you', 'sms'],
                             'default': 'invite'},
                    'subject': {'type': 'string',
                                'description': "Default: 'We want your opinion'."},
                    'body_text': {'type': 'string'},
                    'body_html': {'type': 'string'},
                    'is_branding_enabled': {'type': 'boolean', 'default': True}},
     'required': ['collector_id']})
async def surveymonkey_create_message(credentials: Dict[str, str],
    collector_id: str,
    type: str = "invite",
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    is_branding_enabled: bool = True,
) -> Dict[str, Any]:
    if type not in _MESSAGE_TYPES:
        raise ValueError(f"type must be one of {_MESSAGE_TYPES}")
    body = compact(
        {
            "type": type,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "is_branding_enabled": is_branding_enabled,
        }
    )
    data = await request(
        credentials, "POST", f"/collectors/{collector_id}/messages", json_body=body
    )
    return {"success": True, "message": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /surveys
@server.tool(
    name='surveymonkey_create_survey',
    description="Create a survey. Three mutually exclusive modes: (1) blank survey - just a title, then add pages/questions with surveymonkey_add_page and surveymonkey_add_question; (2) from_template_id - prepopulated from a SurveyMonkey template (see surveymonkey_list_survey_templates); (3) from_survey_id - a copy of an existing survey. A brand-new survey needs at least one page and one question before it can accept responses.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'title': {'type': 'string', 'description': "Default: 'New Survey'."},
                    'from_template_id': {'type': 'string'},
                    'from_survey_id': {'type': 'string'},
                    'folder_id': {'type': 'string'},
                    'language': {'type': 'string',
                                 'description': "ISO 639-1 code, default 'en'."}}})
async def surveymonkey_create_survey(credentials: Dict[str, str],
    title: Optional[str] = None,
    from_template_id: Optional[str] = None,
    from_survey_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    if from_template_id and from_survey_id:
        raise ValueError("from_template_id and from_survey_id are mutually exclusive")
    body = compact(
        {
            "title": title,
            "from_template_id": from_template_id,
            "from_survey_id": from_survey_id,
            "folder_id": folder_id,
            "language": language,
        }
    )
    data = await request(credentials, "POST", "/surveys", json_body=body)
    return {"success": True, "survey": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /webhooks
@server.tool(
    name='surveymonkey_create_webhook',
    description="Subscribe a URL to a SurveyMonkey event, e.g. to push new responses instead of polling. subscription_url must accept a HEAD request returning 200, and must be unique across every webhook in the account regardless of event type. Omit object_type/object_ids for the survey_created event; both are required otherwise.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'name': {'type': 'string'},
                    'event_type': {'type': 'string',
                                   'enum': ['response_completed',
                                            'response_updated',
                                            'response_disqualified',
                                            'response_created',
                                            'response_deleted',
                                            'response_overquota',
                                            'collector_created',
                                            'collector_updated',
                                            'collector_deleted',
                                            'survey_created',
                                            'survey_updated',
                                            'survey_deleted']},
                    'object_type': {'type': 'string',
                                    'enum': ['survey', 'collector'],
                                    'description': 'Omit for survey_created.'},
                    'object_ids': {'type': 'array',
                                   'items': {'type': 'string'},
                                   'description': 'Survey or collector ids to filter on. '
                                                  'Omit for survey_created.'},
                    'subscription_url': {'type': 'string'}},
     'required': ['name', 'event_type', 'subscription_url']})
async def surveymonkey_create_webhook(credentials: Dict[str, str],
    name: str,
    event_type: str,
    subscription_url: str,
    object_type: Optional[str] = None,
    object_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"event_type must be one of {_EVENT_TYPES}")
    if object_type and object_type not in _OBJECT_TYPES:
        raise ValueError(f"object_type must be one of {_OBJECT_TYPES}")
    if event_type == "survey_created":
        if object_type is not None or object_ids is not None:
            raise ValueError("object_type and object_ids must be omitted for survey_created")
    elif not object_type or not object_ids:
        raise ValueError(
            "object_type and object_ids are required for every event_type except survey_created"
        )
    body = compact(
        {
            "name": name,
            "event_type": event_type,
            "object_type": object_type,
            "object_ids": object_ids,
            "subscription_url": subscription_url,
        }
    )
    data = await request(credentials, "POST", "/webhooks", json_body=body)
    return {"success": True, "webhook": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /collectors/{collector_id}
@server.tool(
    name='surveymonkey_delete_collector',
    description="PERMANENTLY delete a collector. This stops it from collecting further responses and removes its distribution link - only call this when the user has explicitly confirmed they want it deleted.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'}},
     'required': ['collector_id']})
async def surveymonkey_delete_collector(credentials: Dict[str, str], collector_id: str
) -> Dict[str, Any]:
    await request(credentials, "DELETE", f"/collectors/{collector_id}")
    return {"success": True, "deleted_collector_id": collector_id}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /surveys/{survey_id}/pages/{page_id}
@server.tool(
    name='surveymonkey_delete_page',
    description="PERMANENTLY delete a page and every question on it. This cannot be undone - only call this when the user has explicitly confirmed, not as a cleanup step you decide on.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'}, 'page_id': {'type': 'string'}},
     'required': ['survey_id', 'page_id']})
async def surveymonkey_delete_page(credentials: Dict[str, str], survey_id: str, page_id: str
) -> Dict[str, Any]:
    await request(credentials, "DELETE", f"/surveys/{survey_id}/pages/{page_id}")
    return {"success": True, "deleted_page_id": page_id}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE, path built at runtime
@server.tool(
    name='surveymonkey_delete_question',
    description="PERMANENTLY delete a question. This cannot be undone - only call this when the user has explicitly confirmed, not as a cleanup step you decide on your own.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'page_id': {'type': 'string'},
                    'question_id': {'type': 'string'}},
     'required': ['survey_id', 'page_id', 'question_id']})
async def surveymonkey_delete_question(credentials: Dict[str, str], survey_id: str, page_id: str, question_id: str
) -> Dict[str, Any]:
    await request(
        credentials, "DELETE", f"/surveys/{survey_id}/pages/{page_id}/questions/{question_id}"
    )
    return {"success": True, "deleted_question_id": question_id}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /surveys/{survey_id}
@server.tool(
    name='surveymonkey_delete_survey',
    description="PERMANENTLY delete a survey, including its pages, questions, and all collected responses. This cannot be undone - only call this when the user has explicitly confirmed they want the survey deleted, not as a cleanup step you decide on your own.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'}},
     'required': ['survey_id']})
async def surveymonkey_delete_survey(credentials: Dict[str, str], survey_id: str
) -> Dict[str, Any]:
    await request(credentials, "DELETE", f"/surveys/{survey_id}")
    return {"success": True, "deleted_survey_id": survey_id}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /webhooks/{webhook_id}
@server.tool(
    name='surveymonkey_delete_webhook',
    description="Delete a webhook subscription.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'webhook_id': {'type': 'string'}},
     'required': ['webhook_id']})
async def surveymonkey_delete_webhook(credentials: Dict[str, str], webhook_id: str
) -> Dict[str, Any]:
    await request(credentials, "DELETE", f"/webhooks/{webhook_id}")
    return {"success": True, "deleted_webhook_id": webhook_id}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /collectors/{collector_id}/messages/{message_id}/stats
@server.tool(
    name='surveymonkey_get_message_stats',
    description="Get delivery/open/click and response stats for one message, or omit message_id for combined stats across every message sent from the collector.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'}, 'message_id': {'type': 'string'}},
     'required': ['collector_id']})
async def surveymonkey_get_message_stats(credentials: Dict[str, str], collector_id: str, message_id: Optional[str] = None
) -> Dict[str, Any]:
    path = (
        f"/collectors/{collector_id}/messages/{message_id}/stats"
        if message_id
        else f"/collectors/{collector_id}/stats"
    )
    data = await request(credentials, "GET", path)
    return {"success": True, "stats": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/responses/{response_id}/details
@server.tool(
    name='surveymonkey_get_response_detail',
    description="Fetch one full response (every question's answers) by response id.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'response_id': {'type': 'string'},
                    'simple': {'type': 'boolean',
                               'default': True,
                               'description': 'Include question/answer text, not just '
                                              'ids.'}},
     'required': ['survey_id', 'response_id']})
async def surveymonkey_get_response_detail(credentials: Dict[str, str],
    survey_id: str,
    response_id: str,
    simple: bool = True,
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "GET",
        f"/surveys/{survey_id}/responses/{response_id}/details",
        params=compact({"simple": simple}),
    )
    return {"success": True, "response": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/rollups
@server.tool(
    name='surveymonkey_get_response_summary',
    description="Get per-question aggregate rollups for a survey: answered/skipped counts, per-choice counts, and basic stats (mean/median/min/max) for close-ended questions. Cheaper than pulling every response when only aggregate numbers are needed, e.g. 'how did the NPS survey do'. Supports the same filters as surveymonkey_list_responses (status, dates, respondent identity, time spent).",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'collector_ids': {'type': 'array',
                                      'items': {'type': 'string'},
                                      'description': 'Only include responses from these '
                                                     'collector ids.'},
                    'status': {'type': 'string',
                               'enum': ['completed',
                                        'partial',
                                        'overquota',
                                        'disqualified']},
                    'start_created_at': {'type': 'string',
                                         'description': 'ISO 8601. Response started after '
                                                        'this.'},
                    'end_created_at': {'type': 'string',
                                       'description': 'ISO 8601. Response started before '
                                                      'this.'},
                    'start_modified_at': {'type': 'string',
                                          'description': 'ISO 8601. Response last modified '
                                                         'after this.'},
                    'end_modified_at': {'type': 'string',
                                        'description': 'ISO 8601. Response last modified '
                                                       'before this.'},
                    'email': {'type': 'string',
                              'description': "Filter to one respondent's email (email "
                                             'collectors only).'},
                    'first_name': {'type': 'string',
                                   'description': 'Filter by respondent first name (email '
                                                  'collectors only).'},
                    'last_name': {'type': 'string',
                                  'description': 'Filter by respondent last name (email '
                                                 'collectors only).'},
                    'ip': {'type': 'string',
                           'description': 'Filter to responses from this IP address.'},
                    'custom': {'type': 'string',
                               'description': "Filter by SurveyMonkey's custom_value field "
                                              'on the response.'},
                    'total_time_min': {'type': 'integer',
                                       'description': 'Only responses that took at least '
                                                      'this long.'},
                    'total_time_max': {'type': 'integer',
                                       'description': 'Only responses that took at most '
                                                      'this long.'},
                    'total_time_units': {'type': 'string',
                                         'enum': ['second', 'minute', 'hour'],
                                         'description': 'Unit for '
                                                        'total_time_min/total_time_max. '
                                                        'Default seconds.'}},
     'required': ['survey_id']})
async def surveymonkey_get_response_summary(credentials: Dict[str, str],
    survey_id: str,
    collector_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    start_created_at: Optional[str] = None,
    end_created_at: Optional[str] = None,
    start_modified_at: Optional[str] = None,
    end_modified_at: Optional[str] = None,
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    ip: Optional[str] = None,
    custom: Optional[str] = None,
    total_time_min: Optional[int] = None,
    total_time_max: Optional[int] = None,
    total_time_units: Optional[str] = None,
) -> Dict[str, Any]:
    params = response_filters(
        collector_ids,
        status,
        start_created_at,
        end_created_at,
        start_modified_at,
        end_modified_at,
        email,
        first_name,
        last_name,
        ip,
        custom,
        total_time_min,
        total_time_max,
        total_time_units,
    )
    data = await request(credentials, "GET", f"/surveys/{survey_id}/rollups", params=params)
    return {"success": True, "questions": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/responses/bulk
@server.tool(
    name='surveymonkey_get_responses_bulk',
    description="Fetch full responses (every question's answers) for a survey in one call. SurveyMonkey caps per_page at 100 for this endpoint - page through with increasing `page` for surveys with more responses than that. Set simple=true to include question/answer TEXT alongside ids. Supports the same filters as surveymonkey_list_responses.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'collector_ids': {'type': 'array',
                                      'items': {'type': 'string'},
                                      'description': 'Only include responses from these '
                                                     'collector ids.'},
                    'status': {'type': 'string',
                               'enum': ['completed',
                                        'partial',
                                        'overquota',
                                        'disqualified']},
                    'start_created_at': {'type': 'string',
                                         'description': 'ISO 8601. Response started after '
                                                        'this.'},
                    'end_created_at': {'type': 'string',
                                       'description': 'ISO 8601. Response started before '
                                                      'this.'},
                    'start_modified_at': {'type': 'string',
                                          'description': 'ISO 8601. Response last modified '
                                                         'after this.'},
                    'end_modified_at': {'type': 'string',
                                        'description': 'ISO 8601. Response last modified '
                                                       'before this.'},
                    'email': {'type': 'string',
                              'description': "Filter to one respondent's email (email "
                                             'collectors only).'},
                    'first_name': {'type': 'string',
                                   'description': 'Filter by respondent first name (email '
                                                  'collectors only).'},
                    'last_name': {'type': 'string',
                                  'description': 'Filter by respondent last name (email '
                                                 'collectors only).'},
                    'ip': {'type': 'string',
                           'description': 'Filter to responses from this IP address.'},
                    'custom': {'type': 'string',
                               'description': "Filter by SurveyMonkey's custom_value field "
                                              'on the response.'},
                    'total_time_min': {'type': 'integer',
                                       'description': 'Only responses that took at least '
                                                      'this long.'},
                    'total_time_max': {'type': 'integer',
                                       'description': 'Only responses that took at most '
                                                      'this long.'},
                    'total_time_units': {'type': 'string',
                                         'enum': ['second', 'minute', 'hour'],
                                         'description': 'Unit for '
                                                        'total_time_min/total_time_max. '
                                                        'Default seconds.'},
                    'sort_by': {'type': 'string', 'enum': ['date_modified']},
                    'sort_order': {'type': 'string', 'enum': ['ASC', 'DESC']},
                    'page_ids': {'type': 'array',
                                 'items': {'type': 'string'},
                                 'description': 'Only include answers from these survey '
                                                'pages. Default: all pages.'},
                    'question_ids': {'type': 'array',
                                     'items': {'type': 'string'},
                                     'description': 'Only include answers to these '
                                                    'questions. Default: all questions.'},
                    'simple': {'type': 'boolean',
                               'default': True,
                               'description': 'Include question/answer text, not just '
                                              'ids.'},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer',
                                 'default': 50,
                                 'description': 'Max 100.'}},
     'required': ['survey_id']})
async def surveymonkey_get_responses_bulk(credentials: Dict[str, str],
    survey_id: str,
    collector_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    start_created_at: Optional[str] = None,
    end_created_at: Optional[str] = None,
    start_modified_at: Optional[str] = None,
    end_modified_at: Optional[str] = None,
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    ip: Optional[str] = None,
    custom: Optional[str] = None,
    total_time_min: Optional[int] = None,
    total_time_max: Optional[int] = None,
    total_time_units: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page_ids: Optional[List[str]] = None,
    question_ids: Optional[List[str]] = None,
    simple: bool = True,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    if per_page > 100:
        raise ValueError("per_page must be 100 or fewer for the bulk responses endpoint")
    params = {
        **_list_filters(
            collector_ids,
            status,
            start_created_at,
            end_created_at,
            start_modified_at,
            end_modified_at,
            email,
            first_name,
            last_name,
            ip,
            custom,
            total_time_min,
            total_time_max,
            total_time_units,
            sort_by,
            sort_order,
            page_ids,
            question_ids,
        ),
        "simple": simple,
        "page": page,
        "per_page": per_page,
    }
    data = await request(
        credentials, "GET", f"/surveys/{survey_id}/responses/bulk", params=params
    )
    return {
        "success": True,
        "total": data.get("total"),
        "page": data.get("page"),
        "responses": data.get("data", []),
    }



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/details
@server.tool(
    name='surveymonkey_get_survey_details',
    description="Get a survey's full structure: title, response_count, and every page and question with its answer choice ids. Call this before reading responses so question/choice ids returned by response tools can be matched to their text.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string',
                                  'description': 'Survey id from '
                                                 'surveymonkey_list_surveys.'}},
     'required': ['survey_id']})
async def surveymonkey_get_survey_details(credentials: Dict[str, str], survey_id: str
) -> Dict[str, Any]:
    data = await request(credentials, "GET", f"/surveys/{survey_id}/details")
    return {"success": True, "survey": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/collectors
@server.tool(
    name='surveymonkey_list_collectors',
    description="List the distribution collectors for a survey (weblink, email, sms, popup), each with its status, url and response_count.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'name': {'type': 'string', 'description': 'Filter by collector name.'},
                    'start_date': {'type': 'string',
                                   'description': 'ISO 8601. Only collectors created after '
                                                  'this.'},
                    'end_date': {'type': 'string',
                                 'description': 'ISO 8601. Only collectors created before '
                                                'this.'},
                    'sort_by': {'type': 'string',
                                'enum': ['id', 'date_modified', 'type', 'status', 'name']},
                    'sort_order': {'type': 'string', 'enum': ['ASC', 'DESC']},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}},
     'required': ['survey_id']})
async def surveymonkey_list_collectors(credentials: Dict[str, str],
    survey_id: str,
    name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    if sort_by and sort_by not in COLLECTOR_SORT_FIELDS:
        raise ValueError(f"sort_by must be one of {COLLECTOR_SORT_FIELDS}")
    if sort_order and sort_order not in SORT_ORDERS:
        raise ValueError(f"sort_order must be one of {SORT_ORDERS}")
    params = compact(
        {
            "name": name,
            "start_date": start_date,
            "end_date": end_date,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "include": _COLLECTOR_DEFAULT_INCLUDE,
            "page": page,
            "per_page": per_page,
        }
    )
    data = await request(credentials, "GET", f"/surveys/{survey_id}/collectors", params=params)
    return {
        "success": True,
        "total": data.get("total"),
        "page": data.get("page"),
        "collectors": data.get("data", []),
    }



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /contact_fields
@server.tool(
    name='surveymonkey_list_contact_fields',
    description="List this account's custom contact field labels (e.g. field id '1' -> label 'Company'). Call this before reading/writing `custom_fields` on a contact - the numbered keys ('1', '2', ...) are meaningless without their labels, which are account-specific and set up in SurveyMonkey's contact settings.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}}})
async def surveymonkey_list_contact_fields(credentials: Dict[str, str], page: int = 1, per_page: int = 50
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "GET",
        "/contact_fields",
        params=compact({"page": page, "per_page": per_page}),
    )
    return {"success": True, "fields": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /contact_lists
@server.tool(
    name='surveymonkey_list_contact_lists',
    description="List contact lists in the account.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}}})
async def surveymonkey_list_contact_lists(credentials: Dict[str, str], page: int = 1, per_page: int = 50
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "GET",
        "/contact_lists",
        params=compact({"page": page, "per_page": per_page}),
    )
    return {"success": True, "total": data.get("total"), "contact_lists": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /contact_lists/{contact_list_id}/contacts
@server.tool(
    name='surveymonkey_list_contacts',
    description="List contacts in a contact list. search+search_by find contacts by a field value; sort_by+sort_order order the results. Both accept a custom field slot ('1'..'6') instead of a built-in field - see surveymonkey_list_contact_fields for what those slots mean in this account.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'contact_list_id': {'type': 'string'},
                    'status': {'type': 'string', 'enum': ['active', 'optout', 'bounced']},
                    'search': {'type': 'string'},
                    'search_by': {'type': 'string',
                                  'enum': ['email',
                                           'first_name',
                                           'last_name',
                                           '1',
                                           '2',
                                           '3',
                                           '4',
                                           '5',
                                           '6']},
                    'sort_by': {'type': 'string',
                                'enum': ['email',
                                         'first_name',
                                         'last_name',
                                         '1',
                                         '2',
                                         '3',
                                         '4',
                                         '5',
                                         '6']},
                    'sort_order': {'type': 'string', 'enum': ['ASC', 'DESC']},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}},
     'required': ['contact_list_id']})
async def surveymonkey_list_contacts(credentials: Dict[str, str],
    contact_list_id: str,
    status: Optional[str] = None,
    search: Optional[str] = None,
    search_by: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    if status and status not in _CONTACT_STATUSES:
        raise ValueError(f"status must be one of {_CONTACT_STATUSES}")
    if search_by and search_by not in _CONTACT_SORT_SEARCH_FIELDS:
        raise ValueError(f"search_by must be one of {_CONTACT_SORT_SEARCH_FIELDS}")
    if sort_by and sort_by not in _CONTACT_SORT_SEARCH_FIELDS:
        raise ValueError(f"sort_by must be one of {_CONTACT_SORT_SEARCH_FIELDS}")
    if sort_order and sort_order not in _SORT_ORDERS:
        raise ValueError(f"sort_order must be one of {_SORT_ORDERS}")
    params = compact(
        {
            "status": status,
            "search": search,
            "search_by": search_by,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "page": page,
            "per_page": per_page,
        }
    )
    data = await request(
        credentials, "GET", f"/contact_lists/{contact_list_id}/contacts", params=params
    )
    return {"success": True, "total": data.get("total"), "contacts": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /survey_folders
@server.tool(
    name='surveymonkey_list_folders',
    description="List survey folders in the account.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}}})
async def surveymonkey_list_folders(credentials: Dict[str, str], page: int = 1, per_page: int = 50
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "GET",
        "/survey_folders",
        params=compact({"page": page, "per_page": per_page}),
    )
    return {"success": True, "total": data.get("total"), "folders": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/pages
@server.tool(
    name='surveymonkey_list_pages',
    description="List the pages on a survey, with question_count per page.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}},
     'required': ['survey_id']})
async def surveymonkey_list_pages(credentials: Dict[str, str], survey_id: str, page: int = 1, per_page: int = 50
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "GET",
        f"/surveys/{survey_id}/pages",
        params=compact({"page": page, "per_page": per_page}),
    )
    return {"success": True, "total": data.get("total"), "pages": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/pages/{page_id}/questions
@server.tool(
    name='surveymonkey_list_questions',
    description="List the questions on a survey page (heading, id, position - not full detail).",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'page_id': {'type': 'string'},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}},
     'required': ['survey_id', 'page_id']})
async def surveymonkey_list_questions(credentials: Dict[str, str],
    survey_id: str,
    page_id: str,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "GET",
        f"/surveys/{survey_id}/pages/{page_id}/questions",
        params=compact({"page": page, "per_page": per_page}),
    )
    return {"success": True, "total": data.get("total"), "questions": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /collectors/{collector_id}/recipients
@server.tool(
    name='surveymonkey_list_recipients',
    description="List individual recipients of a collector's invites, with their response/mail status - who has completed, partially completed, or not responded, and whether their invite was sent/bounced. Requests survey_response_status and mail_status by default; SurveyMonkey only returns fields you explicitly ask for via `include`.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'},
                    'include': {'type': 'array',
                                'items': {'type': 'string',
                                          'enum': ['survey_response_status',
                                                   'mail_status',
                                                   'custom_fields',
                                                   'remove_link',
                                                   'extra_fields',
                                                   'survey_link']},
                                'description': 'Fields to include per recipient. Default: '
                                               'survey_response_status, mail_status. '
                                               "Options: ('survey_response_status', "
                                               "'mail_status', 'custom_fields', "
                                               "'remove_link', 'extra_fields', "
                                               "'survey_link')."},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}},
     'required': ['collector_id']})
async def surveymonkey_list_recipients(credentials: Dict[str, str],
    collector_id: str,
    include=None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    fields = include or ["survey_response_status", "mail_status"]
    bad = [f for f in fields if f not in _INCLUDE_FIELDS]
    if bad:
        raise ValueError(
            f"include contains unknown field(s) {bad}; must be from {_INCLUDE_FIELDS}"
        )
    params = compact({"include": ",".join(fields), "page": page, "per_page": per_page})
    data = await request(
        credentials, "GET", f"/collectors/{collector_id}/recipients", params=params
    )
    return {"success": True, "total": data.get("total"), "recipients": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys/{survey_id}/responses
@server.tool(
    name='surveymonkey_list_responses',
    description="List response ids for a survey (lightweight - id and href only, no answers). Use to count/filter responses before pulling full detail with surveymonkey_get_responses_bulk or surveymonkey_get_response_detail. Supports the full filter set: status, date range (created or modified), respondent identity (email collectors only), time spent, and sorting.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'collector_ids': {'type': 'array',
                                      'items': {'type': 'string'},
                                      'description': 'Only include responses from these '
                                                     'collector ids.'},
                    'status': {'type': 'string',
                               'enum': ['completed',
                                        'partial',
                                        'overquota',
                                        'disqualified']},
                    'start_created_at': {'type': 'string',
                                         'description': 'ISO 8601. Response started after '
                                                        'this.'},
                    'end_created_at': {'type': 'string',
                                       'description': 'ISO 8601. Response started before '
                                                      'this.'},
                    'start_modified_at': {'type': 'string',
                                          'description': 'ISO 8601. Response last modified '
                                                         'after this.'},
                    'end_modified_at': {'type': 'string',
                                        'description': 'ISO 8601. Response last modified '
                                                       'before this.'},
                    'email': {'type': 'string',
                              'description': "Filter to one respondent's email (email "
                                             'collectors only).'},
                    'first_name': {'type': 'string',
                                   'description': 'Filter by respondent first name (email '
                                                  'collectors only).'},
                    'last_name': {'type': 'string',
                                  'description': 'Filter by respondent last name (email '
                                                 'collectors only).'},
                    'ip': {'type': 'string',
                           'description': 'Filter to responses from this IP address.'},
                    'custom': {'type': 'string',
                               'description': "Filter by SurveyMonkey's custom_value field "
                                              'on the response.'},
                    'total_time_min': {'type': 'integer',
                                       'description': 'Only responses that took at least '
                                                      'this long.'},
                    'total_time_max': {'type': 'integer',
                                       'description': 'Only responses that took at most '
                                                      'this long.'},
                    'total_time_units': {'type': 'string',
                                         'enum': ['second', 'minute', 'hour'],
                                         'description': 'Unit for '
                                                        'total_time_min/total_time_max. '
                                                        'Default seconds.'},
                    'sort_by': {'type': 'string', 'enum': ['date_modified']},
                    'sort_order': {'type': 'string', 'enum': ['ASC', 'DESC']},
                    'page_ids': {'type': 'array',
                                 'items': {'type': 'string'},
                                 'description': 'Only include answers from these survey '
                                                'pages. Default: all pages.'},
                    'question_ids': {'type': 'array',
                                     'items': {'type': 'string'},
                                     'description': 'Only include answers to these '
                                                    'questions. Default: all questions.'},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}},
     'required': ['survey_id']})
async def surveymonkey_list_responses(credentials: Dict[str, str],
    survey_id: str,
    collector_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    start_created_at: Optional[str] = None,
    end_created_at: Optional[str] = None,
    start_modified_at: Optional[str] = None,
    end_modified_at: Optional[str] = None,
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    ip: Optional[str] = None,
    custom: Optional[str] = None,
    total_time_min: Optional[int] = None,
    total_time_max: Optional[int] = None,
    total_time_units: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page_ids: Optional[List[str]] = None,
    question_ids: Optional[List[str]] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    params = {
        **_list_filters(
            collector_ids,
            status,
            start_created_at,
            end_created_at,
            start_modified_at,
            end_modified_at,
            email,
            first_name,
            last_name,
            ip,
            custom,
            total_time_min,
            total_time_max,
            total_time_units,
            sort_by,
            sort_order,
            page_ids,
            question_ids,
        ),
        "page": page,
        "per_page": per_page,
    }
    data = await request(credentials, "GET", f"/surveys/{survey_id}/responses", params=params)
    return {
        "success": True,
        "total": data.get("total"),
        "page": data.get("page"),
        "responses": data.get("data", []),
    }



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /survey_templates
@server.tool(
    name='surveymonkey_list_survey_templates',
    description="List SurveyMonkey's built-in survey templates. Template ids can be passed as from_template_id to surveymonkey_create_survey to bootstrap a pre-built survey instead of authoring pages/questions from scratch.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'category': {'type': 'string',
                                 'description': 'Filter by category id (see '
                                                "survey_categories in SurveyMonkey's "
                                                'docs).'},
                    'language': {'type': 'string',
                                 'description': "ISO 639-1 code, default 'en'."},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}}})
async def surveymonkey_list_survey_templates(credentials: Dict[str, str],
    category: Optional[str] = None,
    language: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    params = compact(
        {"category": category, "language": language, "page": page, "per_page": per_page}
    )
    data = await request(credentials, "GET", "/survey_templates", params=params)
    return {"success": True, "total": data.get("total"), "templates": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /surveys
@server.tool(
    name='surveymonkey_list_surveys',
    description="List surveys in the connected SurveyMonkey account. Returns id, title, response_count, question_count and date_modified per survey so an agent can find the right survey by name before pulling its responses.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'title': {'type': 'string',
                              'description': 'Filter by (partial) survey title.'},
                    'folder_id': {'type': 'string',
                                  'description': 'Only return surveys in this folder.'},
                    'sort_by': {'type': 'string',
                                'enum': ['title', 'date_modified', 'num_responses']},
                    'sort_order': {'type': 'string', 'enum': ['ASC', 'DESC']},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer',
                                 'default': 50,
                                 'description': 'Max 1000.'}}})
async def surveymonkey_list_surveys(credentials: Dict[str, str],
    title: Optional[str] = None,
    folder_id: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    params = compact(
        {
            "title": title,
            "folder_id": folder_id,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "include": _SURVEY_DEFAULT_INCLUDE,
            "page": page,
            "per_page": per_page,
        }
    )
    data = await request(credentials, "GET", "/surveys", params=params)
    return {
        "success": True,
        "total": data.get("total"),
        "page": data.get("page"),
        "per_page": data.get("per_page"),
        "surveys": data.get("data", []),
    }



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /webhooks
@server.tool(
    name='surveymonkey_list_webhooks',
    description="List active webhook subscriptions in this SurveyMonkey account.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}}})
async def surveymonkey_list_webhooks(credentials: Dict[str, str], page: int = 1, per_page: int = 50
) -> Dict[str, Any]:
    data = await request(
        credentials, "GET", "/webhooks", params=compact({"page": page, "per_page": per_page})
    )
    return {"success": True, "total": data.get("total"), "webhooks": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /question_bank/questions
@server.tool(
    name='surveymonkey_search_question_bank',
    description="Search SurveyMonkey's curated, methodology-validated question bank. Returns question_id values that can be referenced when authoring a question, and each result's text/modifiers show the correctly-formatted wording for that question type.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'search': {'type': 'string', 'description': 'Free-text search query.'},
                    'locale': {'type': 'string',
                               'description': 'e.g. en_US. Default en_US.'},
                    'page': {'type': 'integer', 'default': 1},
                    'per_page': {'type': 'integer', 'default': 50}}})
async def surveymonkey_search_question_bank(credentials: Dict[str, str],
    search: Optional[str] = None,
    locale: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
) -> Dict[str, Any]:
    params = compact({"search": search, "locale": locale, "page": page, "per_page": per_page})
    data = await request(credentials, "GET", "/question_bank/questions", params=params)
    return {"success": True, "total": data.get("total"), "questions": data.get("data", [])}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /collectors/{collector_id}/messages/{message_id}/send
@server.tool(
    name='surveymonkey_send_message',
    description="Send (or schedule) a message to all its recipients. The message must have status 'not_sent' and already have recipients added.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'},
                    'message_id': {'type': 'string'},
                    'scheduled_date': {'type': 'string',
                                       'description': 'ISO 8601. Omit to send '
                                                      'immediately.'}},
     'required': ['collector_id', 'message_id']})
async def surveymonkey_send_message(credentials: Dict[str, str],
    collector_id: str,
    message_id: str,
    scheduled_date: Optional[str] = None,
) -> Dict[str, Any]:
    data = await request(
        credentials,
        "POST",
        f"/collectors/{collector_id}/messages/{message_id}/send",
        json_body=compact({"scheduled_date": scheduled_date}),
    )
    return {"success": True, "result": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /collectors/{id}; status='closed'
# stops a live collector accepting responses.
# Evidence: PATCH /collectors/{collector_id}
@server.tool(
    name='surveymonkey_update_collector',
    description="Update a collector - most commonly to close it (status='closed') or reopen it (status='open'), or change any of its behavior settings (same fields as surveymonkey_create_collector, minus type which can't be changed after creation).",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'collector_id': {'type': 'string'},
                    'status': {'type': 'string', 'enum': ['open', 'closed']},
                    'name': {'type': 'string', 'description': 'Collector display name.'},
                    'thank_you_message': {'type': 'string'},
                    'disqualification_message': {'type': 'string'},
                    'disqualification_url': {'type': 'string',
                                             'description': 'Redirect here on '
                                                            'disqualification.'},
                    'close_date': {'type': 'string',
                                   'description': 'ISO 8601. Collector auto-closes at this '
                                                  'time.'},
                    'closed_page_message': {'type': 'string',
                                            'description': 'Shown to respondents once '
                                                           'closed.'},
                    'redirect_url': {'type': 'string',
                                     'description': 'Redirect respondents here on '
                                                    'completion.'},
                    'redirect_type': {'type': 'string',
                                      'enum': ['url', 'close', 'loop'],
                                      'description': "'url' uses redirect_url, 'close' "
                                                     "closes the tab, 'loop' restarts the "
                                                     'survey (weblink + '
                                                     'allow_multiple_responses only).'},
                    'display_survey_results': {'type': 'boolean',
                                               'description': 'Show respondents instant '
                                                              'results after they complete '
                                                              'the survey.'},
                    'edit_response_type': {'type': 'string',
                                           'enum': ['until_complete', 'never', 'always'],
                                           'description': 'When respondents can edit their '
                                                          'submitted response.'},
                    'anonymous_type': {'type': 'string',
                                       'enum': ['not_anonymous',
                                                'partially_anonymous',
                                                'fully_anonymous'],
                                       'description': 'Turns off IP tracking; '
                                                      "'fully_anonymous' on an email "
                                                      'collector also strips respondent '
                                                      'identity.'},
                    'allow_multiple_responses': {'type': 'boolean',
                                                 'description': 'Let the same '
                                                                'browser/device respond '
                                                                'more than once. Not '
                                                                'available for email '
                                                                'collectors.'},
                    'password': {'type': 'string',
                                 'description': 'Require a password to access the survey.'},
                    'sender_email': {'type': 'string',
                                     'description': 'Sender address for email collectors.'},
                    'response_limit': {'type': 'integer',
                                       'description': 'Auto-close after this many '
                                                      'responses.'}},
     'required': ['collector_id']})
async def surveymonkey_update_collector(credentials: Dict[str, str],
    collector_id: str,
    status: Optional[str] = None,
    name: Optional[str] = None,
    thank_you_message: Optional[str] = None,
    disqualification_message: Optional[str] = None,
    disqualification_url: Optional[str] = None,
    close_date: Optional[str] = None,
    closed_page_message: Optional[str] = None,
    redirect_url: Optional[str] = None,
    redirect_type: Optional[str] = None,
    display_survey_results: Optional[bool] = None,
    edit_response_type: Optional[str] = None,
    anonymous_type: Optional[str] = None,
    allow_multiple_responses: Optional[bool] = None,
    password: Optional[str] = None,
    sender_email: Optional[str] = None,
    response_limit: Optional[int] = None,
) -> Dict[str, Any]:
    if status and status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}")
    body = collector_settings_body(
        name=name,
        thank_you_message=thank_you_message,
        disqualification_message=disqualification_message,
        disqualification_url=disqualification_url,
        close_date=close_date,
        closed_page_message=closed_page_message,
        redirect_url=redirect_url,
        redirect_type=redirect_type,
        display_survey_results=display_survey_results,
        edit_response_type=edit_response_type,
        anonymous_type=anonymous_type,
        allow_multiple_responses=allow_multiple_responses,
        password=password,
        sender_email=sender_email,
        response_limit=response_limit,
    )
    if status:
        body["status"] = status
    if not body:
        raise ValueError("at least one field to update is required")
    data = await request(credentials, "PATCH", f"/collectors/{collector_id}", json_body=body)
    return {"success": True, "collector": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /surveys/{sid}/pages/{pid}: overwrites
# title and description, and reorders via position.
# Evidence: PATCH /surveys/{survey_id}/pages/{page_id}
@server.tool(
    name='surveymonkey_update_page',
    description="Update a page's title, description, or position within the survey.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'page_id': {'type': 'string'},
                    'title': {'type': 'string'},
                    'description': {'type': 'string'},
                    'position': {'type': 'integer'}},
     'required': ['survey_id', 'page_id']})
async def surveymonkey_update_page(credentials: Dict[str, str],
    survey_id: str,
    page_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    position: Optional[int] = None,
) -> Dict[str, Any]:
    body = compact({"title": title, "description": description, "position": position})
    if not body:
        raise ValueError("at least one field to update is required")
    data = await request(
        credentials, "PATCH", f"/surveys/{survey_id}/pages/{page_id}", json_body=body
    )
    return {"success": True, "page": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH .../questions/{qid}: answers.choices
# is rebuilt from the passed list, discarding the existing options.
# Evidence: PATCH, path built at runtime
@server.tool(
    name='surveymonkey_update_question',
    description="Update a question - most commonly its heading, choices, or whether it's required. For a deeper change (e.g. switching family/subtype or restructuring answers), pass a raw `question` object instead (same shape as surveymonkey_add_question's type='advanced').",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'page_id': {'type': 'string'},
                    'question_id': {'type': 'string'},
                    'heading': {'type': 'string'},
                    'choices': {'type': 'array',
                                'items': {'type': 'string'},
                                'description': 'Replaces the answer choices for a '
                                               'single/multiple choice question.'},
                    'required': {'type': 'boolean'},
                    'question': {'type': 'object',
                                 'description': 'Raw partial SurveyMonkey question body, '
                                                "for changes the other fields don't "
                                                'cover.'}},
     'required': ['survey_id', 'page_id', 'question_id']})
async def surveymonkey_update_question(credentials: Dict[str, str],
    survey_id: str,
    page_id: str,
    question_id: str,
    heading: Optional[str] = None,
    choices: Optional[List[str]] = None,
    required: Optional[bool] = None,
    question: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if question is not None:
        body = question
    else:
        body = {}
        if heading is not None:
            body["headings"] = [{"heading": heading}]
        if choices is not None:
            if not choices:
                raise ValueError("choices must have at least one option")
            body["answers"] = {"choices": [{"text": c} for c in choices]}
        if required is not None:
            # Same non-matrix `required` shape fix as surveymonkey_add_question -
            # SurveyMonkey needs type/amount even for a plain requirement.
            body["required"] = (
                {
                    "text": f"Please answer: {heading or 'this question'}",
                    "type": "all",
                    "amount": "0",
                }
                if required
                else None
            )
    if not body:
        raise ValueError("at least one field to update is required")
    data = await request(
        credentials,
        "PATCH",
        f"/surveys/{survey_id}/pages/{page_id}/questions/{question_id}",
        json_body=body,
    )
    return {"success": True, "question": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /surveys/{id}: overwrites title,
# nickname and language, and moves the survey between folders.
# Evidence: PATCH /surveys/{survey_id}
@server.tool(
    name='surveymonkey_update_survey',
    description="Update a survey's title, nickname, language, or folder.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'survey_id': {'type': 'string'},
                    'title': {'type': 'string'},
                    'nickname': {'type': 'string'},
                    'language': {'type': 'string'},
                    'folder_id': {'type': 'string'}},
     'required': ['survey_id']})
async def surveymonkey_update_survey(credentials: Dict[str, str],
    survey_id: str,
    title: Optional[str] = None,
    nickname: Optional[str] = None,
    language: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    body = compact(
        {"title": title, "nickname": nickname, "language": language, "folder_id": folder_id}
    )
    if not body:
        raise ValueError("at least one field to update is required")
    data = await request(credentials, "PATCH", f"/surveys/{survey_id}", json_body=body)
    return {"success": True, "survey": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /webhooks/{id}: overwrites event_type
# and subscription_url, silently redirecting an existing event stream.
# Evidence: PATCH /webhooks/{webhook_id}
@server.tool(
    name='surveymonkey_update_webhook',
    description="Update a webhook's name, event_type, object_type/object_ids, or subscription_url.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'webhook_id': {'type': 'string'},
                    'name': {'type': 'string'},
                    'event_type': {'type': 'string',
                                   'enum': ['response_completed',
                                            'response_updated',
                                            'response_disqualified',
                                            'response_created',
                                            'response_deleted',
                                            'response_overquota',
                                            'collector_created',
                                            'collector_updated',
                                            'collector_deleted',
                                            'survey_created',
                                            'survey_updated',
                                            'survey_deleted']},
                    'object_type': {'type': 'string', 'enum': ['survey', 'collector']},
                    'object_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'subscription_url': {'type': 'string'}},
     'required': ['webhook_id']})
async def surveymonkey_update_webhook(credentials: Dict[str, str],
    webhook_id: str,
    name: Optional[str] = None,
    event_type: Optional[str] = None,
    object_type: Optional[str] = None,
    object_ids: Optional[List[str]] = None,
    subscription_url: Optional[str] = None,
) -> Dict[str, Any]:
    if event_type and event_type not in _EVENT_TYPES:
        raise ValueError(f"event_type must be one of {_EVENT_TYPES}")
    if object_type and object_type not in _OBJECT_TYPES:
        raise ValueError(f"object_type must be one of {_OBJECT_TYPES}")
    if (object_type is None) != (object_ids is None):
        raise ValueError("object_type and object_ids must be provided together")
    body = compact(
        {
            "name": name,
            "event_type": event_type,
            "object_type": object_type,
            "object_ids": object_ids,
            "subscription_url": subscription_url,
        }
    )
    if not body:
        raise ValueError("at least one field to update is required")
    data = await request(credentials, "PATCH", f"/webhooks/{webhook_id}", json_body=body)
    return {"success": True, "webhook": data}



# credential_headers: ['X-Surveymonkey-Access-Token']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to True - GET /users/me.
@server.tool(
    name='surveymonkey_whoami',
    description="Get the connected SurveyMonkey account's identity, plan, and OAuth scopes. Returns account_type (the plan) plus scopes.available (what the account CAN grant) and scopes.granted (what THIS token actually has). Call this first when a tool fails with a Permission Error (code 1014/1015) - it tells you directly whether a scope is missing from the app config or genuinely requires a paid plan, instead of guessing tool by tool.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Surveymonkey-Access-Token'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
async def surveymonkey_whoami(credentials: Dict[str, str]) -> Dict[str, Any]:
    data = await request(credentials, "GET", "/users/me")
    return {
        "success": True,
        "id": data.get("id"),
        "username": data.get("username"),
        "email": data.get("email"),
        "account_type": data.get("account_type"),
        "scopes_granted": (data.get("scopes") or {}).get("granted", []),
        "scopes_available": (data.get("scopes") or {}).get("available", []),
    }
