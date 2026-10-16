"""The vanna connector, called through a real MCP client.

The model is always a stand-in: a fake OpenAI-compatible client that records
the prompt it was sent and answers with fixed SQL, so nothing leaves the
process. The database is a recording stand-in by default, and a real
PostgreSQL when `MAGONE_TEST_POSTGRES_DSN` names one (see the csv-to-sql
tests for a one-line container). The live tests also need a second database
on the same server, named by `MAGONE_TEST_POSTGRES_DSN_2`, to show training
does not cross databases.
"""

import importlib
import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.vanna.server")
store = importlib.import_module("connectors.vanna._store")
connection = importlib.import_module("connectors.vanna._connection")

V1_TOOLS = {
    "create_context",
    "list_contexts",
    "get_context",
    "delete_context",
    "connect_database",
    "discover_schemas",
    "discover_tables",
    "ask_database",
    "train_from_schema",
    "upload_training",
    "add_training_pair",
    "get_training_status",
    "clear_training",
}
READ_ONLY = {
    "list_contexts",
    "get_context",
    "connect_database",
    "discover_schemas",
    "discover_tables",
    "ask_database",
    "get_training_status",
}


class _Choice:
    def __init__(self, content):
        self.message = type("Message", (), {"content": content})()

    def __iter__(self):  # the real Choice is a pydantic model; `"text" in choice` iterates
        return iter(())


class FakeModel:
    """An OpenAI-compatible client that answers every prompt with `reply`."""

    reply = "SELECT 1;"
    instances: list["FakeModel"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prompts = []
        FakeModel.instances.append(self)
        completions = type("Completions", (), {"create": self._create})()
        self.chat = type("Chat", (), {"completions": completions})()

    def _create(self, model, messages, **kwargs):
        self.prompts.append({"model": model, "messages": messages})
        return type("Response", (), {"choices": [_Choice(FakeModel.reply)]})()


@pytest.fixture
def model(monkeypatch):
    FakeModel.instances = []
    FakeModel.reply = "SELECT 1;"
    monkeypatch.setattr("openai.OpenAI", FakeModel)
    return FakeModel


def use_headers(monkeypatch, headers: dict) -> None:
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(headers))


async def call(tool: str, args: dict | None = None) -> tuple[bool, dict]:
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(tool, args or {})
    return result.is_error, json.loads(result.content[0].text)


# -- no database needed ----------------------------------------------------------


async def test_the_thirteen_v1_tools_are_listed_with_honest_annotations():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == V1_TOOLS
    for name, tool in tools.items():
        assert tool.annotations.read_only_hint is (name in READ_ONLY), name
    assert tools["delete_context"].annotations.destructive_hint is True
    assert tools["clear_training"].annotations.destructive_hint is True
    assert tools["connect_database"].input_schema["properties"] == {}


async def test_no_database_means_a_refusal_not_a_default(monkeypatch):
    use_headers(monkeypatch, {})
    failed, body = await call("list_contexts")
    assert failed and "X-Db-Connection-String" in body["error"]


async def test_ask_needs_the_callers_own_model_key(monkeypatch, model):
    use_headers(monkeypatch, {"x-db-connection-string": "postgresql://u:p@db/app"})
    failed, body = await call("ask_database", {"question": "how many?"})
    assert failed and "X-Llm-Api-Key" in body["error"]
    assert not model.instances


async def test_a_model_endpoint_inside_the_network_is_refused(monkeypatch, model):
    use_headers(
        monkeypatch,
        {
            "x-db-connection-string": "postgresql://u:p@db/app",
            "x-llm-api-key": "sk-test",
            "x-llm-base-url": "http://169.254.169.254/v1",
        },
    )
    failed, body = await call("ask_database", {"question": "how many?"})
    assert failed and "X-Llm-Base-Url refused" in body["error"]
    assert not model.instances


async def test_a_context_name_is_validated_everywhere(monkeypatch):
    use_headers(monkeypatch, {"x-db-connection-string": "postgresql://u:p@db/app"})
    for tool, args in [
        ("create_context", {"name": "bad name;"}),
        ("add_training_pair", {"question": "q", "sql": "s", "context": "../x"}),
        ("clear_training", {"context": "a-b"}),
    ]:
        failed, body = await call(tool, args)
        assert failed, tool
        assert "alphanumeric" in json.dumps(body), tool


def test_retrieval_ranks_by_shared_words_and_keeps_everything_when_small():
    items = [{"kind": "ddl", "question": None, "content": f"CREATE TABLE t{i} (x int)"} for i in range(30)]
    items.append({"kind": "ddl", "question": None, "content": "CREATE TABLE invoices (amount int)"})
    ranked = store.rank("total invoices amount", items)
    assert ranked[0]["content"].startswith("CREATE TABLE invoices")
    assert len(ranked) == store.N_RESULTS
    assert store.rank("anything", items[:3]) == items[:3]


