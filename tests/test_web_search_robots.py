"""`web_search` says who it is, and a crawl asks robots.txt first.

`scrape_url` with `return_full` is the path a crawler walks link by link. A
site owner should be able to tell it from a person and turn it away, so these
tests pin the User-Agent and the RFC 9309 behaviour of the robots check.
"""

import importlib
import json

import httpx
import pytest

pytestmark = pytest.mark.requires_connectors

# Same import dance as test_scrape_url_ssrf.py: this module is imported during
# collection even in an install without the connector dependencies.
try:
    web_search = importlib.import_module("connectors.web_search.server")
except ImportError as exc:  # pragma: no cover - depends on the install
    pytest.skip(
        f"connectors.web_search.server needs the connector dependencies ({exc}); "
        "the Connector tests job installs them",
        allow_module_level=True,
    )
_scrape_url = web_search.server.mcp._tool_manager._tools["scrape_url"].fn


async def scrape_url(**kwargs):
    result = await _scrape_url(**kwargs)
    if isinstance(result, dict):
        return result
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """Every hostname resolves to one public address; no test touches DNS."""
    from magone_mcp_core import net

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def empty_robots_cache():
    web_search._robots_cache.clear()
    yield
    web_search._robots_cache.clear()


@pytest.fixture
def site(monkeypatch):
    """Serve robots.txt from a handler the test sets; record every request."""
    state = {"handler": None, "requests": []}

    def handle(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    real_client = httpx.AsyncClient

    def client_with_mock_transport(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(web_search.httpx, "AsyncClient", client_with_mock_transport)
    return state


@pytest.fixture
def crawl(monkeypatch):
    """Stand in for Crawl4AI and record which URLs reached it."""
    crawled = []

    async def fake_rich(url, extra_args=None):
        crawled.append(url)
        return {"success": True, "url": url, "markdown": "page", "content": "page"}

    monkeypatch.setattr(web_search, "_fetch_via_crawl4ai_rich", fake_rich)
    return crawled


class TestTheUserAgentIsHonest:
    def test_it_names_the_connector_and_the_project(self):
        assert "Mozilla" not in web_search.USER_AGENT
        assert "Chrome" not in web_search.USER_AGENT
        assert web_search.USER_AGENT.startswith("magoneai-web-search/")
        assert "github.com/magurelabs/magoneai-mcp" in web_search.USER_AGENT

    @pytest.mark.asyncio
    async def test_the_text_path_sends_it(self):
        sent = {}

        class FakeClient:
            async def get(self, url, headers=None, **kwargs):
                sent.update(headers or {})
                return httpx.Response(404)

        await web_search._fetch_via_trafilatura(FakeClient(), "https://example.com/", 8000)

        assert sent["User-Agent"] == web_search.USER_AGENT


class TestACrawlAsksRobotsTxt:
    @pytest.mark.asyncio
    async def test_a_disallowed_page_is_not_crawled(self, site, crawl):
        site["handler"] = lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

        result = await scrape_url(url="https://example.com/private/page", return_full=True)

        assert "robots.txt" in result.get("error", "")
        assert crawl == []
        assert str(site["requests"][0].url) == "https://example.com/robots.txt"
        assert site["requests"][0].headers["User-Agent"] == web_search.USER_AGENT

    @pytest.mark.asyncio
    async def test_an_allowed_page_is_crawled(self, site, crawl):
        site["handler"] = lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

        result = await scrape_url(url="https://example.com/public", return_full=True)

        assert "error" not in result
        assert crawl == ["https://example.com/public"]

    @pytest.mark.asyncio
    async def test_a_rule_for_this_agent_by_name_is_honoured(self, site, crawl):
        site["handler"] = lambda r: httpx.Response(
            200, text="User-agent: magoneai-web-search\nDisallow: /\n"
        )

        result = await scrape_url(url="https://example.com/anything", return_full=True)

        assert "error" in result
        assert crawl == []

    @pytest.mark.asyncio
    async def test_no_robots_txt_means_no_rules(self, site, crawl):
        site["handler"] = lambda r: httpx.Response(404)

        result = await scrape_url(url="https://example.com/a", return_full=True)

        assert "error" not in result
        assert crawl == ["https://example.com/a"]

    @pytest.mark.asyncio
    async def test_a_server_error_means_assume_disallowed(self, site, crawl):
        """RFC 9309 section 2.3.1.4: unreachable robots.txt is a complete disallow."""
        site["handler"] = lambda r: httpx.Response(503)

        result = await scrape_url(url="https://example.com/a", return_full=True)

        assert "error" in result
        assert crawl == []

    @pytest.mark.asyncio
    async def test_one_robots_fetch_serves_a_whole_crawl(self, site, crawl):
        site["handler"] = lambda r: httpx.Response(200, text="User-agent: *\nAllow: /\n")

        for path in ("/a", "/b", "/c"):
            await scrape_url(url=f"https://example.com{path}", return_full=True)

        assert len(site["requests"]) == 1
        assert len(crawl) == 3

    @pytest.mark.asyncio
    async def test_a_robots_redirect_to_an_internal_address_is_not_followed(self, site, crawl):
        """robots.txt is fetched from a host the model chose, so it gets the
        same SSRF guard as the page: a redirect hop into the cluster is refused,
        and the refusal reads as disallow rather than as no rules."""
        site["handler"] = lambda r: httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )

        result = await scrape_url(url="https://example.com/a", return_full=True)

        assert "error" in result
        assert crawl == []
        assert [str(r.url) for r in site["requests"]] == ["https://example.com/robots.txt"]
