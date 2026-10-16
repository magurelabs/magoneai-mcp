"""The csv-to-sql connector, called through a real MCP client.

Two layers. The first runs everywhere with no database: a recording
connection stands in for asyncpg, so what is checked is the SQL the tools
send and the refusals they make. The second runs the whole flow against a
real PostgreSQL when `MAGONE_TEST_POSTGRES_DSN` names one, e.g.

    docker run --rm -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16-alpine
    MAGONE_TEST_POSTGRES_DSN=postgresql://postgres:pw@127.0.0.1:55432/postgres pytest
"""

import importlib
import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.csv_to_sql.server")
connection = importlib.import_module("connectors.csv_to_sql._connection")

V1_TOOLS = {
    "preview_csv",
    "create_table",
    "import_data",
    "list_tables",
    "get_table_info",
    "get_import",
    "list_imports",
}

CSV = "Name,Age,Joined,Active\nAda,36,2026-01-02,yes\nAlan,41,2026-02-03,no\n"


async def call(tool: str, args: dict) -> tuple[bool, dict]:
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(tool, args)
    return result.is_error, json.loads(result.content[0].text)


def use_headers(monkeypatch, headers: dict) -> None:
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(headers))


class Recording:
    """Enough of an asyncpg connection to see what the tools send."""

    def __init__(self, existing: bool = True):
        self.statements: list[str] = []
        self.existing = existing

    async def execute(self, sql, *args):
        self.statements.append(sql)
        return "OK"

    async def executemany(self, sql, rows):
        self.statements.append(sql)

    async def fetchval(self, sql, *args):
        self.statements.append(sql)
        return self.existing

    async def fetch(self, sql, *args):
        self.statements.append(sql)
        return []

    async def fetchrow(self, sql, *args):
        self.statements.append(sql)
        return None

    def transaction(self, **kwargs):
        @asynccontextmanager
        async def tx():
            yield

        return tx()


@pytest.fixture
def recording(monkeypatch):
    conn = Recording()

    @asynccontextmanager
    async def fake_connect(credentials):
        connection.resolve_dsn(credentials)
        yield conn

    monkeypatch.setattr(module, "connect", fake_connect)
    use_headers(monkeypatch, {"x-db-connection-string": "postgresql://u:p@db:5432/app"})
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)
    return conn


async def test_the_seven_v1_tools_are_listed_with_honest_annotations():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == V1_TOOLS
    assert tools["create_table"].annotations.read_only_hint is False
    assert tools["create_table"].annotations.destructive_hint is True
    assert tools["import_data"].annotations.read_only_hint is False
    for name in V1_TOOLS - {"create_table", "import_data"}:
        assert tools[name].annotations.read_only_hint is True, name


async def test_preview_infers_v1_types_without_a_database():
    failed, body = await call("preview_csv", {"csv_content": CSV})
    assert not failed
    types = {c["name"]: c["inferred_type"] for c in body["columns"]}
    assert types == {"name": "TEXT", "age": "INTEGER", "joined": "DATE", "active": "BOOLEAN"}
    assert body["row_count"] == 2


async def test_a_call_that_names_no_database_is_refused(monkeypatch):
    use_headers(monkeypatch, {})
    failed, body = await call("list_tables", {})
    assert failed and "X-Db-Connection-String" in body["error"]


async def test_a_database_that_is_not_postgresql_is_refused(monkeypatch):
    use_headers(monkeypatch, {"x-db-type": "MySQL", "x-db-connection-string": "mysql://u@h/d"})
    failed, body = await call("list_tables", {})
    assert failed and "PostgreSQL only" in body["error"]


def test_basic_connection_fields_build_a_quoted_dsn():
    dsn = connection.resolve_dsn(
        {
            "db_type": "PostgreSQL",
            "connection_type": "Basic",
            "db_host": "db.internal",
            "db_name": "sales",
            "db_username": "app",
            "db_password": "p@ss:/word",
        }
    )
    assert dsn == "postgresql://app:p%40ss%3A%2Fword@db.internal:5432/sales"


def test_an_sqlalchemy_style_uri_is_accepted():
    dsn = connection.resolve_dsn({"db_connection_string": "postgresql+asyncpg://u:p@h:5432/d"})
    assert dsn == "postgresql://u:p@h:5432/d"


async def test_a_schema_that_is_not_an_identifier_never_reaches_sql(recording):
    failed, body = await call(
        "create_table",
        {
            "table_name": "people",
            "schema": 'public"; DROP TABLE users; --',
            "columns": [{"name": "a", "inferred_type": "TEXT", "nullable": True}],
        },
    )
    assert failed and "not a valid identifier" in body["error"]
    assert not recording.statements


