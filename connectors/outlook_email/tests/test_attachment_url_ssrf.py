"""attachment_urls goes through the framework's SSRF check on every hop.

send_email, create_draft and reply_email fetch caller-supplied URLs. They used
to carry a private copy of the check that tested only `is_private`,
`is_loopback` and `is_link_local`: it missed carrier-grade NAT (a cloud
metadata address on one provider), IPv6 site-local, and every IPv6 form that
wraps an IPv4 address, and it judged the name before connecting but never the
address connected to.

Through a real MCP client, with the network replaced at the httpx transport:
a refused URL must never reach the transport at all.
"""

import socket

import httpx
import pytest
from mcp import Client

from connectors.outlook_email.tests._graph_mock import install, module

pytestmark = pytest.mark.requires_connectors

PUBLIC = "http://93.184.216.34/report.txt"

REFUSED = [
    pytest.param("http://100.100.100.200/latest/meta-data", id="cgnat-metadata"),
    pytest.param("http://100.64.0.1/x", id="cgnat"),
    pytest.param("http://[64:ff9b::a9fe:a9fe]/x", id="nat64-link-local"),
    pytest.param("http://[fec0::1]/x", id="ipv6-site-local"),
    pytest.param("http://169.254.169.254/latest/meta-data", id="metadata"),
    pytest.param("http://[::ffff:127.0.0.1]/x", id="ipv4-mapped-loopback"),
    pytest.param("http://127.0.0.1/x", id="loopback"),
    pytest.param("file:///etc/passwd", id="scheme"),
]


class Peer:
    """What httpx exposes as the `network_stream` of a real connection."""

    def __init__(self, ip: str):
        self.ip = ip

    def get_extra_info(self, name: str):
        return (self.ip, 80) if name == "server_addr" else None


def router(attachment_hits: list, peer: str | None = None, redirect_to: str | None = None):
    """Graph answers 2xx; anything else is the attachment fetch, which is recorded."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(201, json={"id": "draft-1"})
        attachment_hits.append(str(request.url))
        extensions = {"network_stream": Peer(peer)} if peer else {}
        if redirect_to and len(attachment_hits) == 1:
            return httpx.Response(302, headers={"location": redirect_to}, extensions=extensions)
        return httpx.Response(
            200, content=b"hello", headers={"content-type": "text/plain"}, extensions=extensions
        )

    return route


async def call(tool: str, url: str):
    arguments = {
        "create_draft": {"to": "a@example.com", "subject": "s", "body": "b"},
        "send_email": {"to": "a@example.com", "subject": "s", "body": "b"},
        "reply_email": {"email_id": "m-1", "body": "b"},
    }[tool]
    async with Client(module.server.mcp) as client:
        return await client.call_tool(tool, {**arguments, "attachment_urls": [url]})


TOOLS = ["create_draft", "send_email", "reply_email"]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("url", REFUSED)
async def test_internal_urls_are_refused_before_any_request(monkeypatch, tool, url):
    hits: list = []
    graph = install(monkeypatch, router(hits))

    result = await call(tool, url)

    assert result.is_error is True
    assert "Failed to download attachment" in result.content[0].text
    assert hits == []
    # Nothing was created or sent either.
    assert [r for r in graph if r.method != "GET"] == []


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_public_address_is_fetched_and_attached(monkeypatch, tool):
    hits: list = []
    graph = install(monkeypatch, router(hits))

    result = await call(tool, PUBLIC)

    assert result.is_error is False, result.content[0].text
    assert hits == [PUBLIC]
    assert any(b"report.txt" in r.content for r in graph if r.method == "POST")


async def test_a_redirect_to_an_internal_address_is_refused(monkeypatch):
    hits: list = []
    install(monkeypatch, router(hits, redirect_to="http://100.100.100.200/latest/meta-data"))

    result = await call("create_draft", PUBLIC)

    assert result.is_error is True
    assert hits == [PUBLIC]


async def test_the_connected_address_is_checked_not_just_the_name(monkeypatch):
    """A name that resolved public for the check but connected somewhere internal."""
    monkeypatch.setattr(
        "magone_mcp_core.net.socket.getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
    )
    hits: list = []
    install(monkeypatch, router(hits, peer="169.254.169.254"))

    result = await call("create_draft", "http://files.example.com/report.txt")

    assert result.is_error is True
    assert "internal address" in result.content[0].text
    assert hits == ["http://files.example.com/report.txt"]


async def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(
        "magone_mcp_core.net.socket.getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.100.100.200", 80))],
    )
    hits: list = []
    install(monkeypatch, router(hits))

    result = await call("create_draft", "http://files.example.com/report.txt")

    assert result.is_error is True
    assert hits == []


def test_there_is_one_ssrf_implementation():
    """The connector keeps no address list of its own."""
    from pathlib import Path

    for name in ("server.py", "tools/download.py"):
        source = (Path(module.__file__).parent / name).read_text()
        assert "_is_private_ip" not in source
        assert "_check_ssrf" not in source
        assert "getaddrinfo" not in source
