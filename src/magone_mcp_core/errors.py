"""Why a call failed, in a form a host can act on.

A refusal's `error` is written for a model. A host that calls a tool on its own
(a workflow paging through a history, a scheduler retrying overnight) needs
something it can branch on without reading prose: whether to wait and try the
same call again, and for how long, or to stop. So a failure may also carry:

- `error_code`: one of the codes below;
- `retry_after_seconds`: on `throttled` or `unavailable`, how long the vendor
  asked to wait, when it said.

Only `throttled`, `unavailable`, `network_error` and `timeout` are worth
retrying (`RETRYABLE`); every other code means the same call will fail again.

A connector helper raises `UpstreamError` where it used to raise a bare
`ValueError` for a vendor's error response. It is a `ValueError`, so every
existing `except ValueError` still catches it, and its message is unchanged.
`ConnectorServer` reports one that escapes a tool as a refusal carrying the
code, and a transport failure (`httpx.TransportError`) as `network_error` or
`timeout`, instead of the bare "Error executing tool" the SDK would send.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

THROTTLED = "throttled"
UNAVAILABLE = "unavailable"
NETWORK_ERROR = "network_error"
TIMEOUT = "timeout"
AUTH_EXPIRED = "auth_expired"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
INVALID_ARGUMENT = "invalid_argument"
UPSTREAM_ERROR = "upstream_error"

ERROR_CODES = (
    THROTTLED,
    UNAVAILABLE,
    NETWORK_ERROR,
    TIMEOUT,
    AUTH_EXPIRED,
    FORBIDDEN,
    NOT_FOUND,
    INVALID_ARGUMENT,
    UPSTREAM_ERROR,
)
RETRYABLE = frozenset({THROTTLED, UNAVAILABLE, NETWORK_ERROR, TIMEOUT})

# Properties for a tool's output schema, so a declared result admits a refusal.
ERROR_FIELDS_SCHEMA: dict[str, Any] = {
    "error_code": {
        "type": "string",
        "description": (
            "On failure, why: " + ", ".join(ERROR_CODES) + ". Retry only "
            "throttled, unavailable, network_error and timeout."
        ),
    },
    "retry_after_seconds": {
        "type": "number",
        "description": "On throttled or unavailable: seconds the service asked to wait.",
    },
}

# A vendor asking for longer than this is treated as asking for this: a host
# that waited a day on one header would look hung.
MAX_RETRY_AFTER_SECONDS = 3600.0


def code_for_status(status: int) -> str:
    """The error code for a vendor's HTTP status."""
    if status == 429:
        return THROTTLED
    if status in (502, 503, 504):
        return UNAVAILABLE
    if status == 401:
        return AUTH_EXPIRED
    if status == 403:
        return FORBIDDEN
    if status in (404, 410):
        return NOT_FOUND
    if status in (400, 409, 422):
        return INVALID_ARGUMENT
    return UPSTREAM_ERROR


def retry_after_seconds(
    headers: Mapping[str, str] | None, *, now: datetime | None = None
) -> float | None:
    """A `Retry-After` header in seconds, from a number or an HTTP date; None when unreadable."""
    if not headers:
        return None
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if raw is None or not str(raw).strip():
        return None
    raw = str(raw).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if seconds != seconds:  # NaN
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


class UpstreamError(ValueError):
    """A vendor's error response, with the code a host acts on.

    A `ValueError`, so its message still reaches the model the way a helper's
    `ValueError` always has; `code` and `retry_after` ride beside it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = UPSTREAM_ERROR,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after

    @classmethod
    def from_status(
        cls,
        message: str,
        status: int,
        headers: Mapping[str, str] | None = None,
    ) -> UpstreamError:
        """The error for an HTTP status, reading `Retry-After` when it is worth waiting."""
        code = code_for_status(status)
        wait = retry_after_seconds(headers) if code in (THROTTLED, UNAVAILABLE) else None
        return cls(message, code=code, status=status, retry_after=wait)


def refusal(message: str, code: str, retry_after_seconds: float | None = None) -> dict[str, Any]:
    """A tool's failure result: `success: false`, the message, and its code."""
    out: dict[str, Any] = {"success": False, "error": message, "error_code": code}
    if retry_after_seconds is not None:
        out["retry_after_seconds"] = retry_after_seconds
    return out


def transport_refusal(exc: BaseException) -> dict[str, Any] | None:
    """The refusal for a request that never got an answer, or None for any other exception.

    The message is fixed rather than the exception's: a transport error can
    carry the URL it was sending to.
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return refusal("The service did not answer in time.", TIMEOUT)
    if isinstance(exc, httpx.TransportError):
        return refusal("Could not reach the service.", NETWORK_ERROR)
    return None


def error_fields(exc: BaseException) -> dict[str, Any]:
    """The code fields for a refusal a tool builds itself from a caught exception.

    For a tool that catches its helper's errors and returns
    `{"success": False, "error": ...}`: spread these in so the code survives.
    Empty for an exception that says nothing about why.
    """
    if isinstance(exc, UpstreamError):
        out: dict[str, Any] = {"error_code": exc.code}
        if exc.retry_after is not None:
            out["retry_after_seconds"] = exc.retry_after
        return out
    unreached = transport_refusal(exc)
    return {"error_code": unreached["error_code"]} if unreached else {}
