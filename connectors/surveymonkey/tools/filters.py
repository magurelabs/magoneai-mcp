"""Response-filter schema/builder shared by responses.py and rollups.py.

SurveyMonkey's own docs split these into separate resources (``_responses.md``
vs ``_rollups.md``) but both accept the identical filter query params, so the
schema fragment and validation live here once instead of twice. This is the
FULL filter set both endpoints document - not just date/status/collector,
but also respondent identity (email/first_name/last_name/ip/custom - email
collector responses only) and time-on-survey filters.
"""

from typing import Any, Dict, List, Optional

from .client import compact

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


# Response-list-only additions (rollups doesn't support these): sorting and
# narrowing to specific pages/questions. Shared by surveymonkey_list_responses
# and surveymonkey_get_responses_bulk in responses.py.
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
