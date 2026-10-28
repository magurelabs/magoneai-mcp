"""OTP codes are random, stored only as a salted hash, and checked in constant time.

The codes used to be minted by SQL `RANDOM()` (not a cryptographic source),
stored as plaintext, sent again on a repeat request, compared with `==`, and the
raw identifier was logged on lockout.
"""

import importlib
import inspect
import json

import pytest
from mcp import Client

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.otp_verification.server")


class FakeDB:
    """Just enough of the otp table to run generate_otp and verify_otp."""

    def __init__(self):
        self.rows: list[dict] = []
        self.queries: list[str] = []

    def pending(self):
        return [r for r in self.rows if r["status"] == "pending"]

    async def execute_one(self, conn, query, *args):
        self.queries.append(query)
        if query.startswith("INSERT"):
            row = {
                "id": len(self.rows) + 1,
                "code": args[3],
                "status": "pending",
                "attempts": 0,
                "max_attempts": 3,
                "metadata": args[5],
            }
            self.rows.append(row)
            return {"id": row["id"]}
        if "COUNT" in query:
            return {"cnt": len(self.rows)}
        if "status = 'locked'" in query:
            return {"id": 1} if any(r["status"] == "locked" for r in self.rows) else None
        if "status = 'pending'" in query:
            live = self.pending()
            return dict(live[-1]) if live else None
        return None

    async def execute(self, query, *args):
        self.queries.append(query)
        if "SET status = 'expired'" in query:
            for r in self.pending():
                r["status"] = "expired"
        elif "SET status = 'verified'" in query:
            self._row(args[0])["status"] = "verified"
        elif "SET status = 'locked'" in query:
            row = self._row(args[0])
            row["status"], row["attempts"] = "locked", args[1]
        elif "SET attempts" in query:
            self._row(args[0])["attempts"] = args[1]
        return "UPDATE 1"

    def _row(self, row_id):
        return next(r for r in self.rows if r["id"] == row_id)

    async def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()

    async def fake_get_connection(credentials):
        return fake

    monkeypatch.setattr(module, "get_connection", fake_get_connection)
    monkeypatch.setattr(module, "execute_one", lambda conn, q, *a: fake.execute_one(conn, q, *a))
    monkeypatch.setattr(
        "magone_mcp_core.server._request_headers",
        lambda ctx: {"x-db-connection-string": "postgresql://u:p@db/app"},
    )
    return fake


async def call(name, **args):
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(name, args)
    # A refusal comes back with isError set and the same JSON body.
    return json.loads(result.content[0].text)


async def generate(**kw):
    return await call("generate_otp", identifier="a@example.com", **kw)


async def verify(code, identifier="a@example.com"):
    return await call("verify_otp", identifier=identifier, code=code)


def test_code_is_not_minted_in_sql():
    source = inspect.getsource(module)
    assert "RANDOM()" not in source.upper().replace(" ", "")
    assert "secrets.randbelow" in source


def test_new_code_is_zero_padded_to_length(monkeypatch):
    monkeypatch.setattr(module.secrets, "randbelow", lambda n: 42)
    assert module._new_code(6) == "000042"


async def test_stored_value_is_a_salted_hash_not_the_code(db):
    out = await generate()

    assert len(out["code"]) == module.DEFAULT_OTP_LENGTH and out["code"].isdigit()
    stored = db.rows[0]["code"]
    assert stored != out["code"]
    assert stored.startswith("sha256$")
    assert not any("RANDOM" in q.upper() for q in db.queries)


def test_same_code_hashes_differently_each_time():
    assert module._hash_code("123456") != module._hash_code("123456")


async def test_right_code_verifies(db):
    out = await generate()
    result = await verify(out["code"])
    assert result["verified"] is True
    assert db.rows[0]["status"] == "verified"


async def test_wrong_code_fails_and_counts_an_attempt(db):
    out = await generate()
    wrong = "000000" if out["code"] != "000000" else "111111"
    result = await verify(wrong)
    assert result["verified"] is False
    assert result["attempts_remaining"] == 2
    assert db.rows[0]["status"] == "pending"


async def test_legacy_plaintext_row_does_not_verify(db):
    db.rows.append(
        {"id": 1, "code": "123456", "status": "pending", "attempts": 0, "max_attempts": 3}
    )
    result = await verify("123456")
    assert result["verified"] is False


async def test_second_request_issues_a_new_code_and_retires_the_old(db):
    first = await generate()
    second = await generate()

    assert second["is_resend"] is True
    assert first["is_resend"] is False
    assert len(db.pending()) == 1
    assert db.rows[0]["status"] == "expired"
    assert db.rows[1]["code"] != db.rows[0]["code"]
    if first["code"] != second["code"]:
        assert (await verify(first["code"]))["verified"] is False
    assert (await verify(second["code"]))["verified"] is True


async def test_resend_counts_toward_the_limit(db):
    for _ in range(module.DEFAULT_MAX_RESENDS):
        assert (await generate())["success"] is True
    out = await generate()
    assert out["success"] is False
    assert "Too many" in out["error"]


async def test_lockout_still_works_and_does_not_log_the_identifier(db, monkeypatch):
    logged = []
    monkeypatch.setattr(
        module.logger, "warning", lambda event, **kw: logged.append((event, kw))
    )
    out = await generate()
    wrong = "000000" if out["code"] != "000000" else "111111"

    for _ in range(module.DEFAULT_MAX_ATTEMPTS - 1):
        assert (await verify(wrong))["attempts_remaining"] >= 1
    last = await verify(wrong)

    assert last["attempts_remaining"] == 0
    assert db.rows[0]["status"] == "locked"
    again = await verify(out["code"])
    assert again["verified"] is False and "Locked" in again["error"]

    assert [e for e, _ in logged] == ["otp_locked"]
    assert "a@example.com" not in repr(logged)
