"""Every database tool reaches a driver, through the MCP layer.

All eight failed on every call for every driver. The module carried a
flattened copy of the whole driver package above the tools, and the copy was
what the tools resolved against: it shadowed `time` with `datetime.time` and
swapped the structlog logger for a stdlib one, so the error handler itself
raised. The tools now import the real package, as v1 did.

A recording connector stands in for the driver so this runs without a
database; the live check against real Postgres, MySQL and Mongo is recorded in
the programme's fix tracker.
"""

import importlib

import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.database.server")

HEADERS = {"x-db-type": "postgresql", "x-db-connection-string": "postgresql://u:p@db:5432/app"}


class Recording:
    def __init__(self):
        self.calls = []

    async def query(self, sql, params=None):
        self.calls.append(("query", sql))
        return [{"id": 1}]

    async def insert(self, table, records):
        self.calls.append(("insert", table))
        return {"inserted_count": len(records)}

    async def update(self, table, filters, data):
        self.calls.append(("update", table))
        return {"updated_count": 1}

    async def search(self, table, filters=None, limit=100, offset=0, order_by=None):
        self.calls.append(("search", table))
        return [{"id": 1}]

    async def get_tables(self):
        self.calls.append(("get_tables", None))
        return ["t"]

    async def get_schema(self, table):
        self.calls.append(("get_schema", table))
        return [{"name": "id", "type": "integer", "nullable": False, "primary_key": True}]

    async def create_table(self, table, columns):
        self.calls.append(("create_table", table))
        return {"created": True, "table": table}

    async def drop_table(self, table):
        self.calls.append(("drop_table", table))
        return {"dropped": True, "table": table}


@pytest.fixture
def connector(monkeypatch):
    recording = Recording()

    async def fake_create_connector(connection_string):
        assert connection_string == HEADERS["x-db-connection-string"]
        return recording

    monkeypatch.setattr(module, "create_connector", fake_create_connector)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS))
    return recording


CALLS = [
    ("db_list_tables", {}, "get_tables"),
    ("db_schema", {"table": "t"}, "get_schema"),
    ("db_query", {"query": "SELECT id FROM t"}, "query"),
    ("db_search", {"table": "t", "filters": {"id": 1}}, "search"),
    ("db_insert", {"table": "t", "data": [{"id": 2}]}, "insert"),
    ("db_update", {"table": "t", "filters": {"id": 1}, "data": {"a": 2}}, "update"),
    (
        "db_create_table",
        {"table": "t2", "columns": [{"name": "id", "type": "INTEGER"}]},
        "create_table",
    ),
    ("db_drop_table", {"table": "t2", "confirm": True}, "drop_table"),
]


@pytest.mark.parametrize("tool,args,expected", CALLS, ids=[c[0] for c in CALLS])
async def test_the_tool_reaches_the_driver(connector, tool, args, expected):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(tool, args)

    assert result.is_error is False, result.content[0].text
    assert expected in [kind for kind, _ in connector.calls]


async def test_db_query_bounds_the_rows_it_asks_for(connector):
    async with Client(module.server.mcp) as client:
        await client.call_tool("db_query", {"query": "SELECT id FROM t"})

    assert connector.calls[-1] == ("query", "SELECT id FROM t LIMIT 101")


async def test_db_query_refuses_a_write(connector):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool("db_query", {"query": "DELETE FROM t"})

    assert result.is_error is True
    assert not connector.calls


def test_the_module_imports_the_driver_package_rather_than_a_copy():
    from connectors.database import connectors as package

    assert module.create_connector is package.create_connector


async def test_a_driver_failure_is_reported_not_raised_by_the_handler(monkeypatch):
    """The error path is where the stdlib logger broke: every handler logs
    `logger.exception(..., error=str(e))`, which structlog accepts and the
    stdlib `Logger` rejects with TypeError, so a refused connection surfaced as
    an internal error rather than as the driver's reason."""

    async def refusing(connection_string):
        raise ConnectionRefusedError("connection refused by db:5432")

    monkeypatch.setattr(module, "create_connector", refusing)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(HEADERS))

    async with Client(module.server.mcp) as client:
        result = await client.call_tool("db_list_tables", {})

    assert result.is_error is True
    assert "connection refused" in result.content[0].text.lower()
