"""Redaction on every log path, installed once per process.

`redact.py` had the patterns and nothing called them. Measured before this
existed: a vendor error logged `api_key=sk_live_...` verbatim through
structlog, and the SDK's own traceback for a failed tool printed
`Bearer sk_live_...` and an OAuth access token in full. Both are ordinary
code paths, not misuse, which is why this cannot be left to each connector.

Two paths, because a connector process logs through both:

- **structlog**, which connectors and the framework use. A processor redacts
  the event dict (secret-named keys, credential-shaped values) before the
  renderer sees it.
- **stdlib logging**, which the SDK, uvicorn and httpx use. A record factory
  scrubs every record at creation: the formatted message, and the exception
  traceback, which is where tokens actually leaked. Installing it on a
  handler instead would miss handlers that uvicorn creates after start-up.

The raw upstream body stays available for a local debugging session through
`MCP_LOG_RAW_UPSTREAM_ERRORS`, as before; nothing here changes that.
"""

import logging
import traceback
from typing import Any

import structlog

from .redact import REDACTED, is_secret_key, redact_mapping, scrub_text

_INSTALLED = False

# structlog keys that are part of the event's shape, not user data.
_STRUCTURAL_KEYS = frozenset({"event", "level", "logger", "timestamp", "exc_info"})


def redact_event(logger: Any, method_name: str, event_dict: dict) -> dict:
    """structlog processor: redact the event before it is rendered."""
    out: dict = {}
    for key, value in event_dict.items():
        if key in _STRUCTURAL_KEYS:
            out[key] = scrub_text(value) if isinstance(value, str) else value
        elif is_secret_key(key):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, str):
            out[key] = scrub_text(value)
        else:
            out[key] = value
    exc_info = out.pop("exc_info", None)
    if exc_info:
        # Render the traceback here, scrubbed, rather than let the renderer
        # print it verbatim.
        if exc_info is True:
            import sys

            exc_info = sys.exc_info()
        if isinstance(exc_info, BaseException):
            exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
        if isinstance(exc_info, tuple) and exc_info[0] is not None:
            out["exception"] = scrub_text("".join(traceback.format_exception(*exc_info)))
    return out


def _merged(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:  # noqa: BLE001 - a malformed record must still log
        return str(record.msg)


def _scrub_message(record: logging.LogRecord) -> None:
    """Scrub a record without changing its shape unless it has to.

    A formatter may read the arguments itself: uvicorn's access formatter
    unpacks them as (client, method, path, version, status), and emptying
    them lost every access line. So a clean record is left alone, a secret
    inside an argument is scrubbed in place, and only a secret spanning the
    message and an argument (``"Bearer %s"``) flattens the record.
    """
    message = _merged(record)
    if scrub_text(message) == message:
        return
    if isinstance(record.args, tuple) and record.args:
        record.args = tuple(scrub_text(a) if isinstance(a, str) else a for a in record.args)
        merged = _merged(record)
        if scrub_text(merged) == merged:
            return
    record.msg = scrub_text(message)
    record.args = ()


def _scrubbing_record_factory(previous: Any) -> Any:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        _scrub_message(record)
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = scrub_text(
                "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            )
            # The formatter uses `exc_text` when set; clearing `exc_info` stops a
            # handler that formats it itself from printing the raw traceback.
            record.exc_info = None
        return record

    return factory


def install_log_redaction() -> None:
    """Install both redaction paths, once per process."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    config = structlog.get_config()
    processors = list(config.get("processors") or [])
    if redact_event not in processors:
        # Before the renderer, which is always last.
        processors.insert(max(len(processors) - 1, 0), redact_event)
        structlog.configure(processors=processors)

    logging.setLogRecordFactory(_scrubbing_record_factory(logging.getLogRecordFactory()))
