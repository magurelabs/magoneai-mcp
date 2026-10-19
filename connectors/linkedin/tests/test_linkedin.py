"""The LinkedIn connector, through a real MCP client, with LinkedIn's HTTP API mocked.

Every call goes `Client -> ConnectorServer -> handler -> httpx`, and httpx is
answered by a local `MockTransport`, so what is under test is the wire
contract, the request each tool sends, and how a vendor answer comes back.
Nothing leaves the process.
"""

import importlib
import json
from collections.abc import Callable

import httpx
import pytest
from mcp import Client

module = importlib.import_module("connectors.linkedin.server")
client_module = importlib.import_module("connectors.linkedin._client")

TOKEN_HEADERS = {"x-linkedin-access-token": "tok-direct"}

# A public literal address: `validate_url` judges it without a DNS lookup.
PUBLIC_IMAGE = "https://93.184.216.34/logo.png"

Handler = Callable[[httpx.Request], httpx.Response]


def install(monkeypatch, handler: Handler, headers: dict[str, str] | None = None):
    """Route every httpx call to `handler`, and send `headers` on every MCP call."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recording)
        return real(*args, **kwargs)

    client_module._token_cache.clear()
    monkeypatch.delenv("LINKEDIN_ORG_ID", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    sent = dict(TOKEN_HEADERS if headers is None else headers)
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(sent))
    return requests


async def call(name: str, arguments: dict | None = None):
    async with Client(module.server.mcp) as mcp_client:
        return await mcp_client.call_tool(name, arguments or {})


def payload(result) -> dict:
    return json.loads(result.content[0].text)


def api(request: httpx.Request) -> str:
    """The request path, decoded, for matching."""
    return request.url.path


# -- the catalogue -------------------------------------------------------------


async def test_the_eleven_v1_tools_are_offered_with_their_annotations():
    async with Client(module.server.mcp) as mcp_client:
        tools = {t.name: t for t in (await mcp_client.list_tools()).tools}

    assert sorted(tools) == [
        "create_post",
        "delete_post",
        "engagement_report",
        "get_page_statistics",
        "get_post",
        "get_post_engagement",
        "get_profile",
        "list_organizations",
        "list_post_comments",
        "list_post_reactions",
        "list_posts",
    ]
    for name in ("create_post", "delete_post"):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is True
    for name in set(tools) - {"create_post", "delete_post"}:
        assert tools[name].annotations.read_only_hint is True, name
        assert tools[name].annotations.destructive_hint is False, name


async def test_every_tool_declares_a_group_and_only_org_tools_ask_for_admin():
    """A connection should never have to request rw_organization_admin to post."""
    async with Client(module.server.mcp) as mcp_client:
        tools = (await mcp_client.list_tools()).tools

    admin = set()
    for tool in tools:
        meta = tool.meta or {}
        assert meta.get("io.magone/permission_group", "default") != "default", tool.name
        assert meta.get("io.magone/required_scopes"), tool.name
        if "rw_organization_admin" in meta["io.magone/required_scopes"]:
            admin.add(tool.name)
    assert admin == {"list_organizations", "get_page_statistics", "engagement_report"}


# -- credentials -----------------------------------------------------------------


async def test_no_credentials_is_a_clear_error_and_no_request(monkeypatch):
    requests = install(monkeypatch, lambda r: httpx.Response(500), headers={})

    result = await call("get_profile")

    assert result.is_error is True
    assert "X-LinkedIn-Access-Token" in payload(result)["error"]
    assert requests == []


async def test_a_direct_token_is_sent_with_the_versioned_api_headers(monkeypatch):
    requests = install(
        monkeypatch,
        lambda r: httpx.Response(200, json={"sub": "abc", "name": "Ada", "email": "a@x.io"}),
    )

    result = await call("get_profile")

    assert result.is_error is False
    assert result.structured_content == {
        "name": "Ada",
        "email": "a@x.io",
        "person_urn": "urn:li:person:abc",
    }
    sent = requests[0]
    assert sent.url.host == "api.linkedin.com" and api(sent) == "/v2/userinfo"
    assert sent.headers["authorization"] == "Bearer tok-direct"
    assert sent.headers["linkedin-version"] == client_module.LINKEDIN_VERSION
    assert sent.headers["x-restli-protocol-version"] == "2.0.0"


async def test_a_missing_profile_field_is_omitted_not_null(monkeypatch):
    """The output schema types these as strings; null would fail a strict client."""
    install(monkeypatch, lambda r: httpx.Response(200, json={"sub": "abc"}))

    result = await call("get_profile")

    assert result.is_error is False
    assert result.structured_content == {"person_urn": "urn:li:person:abc"}


async def test_a_refresh_token_is_exchanged_once_and_reused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.linkedin.com":
            return httpx.Response(200, json={"access_token": "minted", "expires_in": 3600})
        return httpx.Response(200, json={"sub": "abc"})

    requests = install(
        monkeypatch,
        handler,
        headers={
            "x-linkedin-refresh-token": "refresh-1",
            "x-linkedin-client-id": "cid",
            "x-linkedin-client-secret": "secret",
        },
    )

    await call("get_profile")
    await call("get_profile")

    token_calls = [r for r in requests if r.url.host == "www.linkedin.com"]
    assert len(token_calls) == 1
    assert b"grant_type=refresh_token" in token_calls[0].content
    api_calls = [r for r in requests if r.url.host == "api.linkedin.com"]
    assert [r.headers["authorization"] for r in api_calls] == ["Bearer minted"] * 2


async def test_a_403_names_the_missing_linkedin_product(monkeypatch):
    install(monkeypatch, lambda r: httpx.Response(403, json={"message": "denied"}))

    result = await call("list_posts", {"author": "123"})

    assert result.is_error is True
    assert "Community Management API" in payload(result)["error"]


# -- posts -----------------------------------------------------------------------


async def test_create_post_as_an_organization_escapes_text(monkeypatch):
    requests = install(
        monkeypatch,
        lambda r: httpx.Response(201, headers={"x-restli-id": "urn:li:share:99"}),
    )

    result = await call("create_post", {"text": "Hello (world)", "author": "123"})

    assert result.is_error is False
    assert result.structured_content["post"] == {
        "id": "urn:li:share:99",
        "author": "urn:li:organization:123",
        "state": "PUBLISHED",
        "url": "https://www.linkedin.com/feed/update/urn:li:share:99/",
    }
    sent = requests[-1]
    assert sent.method == "POST" and api(sent) == "/rest/posts"
    body = json.loads(sent.content)
    assert body["author"] == "urn:li:organization:123"
    assert body["commentary"] == "Hello \\(world\\)"
    assert body["lifecycleState"] == "PUBLISHED"
    assert body["visibility"] == "PUBLIC"


async def test_create_post_as_me_resolves_the_member_first(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if api(request) == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "m1"})
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:5"})

    requests = install(monkeypatch, handler)

    result = await call("create_post", {"text": "hi", "author": "me", "draft": True})

    assert result.structured_content["post"]["author"] == "urn:li:person:m1"
    assert json.loads(requests[-1].content)["lifecycleState"] == "DRAFT"


async def test_create_post_attaches_a_link_without_fetching_it(monkeypatch):
    requests = install(
        monkeypatch, lambda r: httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
    )

    await call(
        "create_post",
        {"text": "read", "author": "1", "link": "https://example.com", "link_title": "Ex"},
    )

    assert [r.url.host for r in requests] == ["api.linkedin.com"]
    body = json.loads(requests[-1].content)
    assert body["content"]["article"] == {"source": "https://example.com", "title": "Ex"}


async def test_create_post_refuses_link_and_image_together(monkeypatch):
    requests = install(monkeypatch, lambda r: httpx.Response(500))

    result = await call(
        "create_post", {"text": "x", "link": "https://a.com", "image_url": PUBLIC_IMAGE}
    )

    assert result.is_error is True
    assert requests == []


async def test_create_post_uploads_an_image_then_posts_it(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(200, content=b"\x89PNG-bytes")
        if api(request) == "/rest/images":
            return httpx.Response(
                200,
                json={
                    "value": {
                        "uploadUrl": "https://www.linkedin.com/dms-uploads/abc",
                        "image": "urn:li:image:42",
                    }
                },
            )
        if api(request) == "/dms-uploads/abc":
            return httpx.Response(201)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7"})

    requests = install(monkeypatch, handler)

    result = await call(
        "create_post",
        {"text": "pic", "author": "1", "image_url": PUBLIC_IMAGE, "image_alt_text": "logo"},
    )

    assert result.is_error is False, result.content[0].text
    upload = next(r for r in requests if api(r) == "/dms-uploads/abc")
    assert upload.method == "PUT" and upload.content == b"\x89PNG-bytes"
    body = json.loads(requests[-1].content)
    assert body["content"]["media"] == {"id": "urn:li:image:42", "altText": "logo"}


@pytest.mark.parametrize(
    "image_url",
    [
        "http://93.184.216.34/logo.png",  # not https
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/logo.png",  # RFC 1918
        "https://[::ffff:169.254.169.254]/x",  # metadata, IPv4-mapped
        "https://intranet.internal/logo.png",  # internal name
    ],
)
async def test_create_post_refuses_an_internal_image_url(monkeypatch, image_url):
    requests = install(monkeypatch, lambda r: httpx.Response(200, content=b"secret"))

    result = await call("create_post", {"text": "x", "author": "1", "image_url": image_url})

    assert result.is_error is True
    assert "Failed to download image" in payload(result)["error"]
    assert requests == []


async def test_a_redirect_into_an_internal_address_is_not_followed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "https://169.254.169.254/"})
        return httpx.Response(200, content=b"metadata")

    requests = install(monkeypatch, handler)

    result = await call("create_post", {"text": "x", "author": "1", "image_url": PUBLIC_IMAGE})

    assert result.is_error is True
    assert [r.url.host for r in requests] == ["93.184.216.34"]


async def test_get_post_explains_an_activity_urn(monkeypatch):
    install(
        monkeypatch,
        lambda r: httpx.Response(400, json={"message": "Validations failed on the UGC entity."}),
    )

    result = await call(
        "get_post", {"post": "https://www.linkedin.com/feed/update/activity-7123456789012345678-ab"}
    )

    error = payload(result)["error"]
    assert result.is_error is True
    assert "activity URN" in error and "list_posts" in error


async def test_get_post_returns_the_flattened_post(monkeypatch):
    requests = install(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "id": "urn:li:share:9",
                "author": "urn:li:organization:1",
                "commentary": "hello",
                "lifecycleState": "PUBLISHED",
                "publishedAt": 1700000000000,
            },
        ),
    )

    result = await call("get_post", {"post": "9"})

    post = result.structured_content["post"]
    assert post["id"] == "urn:li:share:9" and post["text"] == "hello"
    assert post["published_at"].startswith("2023-11-14")
    assert requests[0].url.raw_path.decode().endswith("/rest/posts/urn%3Ali%3Ashare%3A9")


async def test_list_posts_pages_and_sends_no_sort(monkeypatch):
    requests = install(
        monkeypatch,
        lambda r: httpx.Response(200, json={"elements": [{"id": "urn:li:share:1"}]}),
    )

    result = await call("list_posts", {"author": "123", "limit": 5})

    assert result.structured_content["count"] == 1
    params = requests[0].url.params
    assert params["q"] == "author" and params["author"] == "urn:li:organization:123"
    assert params["count"] == "5" and "sortBy" not in params


async def test_delete_post_sends_a_delete(monkeypatch):
    requests = install(monkeypatch, lambda r: httpx.Response(204))

    result = await call("delete_post", {"post": "urn:li:share:3"})

    assert result.structured_content == {"deleted": True, "id": "urn:li:share:3"}
    assert requests[0].method == "DELETE"


# -- engagement ------------------------------------------------------------------


async def test_get_post_engagement_sums_reactions(monkeypatch):
    install(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "reactionSummaries": {"LIKE": {"count": 5}, "PRAISE": {"count": 2}},
                "commentSummary": {"count": 3},
            },
        ),
    )

    result = await call("get_post_engagement", {"post": "urn:li:share:1"})

    data = result.structured_content
    assert data["total_reactions"] == 7 and data["comments"] == 3
    assert data["reactions_by_type"] == {"LIKE": 5, "PRAISE": 2}


async def test_list_post_reactions_encodes_the_urn_in_the_path(monkeypatch):
    requests = install(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"elements": [{"actor": "urn:li:person:a", "reactionType": "LIKE"}]}
        ),
    )

    result = await call("list_post_reactions", {"post": "urn:li:share:1"})

    assert result.structured_content["reactions"][0]["actor"] == "urn:li:person:a"
    assert "(entity:urn%3Ali%3Ashare%3A1)" in requests[0].url.raw_path.decode()


async def test_list_post_comments(monkeypatch):
    install(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "commentUrn": "urn:li:comment:1",
                        "actor": "urn:li:person:b",
                        "message": {"text": "nice"},
                        "likesSummary": {"totalLikes": 2},
                    }
                ]
            },
        ),
    )

    result = await call("list_post_comments", {"post": "urn:li:share:1"})

    comment = result.structured_content["comments"][0]
    assert comment["text"] == "nice" and comment["likes"] == 2


# -- analytics -------------------------------------------------------------------


async def test_list_organizations_resolves_names(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if api(request) == "/rest/organizationAcls":
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"organization": "urn:li:organization:1", "role": "ADMINISTRATOR"},
                        {"organization": "urn:li:organization:2", "role": "ADMINISTRATOR"},
                    ]
                },
            )
        if api(request) == "/rest/organizations/1":
            return httpx.Response(200, json={"localizedName": "One"})
        return httpx.Response(403)

    install(monkeypatch, handler)

    result = await call("list_organizations")

    orgs = {o["id"]: o["name"] for o in result.structured_content["organizations"]}
    assert orgs == {"1": "One", "2": None}


async def test_get_page_statistics_includes_followers(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if api(request) == "/rest/organizationalEntityShareStatistics":
            return httpx.Response(
                200,
                json={"elements": [{"totalShareStatistics": {"impressionCount": 100}}]},
            )
        return httpx.Response(200, json={"firstDegreeSize": 42})

    install(monkeypatch, handler)

    result = await call("get_page_statistics", {"organization": "5"})

    data = result.structured_content
    assert data["statistics"]["impressions"] == 100
    assert data["follower_count"] == 42


async def test_engagement_report_is_organization_only(monkeypatch):
    requests = install(monkeypatch, lambda r: httpx.Response(500))

    result = await call("engagement_report", {"organization": "urn:li:person:abc"})

    assert result.is_error is True
    assert "organization" in payload(result)["error"]
    assert requests == []


async def test_engagement_report_aggregates_and_ranks(monkeypatch):
    s1, s2 = "urn%3Ali%3Ashare%3A1", "urn%3Ali%3Ashare%3A2"
    reactions = {
        s1: [
            {"actor": "urn:li:person:alice", "reactionType": "LIKE"},
            {"actor": "urn:li:person:bob", "reactionType": "LIKE"},
        ],
        s2: [{"actor": "urn:li:person:alice", "reactionType": "PRAISE"}],
    }
    comments = {
        s1: [{"actor": "urn:li:person:alice", "message": {"text": "great"}}],
        # The organization replying on its own post is not an engager.
        s2: [{"actor": "urn:li:organization:123", "message": {"text": "thanks"}}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        if request.url.path == "/rest/posts":
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"id": "urn:li:share:1", "commentary": "p1"},
                        {"id": "urn:li:share:2", "commentary": "p2"},
                    ]
                },
            )
        for key, items in reactions.items():
            if raw.startswith("/rest/reactions/") and key in raw:
                return httpx.Response(200, json={"elements": items})
        for key, items in comments.items():
            if "/comments" in raw and key in raw:
                return httpx.Response(200, json={"elements": items})
        return httpx.Response(200, json={})

    install(monkeypatch, handler)

    result = await call("engagement_report", {"organization": "123"})

    report = result.structured_content
    assert report["posts_analyzed"] == 2
    assert report["totals"] == {"reactions": 3, "comments": 2, "unique_engagers": 2}
    top = report["top_engagers"][0]
    assert (top["actor"], top["reactions"], top["comments"], top["posts_engaged"]) == (
        "urn:li:person:alice",
        2,
        1,
        2,
    )
    assert [e["actor"] for e in report["repeat_engagers"]] == ["urn:li:person:alice"]


async def test_engagement_report_surfaces_a_permission_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            return httpx.Response(200, json={"elements": [{"id": "urn:li:share:1"}]})
        return httpx.Response(403)

    install(monkeypatch, handler)

    result = await call("engagement_report", {"organization": "123"})

    assert result.is_error is True
    assert "403" in payload(result)["error"]


# -- helpers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("urn:li:share:7123", "urn:li:share:7123"),
        ("urn:li:ugcPost:42", "urn:li:ugcPost:42"),
        ("7123456789", "urn:li:share:7123456789"),
        (
            "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/",
            "urn:li:activity:7123456789",
        ),
        (
            "https://www.linkedin.com/posts/acme_activity-7123456789012-Ab1c",
            "urn:li:activity:7123456789012",
        ),
    ],
)
def test_normalize_post_urn(value, expected):
    assert client_module.normalize_post_urn(value) == (expected, None)


@pytest.mark.parametrize("value", ["", "not-a-post", "https://example.com/xyz"])
def test_normalize_post_urn_rejects(value):
    urn, error = client_module.normalize_post_urn(value)
    assert urn is None and error


def test_escape_text():
    assert client_module.escape_text("a(b)c") == "a\\(b\\)c"
    assert client_module.escape_text("x{y}_z*") == "x\\{y\\}\\_z\\*"
    assert client_module.escape_text("a\\(") == "a\\\\\\("
    assert client_module.escape_text("keep #hashtags") == "keep #hashtags"


async def test_the_default_organization_comes_from_the_deployment(monkeypatch):
    monkeypatch.setenv("LINKEDIN_ORG_ID", "999")
    assert await client_module.resolve_author({}, None) == ("urn:li:organization:999", None)
    urn, error = await client_module.resolve_author({}, "acme corp")
    assert urn is None and "not a valid author" in error
