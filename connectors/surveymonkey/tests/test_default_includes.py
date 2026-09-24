"""Collectors and surveys each ask for their own default fields.

v1 had two modules, each with its own `_DEFAULT_INCLUDE`. Flattened into one,
the surveys value overwrote the collectors one, so list_collectors silently
stopped asking for type, status, date_created and url: a success, with less
in it, which nothing error-shaped can catch.
"""

import importlib

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.surveymonkey.server")


@pytest.fixture
def includes(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("include", ""))
        return httpx.Response(200, json={"data": [], "total": 0, "page": 1, "per_page": 50})

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(
        "magone_mcp_core.server._request_headers",
        lambda ctx: {"x-surveymonkey-access-token": "tok"},
    )
    return seen


async def test_collectors_ask_for_the_collector_fields(includes):
    async with Client(module.server.mcp) as client:
        await client.call_tool("surveymonkey_list_collectors", {"survey_id": "s-1"})

    assert includes[-1] == "type,status,response_count,date_created,date_modified,url"


async def test_surveys_ask_for_the_survey_fields(includes):
    async with Client(module.server.mcp) as client:
        await client.call_tool("surveymonkey_list_surveys", {})

    assert includes[-1] == "response_count,date_modified,question_count"