def test_v1_training_file_formats_still_parse():
    csv_items, _ = module._parse_training_file(
        "type,key,value,sql\n"
        'question,What is the cancellation rate?,,"SELECT 1"\n'
        "column,status,Booking status,\n"
        "term,ATS,Average Ticket Size,\n",
        "csv",
    )
    assert [i[0] for i in csv_items] == ["questions", "columns", "terminology"]
    json_items, _ = module._parse_training_file(
        json.dumps(
            {
                "question_sql_pairs": [{"question": "q", "sql": "SELECT 2"}],
                "ddl": ["CREATE TABLE a (b int)", {"content": "CREATE TABLE c (d int)"}],
                "documentation": ["doc"],
                "columns": {"a.b": "the b"},
                "terminology": {"ARR": "annual recurring revenue"},
            }
        ),
        "json",
    )
    assert [i[0] for i in json_items] == [
        "questions",
        "ddl",
        "ddl",
        "documentation",
        "columns",
        "terminology",
    ]


class FakeConnection:
    """Enough of asyncpg for ask_database: a training set and one result row."""

    def __init__(self, items):
        self.items = items
        self.executed = []
        self.readonly = None

    async def fetchval(self, sql, *args):
        return True

    async def fetch(self, sql, *args):
        return self.items

    def transaction(self, readonly=False, **kwargs):
        self.readonly = readonly

        @asynccontextmanager
        async def tx():
            yield

        return tx()

    async def cursor(self, sql, timeout=None):
        self.executed.append(sql)

        class Cursor:
            async def fetch(self, n, timeout=None):
                return [{"n": 42}]

        return Cursor()


async def test_ask_uses_the_callers_model_and_training_and_runs_read_only(monkeypatch, model):
    conn = FakeConnection(
        [
            {"id": 2, "kind": "ddl", "question": None, "content": "CREATE TABLE orders (id int)"},
            {"id": 1, "kind": "sql", "question": "how many orders", "content": "SELECT count(*) FROM orders"},
        ]
    )

    @asynccontextmanager
    async def fake_connect(credentials):
        connection.resolve_dsn(credentials)
        yield conn

    monkeypatch.setattr(module, "connect", fake_connect)
    use_headers(
        monkeypatch,
        {
            "x-db-connection-string": "postgresql://u:p@db/app",
            "x-llm-api-key": "sk-caller-own",
            "x-llm-model": "some-model",
        },
    )
    model.reply = "```sql\nSELECT count(*) AS n FROM orders;\n```"

    failed, body = await call("ask_database", {"question": "How many orders are there?"})

    assert not failed, body
    assert body["results"] == [{"n": 42}]
    assert body["sql"] == "SELECT count(*) AS n FROM orders LIMIT 100"
    assert conn.readonly is True
    (client,) = model.instances
    assert client.kwargs["api_key"] == "sk-caller-own"
    prompt = client.prompts[0]
    assert prompt["model"] == "some-model"
    rendered = json.dumps(prompt["messages"])
    assert "CREATE TABLE orders" in rendered and "how many orders" in rendered


async def test_ask_refuses_to_execute_anything_but_select(monkeypatch, model):
    conn = FakeConnection([])

    @asynccontextmanager
    async def fake_connect(credentials):
        yield conn

    monkeypatch.setattr(module, "connect", fake_connect)
    use_headers(monkeypatch, {"x-db-connection-string": "postgresql://u:p@db/app", "x-llm-api-key": "k"})
    model.reply = "DELETE FROM orders;"
    failed, body = await call("ask_database", {"question": "remove everything"})
    assert failed and "Only SELECT" in body["error"]
    assert not conn.executed


