"""Marking text that came from outside the trust boundary.

A connector returns a Jira description, an email body, a scraped page. That
text reaches a model that also holds write tools, and anyone who can leave a
comment can therefore attempt to give the model instructions.

This is a **convention, not a guarantee**, and the distinction matters enough
to state plainly: wrapping text in a labelled envelope does not stop a model
from following instructions inside it. What it does is give a client something
to key on, so a client that wants to treat third-party text as data rather than
instruction has a way to tell which part is which. Today there is no such
marker anywhere, so a client that wanted to could not.

Deliberately not claimed: that this mitigates prompt injection. It does not.
The read-only mode in `readonly.py` is the control that actually reduces blast
radius, by making a write impossible rather than unlikely.
"""

from typing import Any

OPEN = "<<<UNTRUSTED_CONTENT source={source}>>>"
CLOSE = "<<<END_UNTRUSTED_CONTENT>>>"

# Sequences a hostile document could include to close the envelope early and
# have the rest read as trusted. Neutralised rather than rejected, because a
# page that happens to contain the marker is not necessarily an attack and
# refusing to return it would be a denial of service.
_ESCAPES = (OPEN.format(source=""), CLOSE, "<<<UNTRUSTED_CONTENT", "<<<END_UNTRUSTED")


def _neutralise(text: str) -> str:
    out = text
    for marker in _ESCAPES:
        if marker:
            out = out.replace(marker, marker.replace("<<<", "<​<​<"))
    return out


def wrap(text: str, *, source: str) -> str:
    """Wrap third-party text in a labelled envelope.

    ``source`` names where the text came from, so a client can distinguish a
    Jira comment from a scraped page and apply its own policy.
    """
    if not text:
        return text
    return f"{OPEN.format(source=source)}\n{_neutralise(text)}\n{CLOSE}"


def mark_result(result: Any, *, source: str, fields: tuple = ()) -> Any:
    """Mark the third-party text fields of a tool result.

    Only the named fields are wrapped. A result is usually a mixture of our own
    metadata (ids, counts, timestamps) and the vendor's content, and wrapping
    the whole object would mark our own fields as untrusted while making the
    result harder to parse.
    """
    if not isinstance(result, dict) or not fields:
        return result
    out: dict[str, Any] = dict(result)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = wrap(value, source=source)
    return out
