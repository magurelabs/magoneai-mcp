"""`scrape_url` takes a URL from the model and must not fetch inside the boundary.

`net.py` opens by saying it exists because v1 had three SSRF checks that
disagreed, and that it is "the single implementation". Measured against the
connectors, that sentence was aspirational: `validate_url` had **zero** call
sites outside its own package. Every connector that fetches still carried its
own check, and `web_search` carried none at all.

So the tool an agent is most likely to point at an arbitrary URL was the one
with no check:

    scrape_url(url="http://169.254.169.254/latest/meta-data/iam/...")

went straight to `httpx.get(url, follow_redirects=True)`. Cloud instance
metadata, returned to the model as page text. A prompt injection in any page
the agent had already read was enough to aim it.

`follow_redirects=True` matters twice over: even a validated public URL can
302 to a link-local address, so validating once at the top is necessary but
not sufficient. The fix disables transparent redirects on the text path and
re-validates each hop, which is what `cloudflare_r2` already did.

These tests are the reason the check cannot quietly go away again.
"""

import importlib
import json

import pytest

from magone_mcp_core import SSRFBlocked

# These import real connectors, so they need the connector dependencies.
# `pip install -e ".[dev]"` deliberately installs only the framework's, so an
# outside contributor can run most of the suite with almost nothing. The
# Connector tests job installs `.[dev,all]` and runs exactly these.
pytestmark = pytest.mark.requires_connectors

# The package re-exports the ConnectorServer *instance* as `server`, so
# `from connectors.web_search import server` would hand back the instance
# rather than the module. Import the module by path and take the registered
# tool off the manager, which is also the function the wire actually reaches.
# `importorskip`, not `import_module`: pytest IMPORTS a test module during
# collection, so `pytestmark` deselects the tests but cannot stop the import.
# The Framework tests job installs only the framework's dependencies, so a
# plain import here fails collection outright and takes the whole job with it.
# Imported at MODULE level, so this must survive collection in an install
# that has only the framework's dependencies. `pytestmark` deselects the tests
# but pytest still imports the module to find them, and `importorskip`
# re-raises an ImportError raised from a transitive dependency rather than
# from the named module. An explicit skip is the version-independent form.
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
    """Call the tool as the wire does and return the handler's payload.

    `fn` is the guarded wrapper, so it injects `credentials` itself and wraps
    the return value in a `ToolResult`. Unwrapping here keeps every assertion
    below about the tool's answer rather than about MCP's envelope.
    """
    result = await _scrape_url(**kwargs)
    # A handler that returns an error dict comes back wrapped as a ToolResult;
    # a success comes back as the plain dict. Accept both rather than asserting
    # on which shape, since the shape is the framework's business.
    if isinstance(result, dict):
        return result
    return json.loads(result.content[0].text)


@pytest.fixture
def offline_dns(monkeypatch):
    """Resolve every hostname to one public address.

    These tests must not depend on the network, and this sandbox has no DNS at
    all: without pinning, `validate_url` fails with "cannot resolve hostname"
    and a test asserting a URL is *allowed* passes for the wrong reason. IP
    literals short-circuit before resolution, so the blocked cases never reach
    this.
    """
    from magone_mcp_core import net

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake_getaddrinfo)


BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",  # AWS/Azure/GCP metadata
    "http://[::ffff:169.254.169.254]/latest/meta-data/",  # same, IPv4-mapped
    "http://127.0.0.1:8000/api/v1/organizations",  # the platform itself
    "http://[::1]:8000/api/v1/organizations",
    "http://10.0.0.1/",  # cluster-internal
    "http://192.168.1.1/",
    "http://100.64.0.1/",  # carrier-grade NAT
    "http://[64:ff9b::a9fe:a9fe]/",  # NAT64-wrapped metadata
    "http://0.0.0.0:8000/",
]


