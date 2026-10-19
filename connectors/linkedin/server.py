"""The linkedin connector.

Ported from the earlier (v1) LinkedIn server: post as a member or a company
page, read a post's reactions and comments, and report on who engages with an
organization's content most, through LinkedIn's versioned REST API.

11 tools, 9 of them proposed read-only. Each annotation was traced to the HTTP
method its handler sends: `create_post` (POST /rest/posts, and an image upload)
and `delete_post` (DELETE /rest/posts/{urn}) are writes and marked destructive,
since a published post is visible the moment it exists; the rest only GET.

Every input schema and tool name is carried across from v1 via
`declare_schema`, so a saved agent configuration keeps working. The one
deliberate difference is punctuation: v1 descriptions used long dashes, which
this repository does not, so those few characters were rewritten. No property,
type, default, enum or `required` entry changed.

Credentials arrive per call, in the headers v1 declared:
`X-LinkedIn-Access-Token` (a direct token, which wins), or
`X-LinkedIn-Refresh-Token` with `X-LinkedIn-Client-Id` and
`X-LinkedIn-Client-Secret` for apps with programmatic refresh enabled.

Engagement and analytics tools need the Community Management API product
approved on the caller's LinkedIn app; without it LinkedIn answers 403 and the
tool says which product is missing.
"""

import asyncio
import ipaddress
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import (
    ConnectorServer,
    InvalidURL,
    SSRFBlocked,
    declare_output_schema,
    declare_schema,
    is_blocked_address,
    validate_url,
)

from . import _client as client

SERVER_NAME = "linkedin"
SERVER_VERSION = "2.0.0"

logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description=(
        "Post to LinkedIn (member or company page), track post engagement "
        "(reactions, comments), and build reports on who engages with your "
        "content most"
    ),
)

CREDENTIAL_HEADERS = [
    "X-LinkedIn-Access-Token",
    "X-LinkedIn-Refresh-Token",
    "X-LinkedIn-Client-Id",
    "X-LinkedIn-Client-Secret",
]

# engagement_report caps: how many reactions and comments to pull per post, and
# how many posts to fan out over. Keeps one report to a bounded number of calls.
_MAX_REPORT_POSTS = 20
_PER_POST_ACTORS = 500
_FANOUT_CONCURRENCY = 4

# LinkedIn's Images API rejects anything larger. Enforced while streaming, so a
# hostile URL cannot exhaust memory before the check.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_REDIRECTS = 5
_INTERNAL_SUFFIXES = (".local", ".internal", ".localhost")


# -- image download (create_post with image_url) -----------------------------


class RemoteFetchError(Exception):
    """A caller-supplied URL failed validation or could not be fetched safely."""


def _check_url(url: str) -> None:
    """https only, no internal hostname, and every resolved address public.

    The address decision is the framework's `validate_url`, the one SSRF check
    in this repository. The hostname suffixes are refused before any lookup,
    because a name that only resolves inside a network says enough on its own.
    """
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise RemoteFetchError("Internal hostnames are not allowed")
    try:
        validate_url(url, allowed_schemes={"https"})
    except (InvalidURL, SSRFBlocked) as exc:
        raise RemoteFetchError(str(exc)) from exc