async def test_training_tools_are_refused_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("MCP_READ_ONLY", "1")
    use_headers(monkeypatch, {"x-db-connection-string": "postgresql://u:p@db/app"})
    async with Client(module.server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("clear_training", {})
    assert names == READ_ONLY
    assert result.is_error


# -- against a real PostgreSQL -------------------------------------------------

LIVE_DSN = os.getenv("MAGONE_TEST_POSTGRES_DSN")
LIVE_DSN_2 = os.getenv("MAGONE_TEST_POSTGRES_DSN_2")
live = pytest.mark.skipif(not LIVE_DSN, reason="MAGONE_TEST_POSTGRES_DSN is not set")


@pytest.fixture
async def schema():
    import asyncpg

    name = f"t_{uuid.uuid4().hex[:10]}"
    conn = await asyncpg.connect(LIVE_DSN)
    await conn.execute(
        f"""
        CREATE SCHEMA {name};
        CREATE TABLE {name}.customers (id serial PRIMARY KEY, name text NOT NULL, region text);
        CREATE TABLE {name}.orders (
            id serial PRIMARY KEY,
            customer_id int REFERENCES {name}.customers(id),
            amount numeric(10,2)
        );
        COMMENT ON TABLE {name}.orders IS 'One row per order';
        COMMENT ON COLUMN {name}.orders.amount IS 'Order total in dollars';
        INSERT INTO {name}.customers (name, region) VALUES ('Ada', 'north'), ('Alan', 'south');
        INSERT INTO {name}.orders (customer_id, amount) VALUES (1, 10.50), (2, 7.25);
        """
    )
    yield name
    await conn.execute(f"DROP SCHEMA {name} CASCADE")
    if await conn.fetchval("SELECT to_regclass('_vanna_training') IS NOT NULL"):
        await conn.execute('DELETE FROM "_vanna_training" WHERE context LIKE $1', "ctx_%")
    await conn.close()


@live
async def test_train_then_ask_against_postgresql(monkeypatch, model, schema):
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)
    context = f"ctx_{uuid.uuid4().hex[:8]}"
    use_headers(
        monkeypatch,
        {"x-db-type": "PostgreSQL", "x-db-connection-string": LIVE_DSN, "x-llm-api-key": "sk-x"},
    )

    failed, body = await call("connect_database")
    assert not failed and body["version"].startswith("PostgreSQL")

    failed, body = await call("discover_tables", {"schema": schema})
    assert not failed
    assert {t["name"] for t in body["tables"]} == {"customers", "orders"}

    failed, body = await call("create_context", {"name": context, "description": "sales"})
    assert not failed, body
    failed, body = await call("create_context", {"name": context})
    assert failed and "already exists" in body["error"]

    failed, body = await call("train_from_schema", {"schema": schema, "context": context})
    assert not failed, body
    assert body["details"]["tables"] == ["customers", "orders"]
    assert body["details"]["relationships_trained"] == 1
    assert body["details"]["comments_trained"] == 2

    failed, body = await call(
        "add_training_pair",
        {
            "question": "total order amount",
            "sql": f"SELECT sum(amount) FROM {schema}.orders",
            "context": context,
        },
    )
    assert not failed

    failed, body = await call("get_training_status", {"context": context})
    assert not failed and body["count"] == 6

    model.reply = f"SELECT name FROM {schema}.customers ORDER BY name;"
    failed, body = await call("ask_database", {"question": "list customer names", "context": context})
    assert not failed, body
    assert [r["name"] for r in body["results"]] == ["Ada", "Alan"]
    rendered = json.dumps(model.instances[-1].prompts[0]["messages"])
    assert "Order total in dollars" in rendered and "total order amount" in rendered

    failed, body = await call("get_context", {"name": context})
    assert not failed and body["context"]["tables"] == ["customers", "orders"]

    failed, body = await call("delete_context", {"name": context})
    assert not failed
    failed, body = await call("get_training_status", {"context": context})
    assert not failed and body["count"] == 0, "deleting a context left its training behind"


@live
async def test_generated_sql_that_writes_is_stopped_by_the_read_only_transaction(
    monkeypatch, model, schema
):
    use_headers(monkeypatch, {"x-db-connection-string": LIVE_DSN, "x-llm-api-key": "sk-x"})
    model.reply = f"SELECT nextval('{schema}.customers_id_seq');"
    failed, body = await call("ask_database", {"question": "bump the sequence"})
    assert failed and "read-only transaction" in body["error"]

    # A second statement never reaches the database: vanna's extraction keeps
    # the first, and a cursor runs one prepared statement or none.
    model.reply = f"SELECT 1; DELETE FROM {schema}.orders;"
    failed, body = await call("ask_database", {"question": "two statements"})
    assert "DELETE" not in body["sql"]

    model.reply = f"WITH d AS (DELETE FROM {schema}.orders RETURNING *) SELECT count(*) FROM d;"
    failed, body = await call("ask_database", {"question": "a writable CTE"})
    assert failed and "Only SELECT" in body["error"]

    import asyncpg

    conn = await asyncpg.connect(LIVE_DSN)
    assert await conn.fetchval(f"SELECT count(*) FROM {schema}.orders") == 2
    await conn.close()


@live
@pytest.mark.skipif(not LIVE_DSN_2, reason="MAGONE_TEST_POSTGRES_DSN_2 is not set")
async def test_training_in_one_database_is_invisible_from_another(monkeypatch, schema):
    context = f"ctx_{uuid.uuid4().hex[:8]}"
    use_headers(monkeypatch, {"x-db-connection-string": LIVE_DSN})
    failed, _ = await call(
        "add_training_pair", {"question": "q", "sql": "SELECT 1", "context": context}
    )
    assert not failed

    use_headers(monkeypatch, {"x-db-connection-string": LIVE_DSN_2})
    failed, body = await call("get_training_status", {"context": context})
    assert not failed and body["count"] == 0

    use_headers(monkeypatch, {"x-db-connection-string": LIVE_DSN})
    failed, body = await call("get_training_status", {"context": context})
    assert not failed and body["count"] == 1
