"""The MySQL connector honours the connection string's ssl-mode.

It used to build the pool from host, user, password and database only, so the
query string was dropped and every connection was plaintext. A server with
require_secure_transport=ON (or TiDB Cloud) then refused every login with
error 3159, whatever the URL said.
"""

import ssl

import pytest

from connectors.database.connectors import mysql as m


class FakeAiomysql:
    def __init__(self):
        self.kwargs = None

    async def create_pool(self, **kwargs):
        self.kwargs = kwargs
        return object()


@pytest.fixture
def fake(monkeypatch):
    f = FakeAiomysql()
    monkeypatch.setattr(m, "aiomysql", f)
    return f


async def _connect(fake, url):
    await m.MySQLConnector().connect(url)
    return fake.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["", "?ssl-mode=DISABLED", "?sslmode=disable", "?ssl=false"])
async def test_no_tls_unless_asked(fake, q):
    kw = await _connect(fake, f"mysql://u:p@db:3306/app{q}")
    assert kw["ssl"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "q", ["?ssl-mode=REQUIRED", "?ssl_mode=required", "?sslmode=require", "?ssl=true"]
)
async def test_required_encrypts_without_verifying(fake, q):
    ctx = (await _connect(fake, f"mysql://u:p@db:3306/app{q}"))["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname


@pytest.mark.asyncio
async def test_verify_ca_checks_chain_not_name(fake):
    ctx = (await _connect(fake, "mysql://u:p@db/app?ssl-mode=VERIFY_CA"))["ssl"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and not ctx.check_hostname


@pytest.mark.asyncio
async def test_verify_identity_checks_everything(fake):
    ctx = (await _connect(fake, "mysql://u:p@db/app?ssl-mode=VERIFY_IDENTITY"))["ssl"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.asyncio
async def test_unknown_mode_is_an_error(fake):
    with pytest.raises(ValueError, match="ssl-mode"):
        await _connect(fake, "mysql://u:p@db/app?ssl-mode=sometimes")


@pytest.mark.asyncio
async def test_url_encoded_credentials_are_decoded(fake):
    kw = await _connect(fake, "mysql://us%40er:p%40ss%3Aw@db:3307/my%20db?ssl-mode=REQUIRED")
    assert (kw["user"], kw["password"], kw["db"], kw["port"]) == ("us@er", "p@ss:w", "my db", 3307)
