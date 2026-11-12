"""Every connector that sets a User-Agent uses one pattern.

A site owner reading a log should see the same shape from every connector:
`Mozilla/5.0 (compatible; magoneai-<SERVER_NAME>/<SERVER_VERSION>; +<repository>)`.
"""

import importlib

import httpx
import pytest

pytestmark = pytest.mark.requires_connectors

CONNECTORS = ["cloudflare_r2", "outlook_email", "surveymonkey"]

try:
    modules = {c: importlib.import_module(f"connectors.{c}.server") for c in CONNECTORS}
except ImportError as exc:  # pragma: no cover - depends on the install
    pytest.skip(
        f"the connectors need their dependencies ({exc}); the Connector tests job installs them",
        allow_module_level=True,
    )


@pytest.mark.parametrize("connector", CONNECTORS)
def test_the_pattern(connector):
    module = modules[connector]
    assert module.USER_AGENT == (
        f"Mozilla/5.0 (compatible; magoneai-{module.SERVER_NAME}/{module.SERVER_VERSION}; "
        "+https://github.com/magurelabs/magoneai-mcp)"
    )


@pytest.fixture
def sent(monkeypatch):
    """Answer every request with 200 and record the headers it carried."""
    seen = []

    def handle(request):
        seen.append(request.headers.get("User-Agent"))
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient

    def client_with_mock_transport(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock_transport)
    return seen


@pytest.mark.asyncio
async def test_surveymonkey_sends_it_from_both_request_helpers(sent):
    """server.py has its own request(); tools/client.py has a second copy,
    which reads USER_AGENT from server.py at call time."""
    client = importlib.import_module("connectors.surveymonkey.tools.client")
    credentials = {"surveymonkey_access_token": "t"}

    await modules["surveymonkey"].request(credentials, "GET", "/users/me")
    await client.request(credentials, "GET", "/users/me")

    assert sent == [modules["surveymonkey"].USER_AGENT] * 2


@pytest.mark.asyncio
async def test_outlook_email_attachment_downloads_send_it(sent, monkeypatch):
    from magone_mcp_core import net

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port or 443))],
    )
    download = importlib.import_module("connectors.outlook_email.tools.download")

    await download.download_file("https://example.com/report.pdf")

    assert sent == [modules["outlook_email"].USER_AGENT]
