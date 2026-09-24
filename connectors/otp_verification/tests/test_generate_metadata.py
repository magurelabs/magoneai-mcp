"""generate_otp accepts the metadata its schema describes.

The schema calls `metadata` "a JSON string", and the handler was typed `str`.
The SDK parses a JSON-looking string argument into an object before
validating it, so every well-formed value was rejected with "Input should be
a valid string" and only malformed JSON got through to the handler.
"""

import importlib
import json

import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.otp_verification.server")


class FakeConn:
    async def execute(self, *args):
        return "UPDATE 0"

    async def close(self):
        pass


@pytest.fixture
def stored(monkeypatch):
    inserts: list[tuple] = []

    async def fake_get_connection(credentials):
        return FakeConn()

    async def fake_execute_one(conn, query, *args):
        if query.startswith("INSERT"):
            inserts.append(args)
            return {"code": "123456"}
        if "COUNT" in query:
            return {"cnt": 0}
        return None

    monkeypatch.setattr(module, "get_connection", fake_get_connection)
    monkeypatch.setattr(module, "execute_one", fake_execute_one)
    monkeypatch.setattr(
        "magone_mcp_core.server._request_headers",
        lambda ctx: {"x-db-connection-string": "postgresql://u:p@db/app"},
    )
    return inserts


@pytest.mark.parametrize(
    "metadata", ['{"order": "A-1"}', {"order": "A-1"}], ids=["json-string", "object"]
)
async def test_metadata_is_stored(stored, metadata):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(
            "generate_otp", {"identifier": "a@example.com", "metadata": metadata}
        )

    assert result.is_error is False, result.content[0].text
    assert json.loads(stored[-1][-1]) == {"order": "A-1"}


async def test_malformed_metadata_is_refused(stored):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(
            "generate_otp", {"identifier": "a@example.com", "metadata": "{not json"}
        )

    assert result.is_error is True
    assert not stored