def _reject_private_peer(response: httpx.Response) -> None:
    """Re-check the address actually connected to, closing the DNS-rebinding window."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return
    server_addr = stream.get_extra_info("server_addr")
    if not server_addr:
        return
    try:
        ip = ipaddress.ip_address(server_addr[0])
    except (ValueError, IndexError):
        return
    if is_blocked_address(ip):
        raise RemoteFetchError("URL connected to a private or reserved IP")


async def fetch_remote_file(url: str, *, max_bytes: int, timeout: float = 60.0) -> bytes:
    """Fetch `url` over https with SSRF guards on every hop and a hard size cap.

    Redirects are followed by hand so each hop is validated. A one-shot check
    before an ordinary redirect-following GET would not be a guard: a public
    host can answer 302 to the cloud metadata address.
    """
    current = (url or "").strip()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
            for _ in range(_MAX_REDIRECTS + 1):
                _check_url(current)
                async with http.stream("GET", current) as response:
                    _reject_private_peer(response)
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RemoteFetchError("Redirect without a Location header")
                        current = str(response.url.join(location))
                        continue
                    response.raise_for_status()
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            mb = max_bytes // (1024 * 1024)
                            raise RemoteFetchError(f"Remote file exceeds {mb}MB cap.")
                    return bytes(buf)
    except RemoteFetchError:
        raise
    except httpx.HTTPError as exc:
        raise RemoteFetchError(f"Failed to fetch URL: {type(exc).__name__}") from exc
    raise RemoteFetchError("Too many redirects")


async def upload_image(
    credentials: dict[str, str], owner_urn: str, image_url: str
) -> tuple[str | None, str | None]:
    """Download an image and upload it to LinkedIn. Returns (image_urn, error)."""
    try:
        content = await fetch_remote_file(image_url, max_bytes=_MAX_IMAGE_BYTES)
    except RemoteFetchError as exc:
        return None, f"Failed to download image from image_url: {exc}"

    init, error = await client.rest(
        credentials,
        "POST",
        "/rest/images",
        params={"action": "initializeUpload"},
        json_data={"initializeUploadRequest": {"owner": owner_urn}},
    )
    if error:
        return None, error
    value = init.get("value") or {}
    upload_url, image_urn = value.get("uploadUrl"), value.get("image")
    if not upload_url or not image_urn:
        return None, "LinkedIn image upload initialization returned no uploadUrl/image URN."

    # The upload URL is pre-authorized for this asset; the Images API still
    # requires the member's bearer token on the PUT.
    token, error = await client.get_access_token(credentials)
    if error:
        return None, error
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            put = await http.put(
                upload_url, content=content, headers={"Authorization": f"Bearer {token}"}
            )
            put.raise_for_status()
    except httpx.HTTPError as exc:
        return None, f"Image upload to LinkedIn failed: {type(exc).__name__}"
    return image_urn, None


# -- posts ---------------------------------------------------------------------


@server.tool(
    name="create_post",
    description=(
        "Publish a LinkedIn post as the authenticated member or an organization "
        "(company page). Supports plain text, an article link preview, or one image "
        "fetched from a URL. Returns the new post's URN and public URL."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="manage_linkedin_posts",
    required_scopes=["w_member_social", "w_organization_social", "openid", "profile", "email"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Post text (commentary). Hashtags like #ai work as-is.",
            },
            "author": {
                "type": "string",
                "description": (
                    "Who posts: 'me' (the member), a numeric organization id, or a full "
                    "URN. Default: LINKEDIN_ORG_ID env if set, else the member."
                ),
            },
            "link": {
                "type": "string",
                "description": "Optional article/website URL to attach as a link preview",
            },
            "link_title": {
                "type": "string",
                "description": "Optional title for the attached link",
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Optional public https image URL to upload and attach (max 20MB). "
                    "Must resolve to a public address; internal hosts are refused."
                ),
            },
            "image_alt_text": {
                "type": "string",
                "description": "Alt text for the attached image",
            },
            "visibility": {
                "type": "string",
                "enum": ["PUBLIC", "CONNECTIONS", "LOGGED_IN"],
                "description": "Post visibility (default PUBLIC)",
                "default": "PUBLIC",
            },
            "draft": {
                "type": "boolean",
                "description": "Create as DRAFT instead of publishing (default false)",
                "default": False,
            },
        },
        "required": ["text"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "post": {
                "type": "object",
                "description": "The created post: id (URN), author, url, state",
            },
        },
    }
)
async def create_post(
    credentials: dict[str, str],
    text: str,
    author: str | None = None,
    link: str | None = None,
    link_title: str | None = None,
    image_url: str | None = None,
    image_alt_text: str | None = None,
    visibility: str = "PUBLIC",
    draft: bool = False,
) -> dict[str, Any]:
    """Publish a post. `link` is passed to LinkedIn as-is and never fetched here."""
    if not text or not text.strip():
        return {"error": "text is required"}
    if link and image_url:
        return {"error": "Attach either a link or an image_url, not both."}
    if visibility not in ("PUBLIC", "CONNECTIONS", "LOGGED_IN"):
        return {"error": "visibility must be PUBLIC, CONNECTIONS, or LOGGED_IN"}

    author_urn, error = await client.resolve_author(credentials, author)
    if error:
        return {"error": error}

    body: dict[str, Any] = {
        "author": author_urn,
        "commentary": client.escape_text(text),
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "DRAFT" if draft else "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if link:
        article: dict[str, Any] = {"source": link}
        if link_title:
            article["title"] = link_title
        body["content"] = {"article": article}
    elif image_url:
        image_urn, error = await upload_image(credentials, author_urn, image_url)
        if error:
            return {"error": error}
        media: dict[str, Any] = {"id": image_urn}
        if image_alt_text:
            media["altText"] = image_alt_text
        body["content"] = {"media": media}

    data, error = await client.rest(credentials, "POST", "/rest/posts", json_data=body)
    if error:
        return {"error": error}
    urn = data.get("id", "")
    return {
        "post": {
            "id": urn,
            "author": author_urn,
            "state": "DRAFT" if draft else "PUBLISHED",
            "url": client.post_url(urn) if urn else None,
        }
    }


@server.tool(
    name="get_post",
    description=(
        "Fetch a single LinkedIn post by URN, share URL, or numeric share id. "
        "Returns its text, author, visibility, and publish time. Needs the "
        "Community Management API product; w_member_social alone cannot read."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_posts",
    required_scopes=["r_organization_social"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "post": {
                "type": "string",
                "description": "Post URN (urn:li:share:123), share URL, or numeric id",
            },
        },
        "required": ["post"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "post": {"type": "object", "description": "id, author, text, url, published_at"},
        },
    }
)
async def get_post(credentials: dict[str, str], post: str) -> dict[str, Any]:
    """Fetch one post."""
    urn, error = client.normalize_post_urn(post)
    if error:
        return {"error": error}
    data, error = await client.rest(credentials, "GET", f"/rest/posts/{client.encode_urn(urn)}")
    if error:
        return {"error": client.activity_urn_hint(urn, error)}
    return {"post": client.post_to_dict(data)}


@server.tool(
    name="list_posts",
    description=(
        "List recent posts by an author (organization page or the member). "
        "Ordering is LinkedIn's default and not guaranteed. Use this to find post "
        "URNs for engagement tools. "
        "Needs the Community Management API product: w_member_social alone is "
        "write-only and this returns 403."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_posts",
    required_scopes=["r_organization_social", "openid", "profile", "email"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "author": {
                "type": "string",
                "description": (
                    "'me', a numeric organization id, or a full URN. "
                    "Default: LINKEDIN_ORG_ID env, else the member."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max posts to return (default 10, max 50)",
                "default": 10,
            },
        },
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "posts": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
    }
)
async def list_posts(
    credentials: dict[str, str], author: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List an author's recent posts."""
    limit = max(1, min(limit, 50))
    author_urn, error = await client.resolve_author(credentials, author)
    if error:
        return {"error": error}
    elements, error = await client.rest_paged(
        credentials,
        "/rest/posts",
        # No sortBy: the finder's default ordering is whatever LinkedIn does, and
        # an unverified value risks a 400 on a call that otherwise works.
        {"q": "author", "author": author_urn},
        limit,
        page_size=min(limit, 50),
    )
    if error:
        return {"error": error}
    posts = [client.post_to_dict(p) for p in elements]
    return {"posts": posts, "count": len(posts), "author": author_urn}


