"""Input validation and Graph message-body construction for send_email.

Split out of send_email.py so the tool file stays under the 200-line limit:
these are pure functions with no I/O, covering scalar validation and building
the Graph message payload (including recipient parsing).
"""

from typing import Any, Dict, Optional, Tuple

from ._constants import (
    MAX_BODY_SIZE,
    MAX_SUBJECT_LEN,
    VALID_IMPORTANCE_LIST,
    parse_recipients,
)


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
