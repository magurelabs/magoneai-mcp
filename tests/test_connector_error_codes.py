"""The shared Google request helpers raise errors carrying the code a host acts on.

Google answers a rate limit with 429, or with 403 and a `rateLimitExceeded`
reason; both are `throttled`, with Google's `Retry-After` when it sent one.
A real 403 stays `forbidden`. The message for the model is unchanged.
"""

import importlib

import httpx
import pytest

from magone_mcp_core import UpstreamError

GOOGLE = ["google_drive", "google_gmail", "google_calendar"]


def _response(status: int, body: dict | None = None, headers: dict | None = None):
    return httpx.Response(status, json=body or {}, headers=headers or {})


def _rate_limited_403() -> httpx.Response:
    body = {"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}
    return _response(403, body, {"Retry-After": "20"})


@pytest.mark.parametrize("connector", GOOGLE)
@pytest.mark.parametrize(
    ("response", "code", "message", "wait"),
    [
        (_response(429), "throttled", "Rate limit exceeded. Try again later.", None),
        (_rate_limited_403(), "throttled", "Rate limit exceeded. Try again later.", 20.0),
        (_response(403), "forbidden", "Access denied. Check permissions and scopes.", None),
        (_response(401), "auth_expired", "Authentication failed. Please re-authenticate.", None),
        (_response(404), "not_found", "Resource not found.", None),
    ],
)
def test_google_statuses_carry_their_code(connector, response, code, message, wait):
    shared = importlib.import_module(f"connectors.{connector}._shared")

    error = shared._google_error(response, "detail")

    assert isinstance(error, UpstreamError) and isinstance(error, ValueError)
    assert (error.code, str(error), error.retry_after) == (code, message, wait)


@pytest.mark.parametrize("connector", GOOGLE)
def test_any_other_google_status_keeps_its_detail(connector):
    shared = importlib.import_module(f"connectors.{connector}._shared")

    error = shared._google_error(_response(500), "backend error")

    assert (error.code, str(error)) == ("upstream_error", "Google API error (500): backend error")


@pytest.mark.parametrize("connector", GOOGLE)
def test_a_rate_limited_403_keeps_its_real_status(connector):
    shared = importlib.import_module(f"connectors.{connector}._shared")

    assert shared._google_error(_rate_limited_403(), "detail").status == 403