@server.tool(
    name="delete_post",
    description="Delete a LinkedIn post you authored (by URN, share URL, or id).",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="manage_linkedin_posts",
    required_scopes=["w_member_social", "w_organization_social"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "post": {
                "type": "string",
                "description": "Post URN, share URL, or numeric share id",
            },
        },
        "required": ["post"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "deleted": {"type": "boolean"},
            "id": {"type": "string"},
        },
    }
)
async def delete_post(credentials: dict[str, str], post: str) -> dict[str, Any]:
    """Delete a post."""
    urn, error = client.normalize_post_urn(post)
    if error:
        return {"error": error}
    _, error = await client.rest(credentials, "DELETE", f"/rest/posts/{client.encode_urn(urn)}")
    if error:
        return {"error": client.activity_urn_hint(urn, error)}
    return {"deleted": True, "id": urn}


# -- engagement ----------------------------------------------------------------


@server.tool(
    name="get_post_engagement",
    description=(
        "Aggregate engagement summary for one post: total reactions broken down by "
        "type (LIKE, PRAISE, EMPATHY, INTEREST, APPRECIATION, ENTERTAINMENT) plus "
        "the comment count."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_engagement",
    required_scopes=["r_organization_social"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "post": {
                "type": "string",
                "description": "Post URN, share URL, or numeric share id",
            },
        },
        "required": ["post"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "reactions_by_type": {"type": "object"},
            "total_reactions": {"type": "integer"},
            "comments": {"type": "integer"},
        },
    }
)
async def get_post_engagement(credentials: dict[str, str], post: str) -> dict[str, Any]:
    """Reaction counts by type, and the comment count, for one post."""
    urn, error = client.normalize_post_urn(post)
    if error:
        return {"error": error}
    data, error = await client.rest(
        credentials, "GET", f"/rest/socialMetadata/{client.encode_urn(urn)}"
    )
    if error:
        return {"error": error}
    summaries = data.get("reactionSummaries") or {}
    by_type = {rtype: int((s or {}).get("count", 0) or 0) for rtype, s in summaries.items()}
    return {
        "post": urn,
        "url": client.post_url(urn),
        "reactions_by_type": by_type,
        "total_reactions": sum(by_type.values()),
        "comments": int((data.get("commentSummary") or {}).get("count", 0) or 0),
    }