async def test_a_hostile_column_name_is_sanitized_into_an_identifier(recording):
    failed, body = await call(
        "create_table",
        {
            "table_name": "people.csv",
            "columns": [
                {"name": 'x" TEXT); DROP TABLE users; --', "inferred_type": "TEXT", "nullable": True}
            ],
        },
    )
    assert not failed, body
    create = next(s for s in recording.statements if s.startswith("CREATE TABLE IF NOT EXISTS"))
    assert '"public"."people"' in create
    assert '"x_text_drop_table_users" TEXT' in create
    assert "DROP TABLE users" not in create


async def test_an_unknown_column_type_is_refused(recording):
    failed, body = await call(
        "create_table",
        {"table_name": "t", "columns": [{"name": "a", "inferred_type": "BYTEA", "nullable": True}]},
    )
    assert failed and "inferred_type" in body["error"]


async def test_import_into_a_missing_table_says_so(recording):
    recording.existing = False
    failed, body = await call(
        "import_data",
        {"csv_content": CSV, "table_name": "nope", "columns": [{"name": "name", "inferred_type": "TEXT"}]},
    )
    assert failed and "create_table first" in body["error"]


async def test_list_imports_does_not_create_the_bookkeeping_table(recording):
    recording.existing = False
    failed, body = await call("list_imports", {})
    assert not failed and body["imports"] == []
    assert not any("CREATE TABLE" in s for s in recording.statements)


async def test_writes_are_refused_in_read_only_mode(recording, monkeypatch):
    monkeypatch.setenv("MCP_READ_ONLY", "1")
    async with Client(module.server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool(
            "create_table",
            {"table_name": "t", "columns": [{"name": "a", "inferred_type": "TEXT", "nullable": True}]},
        )
    assert {"create_table", "import_data"}.isdisjoint(names)
    assert result.is_error
    assert not recording.statements


# -- against a real PostgreSQL --------------------------------------------------

LIVE_DSN = os.getenv("MAGONE_TEST_POSTGRES_DSN")
live = pytest.mark.skipif(not LIVE_DSN, reason="MAGONE_TEST_POSTGRES_DSN is not set")


@live
async def test_the_whole_flow_against_postgresql(monkeypatch):
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)
    use_headers(monkeypatch, {"x-db-type": "PostgreSQL", "x-db-connection-string": LIVE_DSN})
    table = f"people_{uuid.uuid4().hex[:8]}"
    csv_content = CSV + "Grace,not-a-number,2026-03-04,yes\n"

    failed, preview = await call("preview_csv", {"csv_content": CSV})
    assert not failed
    columns = preview["columns"]

    failed, body = await call("create_table", {"table_name": f"{table}.csv", "columns": columns})
    assert not failed, body

    failed, body = await call(
        "import_data", {"csv_content": csv_content, "table_name": table, "columns": columns}
    )
    assert not failed, body
    assert body["rows_imported"] == 2
    assert len(body["errors"]) == 1 and body["errors"][0].startswith("Row 4:")
    import_id = body["import_id"]

    failed, body = await call("get_table_info", {"table_name": table})
    assert not failed and body["row_count"] == 2
    assert [c["column_name"] for c in body["columns"]] == ["id", "name", "age", "joined", "active"]

    failed, body = await call("list_tables", {})
    assert not failed and table in [t["table_name"] for t in body["tables"]]

    failed, body = await call("get_import", {"import_id": import_id})
    assert not failed and body["import"]["row_count"] == 2

    failed, body = await call("list_imports", {"limit": 5})
    assert not failed and import_id in [i["import_id"] for i in body["imports"]]

    failed, body = await call(
        "create_table", {"table_name": table, "columns": columns, "drop_existing": True}
    )
    assert not failed
    failed, body = await call("get_table_info", {"table_name": table})
    assert not failed and body["row_count"] == 0


@live
async def test_a_missing_table_is_reported_not_raised(monkeypatch):
    use_headers(monkeypatch, {"x-db-connection-string": LIVE_DSN})
    failed, body = await call("get_table_info", {"table_name": "no_such_table_here"})
    assert failed and "Table not found" in body["error"]


@live
async def test_a_wrong_password_is_reported_without_echoing_it(monkeypatch):
    from urllib.parse import urlparse

    parsed = urlparse(LIVE_DSN)
    use_headers(
        monkeypatch,
        {
            "x-connection-type": "Basic",
            "x-db-host": parsed.hostname,
            "x-db-port": str(parsed.port or 5432),
            "x-db-name": parsed.path.lstrip("/"),
            "x-db-username": parsed.username,
            "x-db-password": "definitely-wrong-zz9",
        },
    )
    failed, body = await call("list_tables", {})
    assert failed and "Connection failed" in body["error"]
    assert "definitely-wrong-zz9" not in body["error"]
