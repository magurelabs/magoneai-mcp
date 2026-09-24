"""A vendor error from Kapso is reported as a failure, with its message.

The envelope put the vendor's JSON body in `error` as a dict. A result is a
refusal only when `error` is a non-empty string, so every Kapso 4xx and 5xx
reached the caller as a success. (v1 had the same shape; it is fixed here.)
"""

import importlib

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.kapso_whatsapp.server")

HEADERS = {"x-api-key": "k", "x-phone-number-id": "pn-1"}


def install(monkeypatch, status: int, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS))


async def test_a_vendor_error_is_a_failure_with_the_vendor_message(monkeypatch):
    install(
        monkeypatch,
        400,
        {"error": {"message": "Recipient phone number not in allowed list", "code": 131030}},
    )

    async with Client(module.server.mcp) as client:
        result = await client.call_tool("send_text_message", {"to": "+971500000000", "text": "hi"})

    assert result.is_error is True
    assert "Recipient phone number not in allowed list" in result.content[0].text
    assert "131030" in result.content[0].text


async def test_a_success_is_still_a_success(monkeypatch):
    install(monkeypatch, 200, {"messages": [{"id": "wamid.1"}]})

    async with Client(module.server.mcp) as client:
        result = await client.call_tool("send_text_message", {"to": "+971500000000", "text": "hi"})

    assert result.is_error is False