@server.tool(
    name="list_post_reactions",
    description=(
        "List who reacted to a post: each reaction's actor URN (person or "
        "organization), reaction type, and time. Useful for finding your fans."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_engagement",
    required_scopes=["r_organization_social"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "post": {
                "type": "string",
                "description": "Post URN, share URL, or numeric share id",
            },
            "limit": {
                "type": "integer",
                "description": "Max reactions to return (default 100, max 500)",
                "default": 100,
            },
        },
        "required": ["post"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "reactions": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Each: actor (URN), type, created_at",
            },
            "count": {"type": "integer"},
        },
    }
)
async def list_post_reactions(
    credentials: dict[str, str], post: str, limit: int = 100
) -> dict[str, Any]:
    """Who reacted to a post."""
    urn, error = client.normalize_post_urn(post)
    if error:
        return {"error": error}
    elements, error = await client.rest_paged(
        credentials,
        f"/rest/reactions/(entity:{client.encode_urn(urn)})",
        {"q": "entity"},
        max(1, min(limit, 500)),
    )
    if error:
        return {"error": error}
    reactions = [client.reaction_to_dict(r) for r in elements]
    return {"post": urn, "reactions": reactions, "count": len(reactions)}


@server.tool(
    name="list_post_comments",
    description=(
        "List comments on a post, with each comment's actor URN, text, like count, and time."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_engagement",
    required_scopes=["r_organization_social"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "post": {
                "type": "string",
                "description": "Post URN, share URL, or numeric share id",
            },
            "limit": {
                "type": "integer",
                "description": "Max comments to return (default 50, max 500)",
                "default": 50,
            },
        },
        "required": ["post"],
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "comments": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Each: actor (URN), text, likes, created_at",
            },
            "count": {"type": "integer"},
        },
    }
)
async def list_post_comments(
    credentials: dict[str, str], post: str, limit: int = 50
) -> dict[str, Any]:
    """Comments on a post."""
    urn, error = client.normalize_post_urn(post)
    if error:
        return {"error": error}
    elements, error = await client.rest_paged(
        credentials,
        f"/rest/socialActions/{client.encode_urn(urn)}/comments",
        {},
        max(1, min(limit, 500)),
    )
    if error:
        return {"error": error}
    comments = [client.comment_to_dict(c) for c in elements]
    return {"post": urn, "comments": comments, "count": len(comments)}