class TestTheMetadataServiceIsUnreachable:
    @pytest.mark.parametrize("url", BLOCKED)
    @pytest.mark.asyncio
    async def test_a_blocked_url_is_refused_before_any_request(self, url):
        """Refused by address, not by hostname string matching: a blocklist of
        names is bypassed by an IP literal, which is what these all are."""
        result = await scrape_url(url=url)

        assert "error" in result, f"{url} was fetched"
        assert "blocked" in result["error"].lower()

    @pytest.mark.parametrize("url", BLOCKED)
    @pytest.mark.asyncio
    async def test_the_full_markdown_path_is_refused_too(self, url):
        """`return_full=True` runs a headless browser instead of httpx. Its
        schema suggests callers pin DNS with --host-resolver-rules, but that is
        the caller's choice and the model does not make it. Both branches have
        to be closed or only the one that was tested is."""
        result = await scrape_url(url=url, return_full=True)

        assert "error" in result, f"{url} was fetched via crawl4ai"
        assert "blocked" in result["error"].lower()


class TestAPublicUrlStillWorks:
    @pytest.mark.asyncio
    async def test_a_public_host_is_not_refused_by_the_guard(self, monkeypatch, offline_dns):
        """A guard that blocks everything passes the tests above and breaks the
        tool. This asserts the check lets a public address through to the
        fetch, without making a network call."""
        called = {}

        async def fake_fetch(client, url, max_chars=8000):
            called["url"] = url
            return "some page text that is long enough to clear the minimum"

        monkeypatch.setattr(web_search, "_fetch_page_content", fake_fetch)

        result = await scrape_url(url="https://example.com/a")

        assert called["url"] == "https://example.com/a"
        assert "error" not in result


class TestRedirectsCannotEscape:
    def test_the_text_path_does_not_follow_redirects_transparently(self):
        """A public URL that 302s to 169.254.169.254 defeats a check that only
        runs on the URL the model supplied. httpx must not follow the hop by
        itself; the fetch helper re-validates and follows deliberately."""
        import inspect

        source = inspect.getsource(web_search._fetch_via_trafilatura)

        assert "follow_redirects=True" not in source, (
            "transparent redirects are back; a validated public URL can hop to "
            "an internal address and the guard never sees it"
        )

    @pytest.mark.asyncio
    async def test_a_redirect_to_an_internal_address_is_refused(self, monkeypatch):
        """End to end through the helper: hop one is public, hop two is
        metadata, and the second hop must be refused rather than followed."""
        hops = iter(
            [
                ("https://example.com/start", 302, "http://169.254.169.254/latest/"),
            ]
        )
        fetched = []

        class FakeResponse:
            def __init__(self, status, location):
                self.status_code = status
                self.headers = {"location": location} if location else {}
                self.text = ""

        class TransparentRedirectsAreBack(BaseException):
            """Deliberately NOT an Exception.

            `_fetch_via_trafilatura` ends in `except Exception: return ""`, so a
            plain assert inside the fake client would be swallowed and this test
            would pass whether or not the fix were present. Raising outside the
            Exception hierarchy is what makes the failure reach pytest.
            """

        class FakeClient:
            async def get(self, url, **kwargs):
                fetched.append(url)
                if kwargs.get("follow_redirects") is True:
                    raise TransparentRedirectsAreBack(url)
                _, status, location = next(hops)
                return FakeResponse(status, location)

        content = await web_search._fetch_via_trafilatura(
            FakeClient(), "https://example.com/start", 8000
        )

        assert content == ""
        assert fetched == ["https://example.com/start"], (
            "the internal address was requested: " + repr(fetched)
        )


class TestTheSharedCheckIsTheOneUsed:
    def test_web_search_uses_the_framework_check(self):
        """The point of `net.py` is that there is one implementation. A
        connector that grows its own copy reintroduces the exact defect that
        module's docstring describes."""
        import inspect

        source = inspect.getsource(web_search)

        assert "validate_url" in source, "web_search stopped using the shared SSRF check"
        assert "_PRIVATE_NETWORKS" not in source, (
            "web_search has grown a private copy of the blocklist"
        )

    def test_the_guard_raises_the_frameworks_error(self):
        """So a caller can catch one exception type across connectors."""
        from magone_mcp_core import validate_url

        with pytest.raises(SSRFBlocked):
            validate_url("http://169.254.169.254/")
