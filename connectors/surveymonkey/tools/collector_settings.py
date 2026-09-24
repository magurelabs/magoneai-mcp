"""Shared collector-settings schema/body-builder, used by both
surveymonkey_create_collector (collectors.py) and surveymonkey_update_collector
(collector_admin.py) - PATCH accepts every field POST does (per SurveyMonkey's
docs), so the settings fragment lives in exactly one place rather than being
duplicated across create and update.

Fields confirmed against SurveyMonkey/public_api_docs (``_collectors.md``,
2026-08) - this is the full set of behavior-affecting fields their own
collector settings screen exposes, not just a handful. Popup-only visual
styling (width/height/colors/custom buttons) is intentionally not included;
see the README.
"""

from typing import Any, Dict, Optional

from .client import compact

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