# -- profile and analytics -----------------------------------------------------


async def _resolve_org_names(
    credentials: dict[str, str], urns: Iterable[str]
) -> dict[str, str | None]:
    """Look up the display name of every organization URN, concurrently.

    Person URNs are skipped: LinkedIn does not expose arbitrary member
    profiles. A failed lookup maps to None rather than failing the caller; a
    missing display name is not worth losing a report over.
    """
    org_urns = {u for u in urns if u and client.org_id_from_urn(u)}
    if not org_urns:
        return {}

    semaphore = asyncio.Semaphore(_FANOUT_CONCURRENCY)

    async def one(urn: str) -> tuple[str, str | None]:
        async with semaphore:
            org, error = await client.rest(
                credentials, "GET", f"/rest/organizations/{client.org_id_from_urn(urn)}"
            )
        return urn, (org.get("localizedName") if not error else None)

    return dict(await asyncio.gather(*(one(u) for u in org_urns)))


async def _resolve_org(
    credentials: dict[str, str], organization: str | None
) -> tuple[str | None, str | None]:
    """Resolve to an organization URN; engagement analytics are organization-only."""
    urn, error = await client.resolve_author(credentials, organization)
    if error:
        return None, error
    if not urn.startswith("urn:li:organization:"):
        return None, (
            "Engagement analytics need an organization (company page): LinkedIn does "
            "not expose engagement data for personal-profile posts via API. Pass a "
            "numeric organization id (find it with list_organizations) or set "
            "LINKEDIN_ORG_ID."
        )
    return urn, None


@server.tool(
    name="get_profile",
    description=(
        "Get the authenticated LinkedIn member's profile (name, email, person URN). "
        "Also a quick way to verify the credentials work."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_profile",
    required_scopes=["openid", "profile", "email"],
)
@declare_schema({"type": "object", "properties": {}})
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
            "person_urn": {"type": "string"},
        },
    }
)
async def get_profile(credentials: dict[str, str]) -> dict[str, Any]:
    """The authenticated member's profile.

    A field LinkedIn did not return is omitted rather than sent as null: the
    declared output schema types these as strings, and a conforming client
    rejects a structured result that does not match.
    """
    data, error = await client.rest(credentials, "GET", "/v2/userinfo")
    if error:
        return {"error": error}
    profile = {
        "name": data.get("name"),
        "email": data.get("email"),
        "person_urn": f"urn:li:person:{data.get('sub')}" if data.get("sub") else None,
        "picture": data.get("picture"),
    }
    return {k: v for k, v in profile.items() if v is not None}


@server.tool(
    name="list_organizations",
    description=(
        "List LinkedIn organizations (company pages) the authenticated member "
        "administers. Use the returned organization id as `author`/`organization` "
        "in other tools."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_analytics",
    required_scopes=["rw_organization_admin"],
)
@declare_schema({"type": "object", "properties": {}})
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "organizations": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Each: id, urn, name, role",
            },
            "count": {"type": "integer"},
        },
    }
)
async def list_organizations(credentials: dict[str, str]) -> dict[str, Any]:
    """Company pages the member administers, with their names."""
    elements, error = await client.rest_paged(
        credentials,
        "/rest/organizationAcls",
        {"q": "roleAssignee", "state": "APPROVED"},
        50,
    )
    if error:
        return {"error": error}

    urns = [acl.get("organization") or acl.get("organizationalTarget") or "" for acl in elements]
    # Concurrent: this is the first tool anyone calls (it is where the org id
    # comes from), and an admin of many pages would otherwise wait on each.
    names = await _resolve_org_names(credentials, urns)
    orgs = [
        {
            "id": client.org_id_from_urn(urn),
            "urn": urn,
            "name": names.get(urn),
            "role": acl.get("role"),
        }
        for acl, urn in zip(elements, urns, strict=True)
    ]
    return {"organizations": orgs, "count": len(orgs)}


@server.tool(
    name="get_page_statistics",
    description=(
        "Lifetime statistics for an organization page: impressions, unique "
        "impressions, clicks, likes, comments, shares, engagement rate, and "
        "follower count."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_analytics",
    required_scopes=["r_organization_social", "rw_organization_admin"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "organization": {
                "type": "string",
                "description": (
                    "Numeric organization id or urn:li:organization:... "
                    "Default: LINKEDIN_ORG_ID env."
                ),
            },
        },
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "statistics": {"type": "object"},
            "follower_count": {"type": "integer"},
        },
    }
)
async def get_page_statistics(
    credentials: dict[str, str], organization: str | None = None
) -> dict[str, Any]:
    """Lifetime share statistics and follower count for a page."""
    org_urn, error = await _resolve_org(credentials, organization)
    if error:
        return {"error": error}

    data, error = await client.rest(
        credentials,
        "GET",
        "/rest/organizationalEntityShareStatistics",
        params={"q": "organizationalEntity", "organizationalEntity": org_urn},
    )
    if error:
        return {"error": error}
    elements = data.get("elements") or []
    stats = (elements[0].get("totalShareStatistics", {}) if elements else {}) or {}

    result: dict[str, Any] = {
        "organization": org_urn,
        "statistics": {
            "impressions": stats.get("impressionCount"),
            "unique_impressions": stats.get("uniqueImpressionsCount"),
            "clicks": stats.get("clickCount"),
            "likes": stats.get("likeCount"),
            "comments": stats.get("commentCount"),
            "shares": stats.get("shareCount"),
            "engagement_rate": stats.get("engagement"),
        },
    }

    # Follower count is a separate endpoint; a failure there does not fail the
    # call. Omitted, not null, when absent: the output schema types it integer.
    followers, follower_err = await client.rest(
        credentials,
        "GET",
        f"/rest/networkSizes/{client.encode_urn(org_urn)}",
        params={"edgeType": "COMPANY_FOLLOWED_BY_MEMBER"},
    )
    if not follower_err and isinstance(followers.get("firstDegreeSize"), int):
        result["follower_count"] = followers["firstDegreeSize"]
    return result


@server.tool(
    name="engagement_report",
    description=(
        "Report on who engages with an organization's recent posts: fetches the "
        "last N posts, collects all reactions and comments, and ranks the people/"
        "organizations engaging most often, including repeat engagers who "
        "interacted with 2+ posts. Actors are LinkedIn URNs; organization actors "
        "are resolved to names (member names aren't exposed by the API)."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=[
        "X-LinkedIn-Access-Token",
        "X-LinkedIn-Refresh-Token",
        "X-LinkedIn-Client-Id",
        "X-LinkedIn-Client-Secret",
    ],
    permission_group="read_linkedin_engagement",
    required_scopes=["r_organization_social", "rw_organization_admin"],
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "organization": {
                "type": "string",
                "description": (
                    "Numeric organization id or urn:li:organization:... "
                    "Default: LINKEDIN_ORG_ID env."
                ),
            },
            "post_count": {
                "type": "integer",
                "description": "How many recent posts to analyze (default 10, max 20)",
                "default": 10,
            },
            "top_n": {
                "type": "integer",
                "description": "How many top engagers to return (default 20)",
                "default": 20,
            },
        },
    }
)
@declare_output_schema(
    {
        "type": "object",
        "properties": {
            "posts_analyzed": {"type": "integer"},
            "totals": {
                "type": "object",
                "description": "reactions, comments, unique_engagers across all posts",
            },
            "top_engagers": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Ranked by total engagement: actor, name (orgs only), reactions, "
                    "comments, posts_engaged"
                ),
            },
            "repeat_engagers": {
                "type": "array",
                "description": "Engagers who interacted with 2+ of the analyzed posts",
            },
            "per_post": {"type": "array", "items": {"type": "object"}},
        },
    }
)
async def engagement_report(
    credentials: dict[str, str],
    organization: str | None = None,
    post_count: int = 10,
    top_n: int = 20,
) -> dict[str, Any]:
    """Rank who engages with an organization's recent posts."""
    post_count = max(1, min(post_count, _MAX_REPORT_POSTS))
    top_n = max(1, min(top_n, 100))

    org_urn, error = await _resolve_org(credentials, organization)
    if error:
        return {"error": error}

    posts, error = await client.rest_paged(
        credentials,
        "/rest/posts",
        {"q": "author", "author": org_urn},
        post_count,
        page_size=post_count,
    )
    if error:
        return {"error": error}
    if not posts:
        return {"error": f"No posts found for {org_urn}."}

    semaphore = asyncio.Semaphore(_FANOUT_CONCURRENCY)

    async def fetch_post_engagement(post: dict[str, Any]) -> dict[str, Any]:
        urn = post.get("id", "")
        encoded = client.encode_urn(urn)
        async with semaphore:
            reactions, r_err = await client.rest_paged(
                credentials,
                f"/rest/reactions/(entity:{encoded})",
                {"q": "entity"},
                _PER_POST_ACTORS,
            )
            comments, c_err = await client.rest_paged(
                credentials,
                f"/rest/socialActions/{encoded}/comments",
                {},
                _PER_POST_ACTORS,
            )
        return {
            "post": urn,
            "text": (post.get("commentary") or "")[:120],
            "url": client.post_url(urn),
            "published_at": client.ms_to_iso(post.get("publishedAt")),
            "reactions": [client.reaction_to_dict(r) for r in (reactions or [])],
            "comments": [client.comment_to_dict(c) for c in (comments or [])],
            "errors": [e for e in (r_err, c_err) if e],
        }

    results = await asyncio.gather(*(fetch_post_engagement(p) for p in posts))

    # Every post failing to yield engagement data (typically a missing
    # Community Management API product) is surfaced, not an empty report.
    all_errors = [e for r in results for e in r["errors"]]
    if len(all_errors) >= 2 * len(results):
        return {"error": all_errors[0]}

    engagers: dict[str, dict[str, Any]] = {}
    for r in results:
        for reaction in r["reactions"]:
            actor = reaction.get("actor")
            if actor:
                entry = engagers.setdefault(actor, {"reactions": 0, "comments": 0, "posts": set()})
                entry["reactions"] += 1
                entry["posts"].add(r["post"])
        for comment in r["comments"]:
            actor = comment.get("actor")
            # The organization engaging with its own posts is not an engager.
            if actor and actor != org_urn:
                entry = engagers.setdefault(actor, {"reactions": 0, "comments": 0, "posts": set()})
                entry["comments"] += 1
                entry["posts"].add(r["post"])
    engagers.pop(org_urn, None)

    org_names = await _resolve_org_names(credentials, engagers)

    ranked = sorted(
        (
            {
                "actor": actor,
                "name": org_names.get(actor),
                "reactions": e["reactions"],
                "comments": e["comments"],
                "total": e["reactions"] + e["comments"],
                "posts_engaged": len(e["posts"]),
            }
            for actor, e in engagers.items()
        ),
        key=lambda x: (x["total"], x["posts_engaged"]),
        reverse=True,
    )

    per_post = [
        {
            "post": r["post"],
            "url": r["url"],
            "text": r["text"],
            "published_at": r["published_at"],
            "reactions": len(r["reactions"]),
            "comments": len(r["comments"]),
        }
        for r in results
    ]

    report: dict[str, Any] = {
        "organization": org_urn,
        "posts_analyzed": len(results),
        "totals": {
            "reactions": sum(p["reactions"] for p in per_post),
            "comments": sum(p["comments"] for p in per_post),
            "unique_engagers": len(engagers),
        },
        "top_engagers": ranked[:top_n],
        "repeat_engagers": [e for e in ranked if e["posts_engaged"] >= 2][:top_n],
        "per_post": per_post,
    }
    if all_errors:
        report["warnings"] = list(dict.fromkeys(all_errors))[:3]
    return report
