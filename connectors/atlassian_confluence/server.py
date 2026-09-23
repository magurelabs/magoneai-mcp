"""The atlassian-confluence connector.

Ported from the v1 mixin form by a codemod.

26 tools, 16 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema

SERVER_NAME = "atlassian-confluence"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit
import httpx
from ._shared.atlassian_auth import (
    CONFLUENCE_READ_ATTACHMENT,
    CONFLUENCE_READ_BLOGPOST,
    CONFLUENCE_READ_COMMENT,
    CONFLUENCE_READ_FOLDER,
    CONFLUENCE_READ_HIERARCHY,
    CONFLUENCE_READ_LABEL,
    CONFLUENCE_READ_PAGE,
    CONFLUENCE_READ_SPACE,
    CONFLUENCE_READ_TEMPLATE,
)
from typing import Any, Dict
from ._shared.atlassian_auth import CONFLUENCE_SEARCH
import base64
import binascii
from typing import Any, Dict, List, Optional
from ._shared.atlassian_auth import (
    CONFLUENCE_DELETE_ATTACHMENT,
    CONFLUENCE_DELETE_PAGE,
    CONFLUENCE_UPLOAD_ATTACHMENT,
    CONFLUENCE_WRITE_BLOGPOST,
    CONFLUENCE_WRITE_COMMENT,
    CONFLUENCE_WRITE_LABEL,
    CONFLUENCE_WRITE_PAGE,
)
from typing import Dict, Iterable, List
from typing import Any, Dict, Tuple


PRODUCT = "Confluence"

MAX_PAGE_SIZE = 250

DEFAULT_PAGE_SIZE = 25

async def resolve_cloud_id(credentials: Dict[str, str]):
    """(cloud_id, site_url) for the Confluence site this connection targets."""
    return await _resolve_cloud_id(credentials, PRODUCT)

def _error_message(response: httpx.Response, method: str, path: str) -> str:
    """Atlassian's own message where it has one, so the caller can act on it."""
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        errors = payload.get("errors")
        # The element type is checked, not just the list: an `errors` list of
        # strings would make .get raise AttributeError here, replacing the real
        # API error with an internal one on the failure path.
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = str(errors[0].get("title") or errors[0].get("detail") or "")
        if not detail:
            detail = str(payload.get("message") or payload.get("error") or "")
    if not detail:
        detail = (response.text or "")[:300]

    hint = ""
    if response.status_code in (401, 403):
        hint = (
            " The connection may lack the scope this endpoint needs; "
            "api.atlassian.com answers 401 for a scope that was never granted."
        )
    elif response.status_code == 429:
        hint = " Confluence is rate limiting; retry after a pause."
    return (
        f"Confluence API error {response.status_code} on {method} {path}: "
        f"{detail or response.reason_phrase}.{hint}"
    )

async def request(
    credentials: Dict[str, str],
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Any] = None,
) -> Any:
    """One authenticated Confluence call. Returns parsed JSON, or None for 204.

    `path` starts at the wiki root, e.g. "/wiki/api/v2/pages/123" for v2 or
    "/wiki/rest/api/search" for v1.
    """
    cloud_id, _ = await resolve_cloud_id(credentials)
    url = f"{GATEWAY}/ex/confluence/{cloud_id}{path}"
    headers = {**auth_headers(credentials), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.request(
            method,
            url,
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json_body,
            headers=headers,
        )

    if response.status_code == 404:
        raise ValueError(
            f"Not found: {method} {path}. Check the id, and that the connected "
            "user can see it: Confluence answers 404 rather than 403 for content "
            "the caller has no permission on."
        )
    if response.status_code >= 400:
        raise ValueError(_error_message(response, method, path))
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None

async def get(credentials: Dict[str, str], path: str, **params: Any) -> Any:
    return await request(credentials, "GET", path, params=params)

async def post(credentials: Dict[str, str], path: str, body: Any, **params: Any) -> Any:
    return await request(credentials, "POST", path, json_body=body, params=params)

async def put(credentials: Dict[str, str], path: str, body: Any, **params: Any) -> Any:
    return await request(credentials, "PUT", path, json_body=body, params=params)

async def delete(credentials: Dict[str, str], path: str, **params: Any) -> Any:
    return await request(credentials, "DELETE", path, params=params)

async def upload_attachment(
    credentials: Dict[str, str],
    path: str,
    *,
    file_name: str,
    content: bytes,
    mime_type: Optional[str] = None,
) -> Any:
    """Multipart upload for the v1 attachment endpoint.

    v2 has no endpoint that creates an attachment, only ones that read or
    delete one, so this is the one place a write here still has to speak v1.
    Confluence rejects a JSON content-type on this endpoint and requires
    ``X-Atlassian-Token: no-check`` to bypass its XSRF check, so this bypasses
    ``request()`` rather than bending it to send both shapes.
    """
    cloud_id, _ = await resolve_cloud_id(credentials)
    url = f"{GATEWAY}/ex/confluence/{cloud_id}{path}"
    headers = {**auth_headers(credentials), "X-Atlassian-Token": "no-check"}
    files = {"file": (file_name, content, mime_type or "application/octet-stream")}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(url, files=files, headers=headers)

    if response.status_code >= 400:
        raise ValueError(_error_message(response, "POST", path))
    try:
        return response.json()
    except ValueError:
        return None

def clamp(limit: Optional[int]) -> int:
    """Keep a caller-supplied limit inside what Confluence will accept."""
    if not limit or limit < 1:
        return DEFAULT_PAGE_SIZE
    return min(limit, MAX_PAGE_SIZE)

def next_cursor(response: Any) -> Optional[str]:
    """Pull the opaque cursor out of a v2 response's `_links.next`.

    v2 pages by cursor, not offset, and only ever hands back a full next URL.
    Callers need the cursor alone so it can round-trip as a tool argument.
    """
    if not isinstance(response, dict):
        return None
    links = response.get("_links")
    if not isinstance(links, dict):
        return None
    link = links.get("next")
    if not isinstance(link, str) or not link:
        return None
    return parse_qs(urlsplit(link).query).get("cursor", [None])[0] or None

def page_url(site_url: str, page: Dict[str, Any]) -> str:
    """A link a human can open, built from the site rather than the gateway.

    Confluence returns `_links.webui` relative to the site's /wiki root, so it
    needs the site prefixed; without that the caller gets a path, not a URL.
    """
    if not site_url:
        return ""
    webui = (page.get("_links") or {}).get("webui")
    if not isinstance(webui, str) or not webui:
        return ""
    base = site_url.rstrip("/")
    return f"{base}/wiki{webui}" if webui.startswith("/") else f"{base}/wiki/{webui}"

def body_text(page: Dict[str, Any]) -> Optional[str]:
    """The page body, whichever representation was asked for."""
    body = page.get("body")
    if not isinstance(body, dict):
        return None
    for representation in ("storage", "atlas_doc_format", "view"):
        block = body.get(representation)
        if isinstance(block, dict) and block.get("value") is not None:
            return block.get("value")
    return None


def _shape_page(page: Dict[str, Any], site_url: str = "", include_body: bool = False) -> dict:
    """Flatten a v2 page into the shape every tool here returns."""
    shaped = {
        "id": page.get("id"),
        "title": page.get("title"),
        "status": page.get("status"),
        "space_id": page.get("spaceId"),
        "parent_id": page.get("parentId"),
        "author_id": page.get("authorId"),
        "version": (page.get("version") or {}).get("number"),
        "url": page_url(site_url, page),
    }
    if include_body:
        shaped["body"] = body_text(page)
    return shaped

def _shape_attachment(attachment: Dict[str, Any], site_url: str = "") -> dict:
    return {
        "id": attachment.get("id"),
        "title": attachment.get("title"),
        "media_type": attachment.get("mediaType"),
        "file_size": attachment.get("fileSize"),
        "version": (attachment.get("version") or {}).get("number"),
        "url": page_url(site_url, attachment),
    }

def _shape_label(label: Dict[str, Any]) -> dict:
    return {"id": label.get("id"), "name": label.get("name"), "prefix": label.get("prefix")}

def _shape_comment(comment: Dict[str, Any]) -> dict:
    return {
        "id": comment.get("id"),
        "status": comment.get("status"),
        "title": comment.get("title"),
        "body": body_text(comment),
        "version": (comment.get("version") or {}).get("number"),
    }


def _shape_result(result: Dict[str, Any], site_url: str) -> dict:
    """Flatten one v1 search hit."""
    content = result.get("content") or {}
    base = site_url.rstrip("/")
    webui = result.get("url") or ""
    return {
        "id": content.get("id"),
        "title": result.get("title") or content.get("title"),
        "type": content.get("type"),
        "status": content.get("status"),
        "space": ((result.get("resultGlobalContainer") or {}).get("title")),
        "excerpt": result.get("excerpt"),
        "last_modified": result.get("lastModified"),
        "url": f"{base}/wiki{webui}" if base and webui.startswith("/") else "",
    }


_READ_FORMAT = {"wiki": "storage"}

BASE_SCOPES: List[str] = ["read:me", "offline_access"]

JIRA_READ = ["read:jira-work"]

JIRA_WRITE = ["write:jira-work"]

JIRA_SEARCH = ["search:jira-work"]

JIRA_SOFTWARE_READ_BOARD = ["read:board-scope:jira-software", "read:project:jira"]

JIRA_SOFTWARE_READ_SPRINT = ["read:sprint:jira-software"]

JIRA_SOFTWARE_READ_EPIC = [
    "read:epic:jira-software",
    "read:issue-details:jira",
    "read:jql:jira",
]

JIRA_SOFTWARE_READ = JIRA_SOFTWARE_READ_BOARD + JIRA_SOFTWARE_READ_SPRINT + JIRA_SOFTWARE_READ_EPIC

JIRA_SOFTWARE_WRITE = ["write:sprint:jira-software"]

JIRA_MANAGE_PROJECT = ["manage:jira-project"]

CONFLUENCE_READ_PAGE = ["read:page:confluence"]

CONFLUENCE_READ_HIERARCHY = ["read:hierarchical-content:confluence"]

CONFLUENCE_READ_COMMENT = ["read:comment:confluence"]

CONFLUENCE_READ_SPACE = ["read:space:confluence"]

CONFLUENCE_READ_FOLDER = ["read:folder:confluence"]

CONFLUENCE_WRITE_PAGE = ["write:page:confluence"]

CONFLUENCE_SEARCH = ["search:confluence"]

CONFLUENCE_READ_ATTACHMENT = ["read:attachment:confluence"]

CONFLUENCE_DELETE_PAGE = ["delete:page:confluence"]

CONFLUENCE_READ_LABEL = ["read:page:confluence"]

CONFLUENCE_WRITE_LABEL = ["write:confluence-content"]

CONFLUENCE_READ_BLOGPOST = ["read:page:confluence"]

CONFLUENCE_WRITE_BLOGPOST = ["write:page:confluence"]

CONFLUENCE_READ_TEMPLATE = ["read:confluence-content.summary"]

CONFLUENCE_WRITE_COMMENT = ["write:comment:confluence"]

CONFLUENCE_UPLOAD_ATTACHMENT = ["write:confluence-file"]

CONFLUENCE_DELETE_ATTACHMENT = ["delete:attachment:confluence"]

GROUP_SCOPES: Dict[str, List[str]] = {
    "common": [],  # covered by BASE_SCOPES
    "read_jira": JIRA_READ,
    "write_jira": JIRA_WRITE,
    "search_jira": JIRA_SEARCH,
    "read_jira_software": JIRA_SOFTWARE_READ,
    "write_jira_software": JIRA_SOFTWARE_WRITE,
    "manage_jira_project": JIRA_MANAGE_PROJECT,
    "read_confluence": (
        CONFLUENCE_READ_PAGE
        + CONFLUENCE_READ_HIERARCHY
        + CONFLUENCE_READ_COMMENT
        + CONFLUENCE_READ_SPACE
        + CONFLUENCE_READ_FOLDER
        + CONFLUENCE_READ_ATTACHMENT
    ),
    "write_confluence": CONFLUENCE_WRITE_PAGE
    + CONFLUENCE_DELETE_PAGE
    + CONFLUENCE_UPLOAD_ATTACHMENT
    + CONFLUENCE_DELETE_ATTACHMENT
    + CONFLUENCE_WRITE_COMMENT,
    "search_confluence": CONFLUENCE_SEARCH,
    "read_confluence_label": CONFLUENCE_READ_LABEL,
    "write_confluence_label": CONFLUENCE_WRITE_LABEL,
    "read_confluence_blogpost": CONFLUENCE_READ_BLOGPOST,
    "write_confluence_blogpost": CONFLUENCE_WRITE_BLOGPOST,
    "read_confluence_template": CONFLUENCE_READ_TEMPLATE,
}

JIRA_GROUPS = [
    "common",
    "read_jira",
    "write_jira",
    "search_jira",
    "read_jira_software",
    "write_jira_software",
    "manage_jira_project",
]

CONFLUENCE_GROUPS = [
    "common",
    "read_confluence",
    "write_confluence",
    "search_confluence",
    "read_confluence_label",
    "write_confluence_label",
    "read_confluence_blogpost",
    "write_confluence_blogpost",
    "read_confluence_template",
]

def scopes_for(groups: Iterable[str]) -> List[str]:
    """The minimal scope set for a selection of permission groups.

    Unknown group names are ignored rather than raising: a connection may carry
    a selection made against a newer build of the server, and failing the whole
    authorization because one name is unrecognised is worse than granting the
    subset that is understood.

    Sorted, because the result lands in a consent URL and in test assertions and
    neither wants set ordering.
    """
    scopes = set(BASE_SCOPES)
    for group in groups:
        scopes.update(GROUP_SCOPES.get(group, []))
    return sorted(scopes)

ALL_SCOPES: List[str] = scopes_for(GROUP_SCOPES)

# `build_auth_config` and its `base.models` imports were removed here.
#
# In v1 they produced the platform-facing connection form, passed as
# `auth_config=` at registration. The v2 ConnectorServer has no `auth_config`
# parameter, so nothing could send it and nothing called the function: it was
# dead code that only kept an import of the retired v1 framework alive, and
# that import is why this module would not import at all.
#
# No information is lost. The platform already holds each template's
# auth_config and keeps it locked; changing it is a deliberate step on the
# platform side, not something a connector asserts.


GATEWAY = "https://api.atlassian.com"

ACCESSIBLE_RESOURCES_URL = f"{GATEWAY}/oauth/token/accessible-resources"

ME_URL = f"{GATEWAY}/me"

TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

ATLASSIAN_CREDENTIAL_HEADERS = [
    "X-Atlassian-Access-Token",
    "X-Atlassian-Cloud-Id",
]

COMMON_GROUP = "common"

COMMON_SCOPES = ["read:me"]

def access_token(credentials: Dict[str, str]) -> str:
    """The bearer token for this connection.

    The message names the fix rather than the symptom: an absent token means the
    connection was never completed or its refresh failed, and neither is
    something a model can retry its way out of.
    """
    token = credentials.get("atlassian_access_token")
    if not token:
        raise ValueError(
            "No Atlassian access token on this connection. Reconnect Atlassian "
            "from the connections page."
        )
    return token

def auth_headers(credentials: Dict[str, str]) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token(credentials)}",
        "Accept": "application/json",
    }

def reject_bad_token(response: httpx.Response) -> None:
    """Turn a gateway auth failure into something the reader can act on."""
    if response.status_code in (401, 403):
        raise ValueError(
            f"Atlassian rejected the access token (HTTP {response.status_code}). "
            "The connection may need to be re-authorized."
        )

async def accessible_resources(credentials: Dict[str, str]) -> list:
    """Every Atlassian site this token can reach, with its cloudId."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(ACCESSIBLE_RESOURCES_URL, headers=auth_headers(credentials))
    reject_bad_token(response)
    response.raise_for_status()
    resources = response.json()
    return resources if isinstance(resources, list) else []

async def _resolve_cloud_id(credentials: Dict[str, str], product: str) -> Tuple[str, str]:
    """Return (cloud_id, site_url) for the site this connection targets.

    Both are needed and they are not interchangeable. The gateway is the only
    host the token authenticates against, so requests go there; the site URL is
    what a browsable link has to be built from, and handing a user an
    api.atlassian.com link gives them something they cannot open.

    An explicitly configured cloud_id wins. Without one, a multi-site org would
    otherwise get whichever site Atlassian happens to list first, which is a
    quiet wrong answer rather than a loud one.

    Args:
        credentials: The tool's credentials dict.
        product: Named only so the "no sites" message can say which product the
            caller was reaching for.
    """
    wanted = credentials.get("atlassian_cloud_id")
    resources = await accessible_resources(credentials)
    if not resources:
        raise ValueError(
            f"This Atlassian account has no accessible sites. Check that the "
            f"connected user has access to a {product} site."
        )

    if wanted:
        for resource in resources:
            if resource.get("id") == wanted:
                return wanted, resource.get("url", "")
        available = ", ".join(r.get("url", r.get("id", "?")) for r in resources)
        raise ValueError(
            f"Atlassian cloud id {wanted!r} is not accessible with this connection. "
            f"Accessible sites: {available}"
        )
    return resources[0]["id"], resources[0].get("url", "")



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence_label'  required_scopes: ['write:confluence-content']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /wiki/rest/api/content/{pageId}/label
@server.tool(
    name='addConfluenceLabel',
    description="Add one or more labels to a Confluence page.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence_label',
    required_scopes=['write:confluence-content'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'labels': {'type': 'array',
                               'items': {'type': 'string'},
                               'description': "Label names to add, e.g. ['roadmap', "
                                              "'q3']"}},
     'required': ['pageId', 'labels']})
async def addConfluenceLabel(credentials: Dict[str, str], pageId: str, labels: List[str]
) -> dict:
    """Attach one or more labels."""
    body = [{"prefix": "global", "name": name} for name in labels]
    result = await post(credentials, f"/wiki/rest/api/content/{pageId}/label", body)
    added = (result or {}).get("results") or []
    return {
        "success": True,
        "page_id": pageId,
        "labels": [label.get("name") for label in added if isinstance(label, dict)] or labels,
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'common'  required_scopes: ['read:me']
# REVIEWED: readOnlyHint corrected to True - GET /me; reads the caller's own profile.
@server.tool(
    name='atlassianUserInfo',
    description="Get the current authenticated user's Atlassian account information (account id, name, email). Useful for resolving 'me' or 'assign it to me' to a concrete account id.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='common',
    required_scopes=['read:me'],
)
@declare_schema({'type': 'object', 'properties': {}})
async def atlassianUserInfo(credentials: Dict[str, str]) -> dict:
    """Read the profile behind this connection's token."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(ME_URL, headers=auth_headers(credentials))
    reject_bad_token(response)
    response.raise_for_status()
    profile: Dict[str, Any] = response.json()
    return {
        "account_id": profile.get("account_id"),
        "name": profile.get("name"),
        "email": profile.get("email"),
        "picture": profile.get("picture"),
        "locale": profile.get("locale"),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['write:confluence-file']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /wiki/rest/api/content/{id}/child/attachment as multipart,
# via upload_attachment() rather than the shared post() wrapper.
@server.tool(
    name='createConfluenceAttachment',
    description="Attach a file to a Confluence page. Content is base64-encoded, so a binary file such as a PDF or image is sent the same way as text. Uploading a file with the same name as an existing attachment adds a new version of it rather than a duplicate.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['write:confluence-file'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'fileName': {'type': 'string',
                                 'description': 'Name the attachment is saved as, e.g. '
                                                "'report.pdf'"},
                    'fileContentBase64': {'type': 'string',
                                          'description': 'File content, base64-encoded'},
                    'mimeType': {'type': 'string',
                                 'description': '(Optional) MIME type, e.g. '
                                                "'application/pdf'. Defaults to "
                                                'application/octet-stream when omitted.'}},
     'required': ['pageId', 'fileName', 'fileContentBase64']})
async def createConfluenceAttachment(credentials: Dict[str, str],
    pageId: str,
    fileName: str,
    fileContentBase64: str,
    mimeType: Optional[str] = None,
) -> dict:
    """Upload one attachment, or a new version of an existing one."""
    try:
        content = base64.b64decode(fileContentBase64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"fileContentBase64 is not valid base64: {exc}") from exc

    result = await upload_attachment(
        credentials,
        f"/wiki/rest/api/content/{pageId}/child/attachment",
        file_name=fileName,
        content=content,
        mime_type=mimeType,
    )
    # v1's create-attachment endpoint wraps its output in a `results` list,
    # one entry per file created by the call. It is always one here since
    # exactly one file is sent per call.
    items = (result or {}).get("results") or []
    first = items[0] if items else {}
    return {
        "success": True,
        "page_id": pageId,
        "attachment_id": first.get("id"),
        "filename": first.get("title", fileName),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence_blogpost'  required_scopes: ['write:page:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /wiki/api/v2/blogposts
@server.tool(
    name='createConfluenceBlogPost',
    description="Create a Confluence blog post in a space.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence_blogpost',
    required_scopes=['write:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'spaceId': {'type': 'string',
                                'description': 'Numeric space id from getConfluenceSpaces'},
                    'title': {'type': 'string', 'description': 'Blog post title'},
                    'body': {'type': 'string',
                             'description': 'Post body in the given bodyFormat. For '
                                            "'storage' this is Confluence XHTML, e.g. "
                                            "'<p>Hello</p>'."},
                    'bodyFormat': {'type': 'string',
                                   'enum': ['storage', 'atlas_doc_format', 'wiki'],
                                   'description': "(Optional) Format of 'body'. Defaults "
                                                  'to Confluence storage XHTML.',
                                   'default': 'storage'},
                    'status': {'type': 'string',
                               'enum': ['current', 'draft'],
                               'description': "(Optional) Publish immediately ('current') "
                                              'or save as a draft',
                               'default': 'current'}},
     'required': ['spaceId', 'title', 'body']})
async def createConfluenceBlogPost(credentials: Dict[str, str],
    spaceId: str,
    title: str,
    body: str,
    bodyFormat: str = "storage",
    status: str = "current",
) -> dict:
    """Create one blog post."""
    _, site_url = await resolve_cloud_id(credentials)
    payload: Dict[str, Any] = {
        "spaceId": spaceId,
        "status": status,
        "title": title,
        "body": {"representation": bodyFormat, "value": body},
    }
    created = await post(credentials, "/wiki/api/v2/blogposts", payload)
    return {
        "id": (created or {}).get("id"),
        "title": (created or {}).get("title"),
        "space_id": (created or {}).get("spaceId"),
        "version": ((created or {}).get("version") or {}).get("number"),
        "url": page_url(site_url, created or {}),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['write:comment:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /wiki/api/v2/footer-comments
@server.tool(
    name='createConfluenceFooterComment',
    description="Add a footer comment to a Confluence page, or reply to an existing comment when parentCommentId is given. Footer comments are the page-level discussion. Send either pageId or parentCommentId.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['write:comment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string',
                               'description': '(Optional) Numeric page id to comment on. '
                                              'Omit only when replying, where '
                                              'parentCommentId identifies the thread.'},
                    'body': {'type': 'string',
                             'description': 'Comment body in Confluence storage XHTML, '
                                            "e.g. '<p>Hi</p>'"},
                    'parentCommentId': {'type': 'string',
                                        'description': '(Optional) Comment id to reply to, '
                                                       'making this a reply rather than a '
                                                       'new top-level comment'}},
     'required': ['body'],
     'anyOf': [{'required': ['pageId']}, {'required': ['parentCommentId']}]})
async def createConfluenceFooterComment(credentials: Dict[str, str],
    body: str,
    pageId: Optional[str] = None,
    parentCommentId: Optional[str] = None,
) -> dict:
    """Add a page comment or a reply."""
    if not pageId and not parentCommentId:
        raise ValueError(
            "Provide 'pageId' to comment on a page, or 'parentCommentId' to reply."
        )
    payload: Dict[str, Any] = {"body": {"representation": "storage", "value": body}}
    if parentCommentId:
        payload["parentCommentId"] = parentCommentId
    else:
        payload["pageId"] = pageId

    created = await post(credentials, "/wiki/api/v2/footer-comments", payload)
    return {
        "id": (created or {}).get("id"),
        "page_id": (created or {}).get("pageId", pageId),
        "is_reply": bool(parentCommentId),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['write:comment:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /wiki/api/v2/inline-comments
@server.tool(
    name='createConfluenceInlineComment',
    description="Add an inline comment anchored to a selection of text on a Confluence page. The selected text must appear on the page exactly as given, or Confluence cannot anchor the comment.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['write:comment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'body': {'type': 'string',
                             'description': 'Comment body in Confluence storage XHTML'},
                    'selectedText': {'type': 'string',
                                     'description': 'The exact text on the page to anchor '
                                                    'to'},
                    'textSelectionMatchCount': {'type': 'integer',
                                                'description': '(Optional) How many times '
                                                               'the selected text occurs '
                                                               'on the page before and '
                                                               'including the one being '
                                                               'anchored to. Use 1 when '
                                                               'the text appears only '
                                                               'once.',
                                                'default': 1},
                    'textSelectionMatchIndex': {'type': 'integer',
                                                'description': '(Optional) 0-based index '
                                                               'of which occurrence to '
                                                               'anchor to',
                                                'default': 0}},
     'required': ['pageId', 'body', 'selectedText']})
async def createConfluenceInlineComment(credentials: Dict[str, str],
    pageId: str,
    body: str,
    selectedText: str,
    textSelectionMatchCount: int = 1,
    textSelectionMatchIndex: int = 0,
) -> dict:
    """Anchor a comment to page text."""
    payload = {
        "pageId": pageId,
        "body": {"representation": "storage", "value": body},
        "inlineCommentProperties": {
            "textSelection": selectedText,
            "textSelectionMatchCount": textSelectionMatchCount,
            "textSelectionMatchIndex": textSelectionMatchIndex,
        },
    }
    created = await post(credentials, "/wiki/api/v2/inline-comments", payload)
    return {
        "id": (created or {}).get("id"),
        "page_id": (created or {}).get("pageId", pageId),
        "anchored_to": selectedText,
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['write:page:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /wiki/api/v2/pages
@server.tool(
    name='createConfluencePage',
    description="Create a Confluence page or live doc in a space. Takes the space's numeric id, which getConfluenceSpaces returns; a space KEY will not work here.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['write:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'spaceId': {'type': 'string',
                                'description': 'Numeric space id from getConfluenceSpaces'},
                    'title': {'type': 'string', 'description': 'Page title'},
                    'body': {'type': 'string',
                             'description': 'Page body in the given bodyFormat. For '
                                            "'storage' this is Confluence XHTML, e.g. "
                                            "'<p>Hello</p>'."},
                    'bodyFormat': {'type': 'string',
                                   'enum': ['storage', 'atlas_doc_format', 'wiki'],
                                   'description': "(Optional) Format of 'body'. Defaults "
                                                  'to Confluence storage XHTML.',
                                   'default': 'storage'},
                    'parentId': {'type': 'string',
                                 'description': '(Optional) Id of the parent page, to nest '
                                                'this one under it rather than at the '
                                                'space root'},
                    'status': {'type': 'string',
                               'enum': ['current', 'draft'],
                               'description': "(Optional) Publish immediately ('current') "
                                              'or save as a draft',
                               'default': 'current'}},
     'required': ['spaceId', 'title', 'body']})
async def createConfluencePage(credentials: Dict[str, str],
    spaceId: str,
    title: str,
    body: str,
    bodyFormat: str = "storage",
    parentId: Optional[str] = None,
    status: str = "current",
) -> dict:
    """Create one page."""
    _, site_url = await resolve_cloud_id(credentials)
    payload: Dict[str, Any] = {
        "spaceId": spaceId,
        "status": status,
        "title": title,
        "body": {"representation": bodyFormat, "value": body},
    }
    if parentId:
        payload["parentId"] = parentId

    created = await post(credentials, "/wiki/api/v2/pages", payload)
    return {
        "id": (created or {}).get("id"),
        "title": (created or {}).get("title"),
        "space_id": (created or {}).get("spaceId"),
        "version": ((created or {}).get("version") or {}).get("number"),
        "url": page_url(site_url, created or {}),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['delete:attachment:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /wiki/api/v2/attachments/{attachmentId}
@server.tool(
    name='deleteConfluenceAttachment',
    description="Delete a Confluence attachment by its id, removing every version of it.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['delete:attachment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'attachmentId': {'type': 'string',
                                     'description': 'Attachment id, from '
                                                    'getConfluencePageAttachments'}},
     'required': ['attachmentId']})
async def deleteConfluenceAttachment(credentials: Dict[str, str], attachmentId: str
) -> dict:
    """Remove one attachment, all versions."""
    await delete(credentials, f"/wiki/api/v2/attachments/{attachmentId}")
    return {"success": True, "attachment_id": attachmentId}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['delete:page:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /wiki/api/v2/pages/{pageId}
@server.tool(
    name='deleteConfluencePage',
    description="Permanently delete a Confluence page. Deleting a page with children moves them up to the deleted page's parent rather than deleting them too.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['delete:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'}},
     'required': ['pageId']})
async def deleteConfluencePage(credentials: Dict[str, str], pageId: str) -> dict:
    """Permanently delete one page."""
    await delete(credentials, f"/wiki/api/v2/pages/{pageId}")
    return {"success": True, "page_id": pageId}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'common'  required_scopes: ['read:me']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET https://api.atlassian.com/oauth/token/accessible-resources, an
# absolute URL rather than a site path, which is why it reads as a
# different shape from every other tool here.
@server.tool(
    name='getAccessibleAtlassianResources',
    description="List every Atlassian site this connection can reach, with the cloudId of each. Every other tool needs a cloudId, so call this first when the site is not already known, and whenever an account has access to more than one site.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='common',
    required_scopes=['read:me'],
)
@declare_schema({'type': 'object', 'properties': {}})
async def getAccessibleAtlassianResources(credentials: Dict[str, str]) -> dict:
    """Enumerate the sites this token reaches."""
    resources = await accessible_resources(credentials)
    return {
        "count": len(resources),
        "resources": [
            {
                "cloud_id": r.get("id"),
                "url": r.get("url"),
                "name": r.get("name"),
                "scopes": r.get("scopes", []),
            }
            for r in resources
            if isinstance(r, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence_blogpost'  required_scopes: ['read:page:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/blogposts/{blogPostId}
@server.tool(
    name='getConfluenceBlogPost',
    description="Get a Confluence blog post by id, including its body.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence_blogpost',
    required_scopes=['read:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'blogPostId': {'type': 'string', 'description': 'Numeric blog post id'},
                    'bodyFormat': {'type': 'string',
                                   'enum': ['storage', 'atlas_doc_format', 'view'],
                                   'description': '(Optional) Body representation to '
                                                  'return',
                                   'default': 'storage'}},
     'required': ['blogPostId']})
async def getConfluenceBlogPost(credentials: Dict[str, str], blogPostId: str, bodyFormat: str = "storage"
) -> dict:
    """Fetch one blog post with its body."""
    _, site_url = await resolve_cloud_id(credentials)
    post_ = await get(
        credentials, f"/wiki/api/v2/blogposts/{blogPostId}", **{"body-format": bodyFormat}
    )
    return _shape_page(post_ or {}, site_url, include_body=True)



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:comment:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/{segment}/{commentId}/children
@server.tool(
    name='getConfluenceCommentChildren',
    description="List the replies to a Confluence comment. A thread is a comment plus its children, so this is how a discussion is read in order. commentType must match what the comment actually is: v2 has separate endpoints for footer and inline comments, not one unified 'comment' route, so the wrong type 404s.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:comment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'commentId': {'type': 'string', 'description': 'Numeric comment id'},
                    'commentType': {'type': 'string',
                                    'enum': ['footer', 'inline'],
                                    'description': 'Which kind of comment this is - '
                                                   "'footer' if it came from "
                                                   'getConfluencePageFooterComments, '
                                                   "'inline' if it came from "
                                                   'getConfluencePageInlineComments.'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum replies to return (1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}},
     'required': ['commentId', 'commentType']})
async def getConfluenceCommentChildren(credentials: Dict[str, str],
    commentId: str,
    commentType: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Replies to one comment."""
    segment = "footer-comments" if commentType == "footer" else "inline-comments"
    payload = await get(
        credentials,
        f"/wiki/api/v2/{segment}/{commentId}/children",
        limit=clamp(limit),
        cursor=cursor,
        **{"body-format": "storage"},
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "comments": [_shape_comment(c) for c in results if isinstance(c, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:folder:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/folders/{folderId}
@server.tool(
    name='getConfluenceFolder',
    description="Get metadata for a Confluence folder: its title, parent and space. Folders are a Cloud content type distinct from pages, so a folder id will not work with getConfluencePage. Recognise one from a URL shaped /wiki/spaces/{key}/folder/{id}.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:folder:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'folderId': {'type': 'string', 'description': 'Numeric folder id'}},
     'required': ['folderId']})
async def getConfluenceFolder(credentials: Dict[str, str], folderId: str) -> dict:
    """Metadata for one folder."""
    _, site_url = await resolve_cloud_id(credentials)
    folder = await get(credentials, f"/wiki/api/v2/folders/{folderId}") or {}
    return {
        "id": folder.get("id"),
        "title": folder.get("title"),
        "type": folder.get("type"),
        "status": folder.get("status"),
        "space_id": folder.get("spaceId"),
        "parent_id": folder.get("parentId"),
        "parent_type": folder.get("parentType"),
        "url": page_url(site_url, folder),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:hierarchical-content:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/folders/{folderId}/direct-children
@server.tool(
    name='getConfluenceFolderChildren',
    description="List what sits directly inside a Confluence folder: pages, and nested folders. Use this to walk a folder-organised space, the way getConfluencePageDescendants walks a page tree.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:hierarchical-content:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'folderId': {'type': 'string', 'description': 'Numeric folder id'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum children to return '
                                             '(1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'. It is null once "
                                              'there are no further results.'}},
     'required': ['folderId']})
async def getConfluenceFolderChildren(credentials: Dict[str, str],
    folderId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Direct children of one folder."""
    # v2 has no plain /children route on a folder, only /direct-children
    # (one level) and /descendants (every level); calling /children 404s.
    _, site_url = await resolve_cloud_id(credentials)
    payload = await get(
        credentials,
        f"/wiki/api/v2/folders/{folderId}/direct-children",
        limit=clamp(limit),
        cursor=cursor,
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        # Children are a mix of pages and nested folders, so the type is
        # reported: a caller has to know which tool reads each one.
        "children": [
            {**_shape_page(c, site_url), "type": c.get("type")}
            for c in results
            if isinstance(c, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:page:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/pages/{pageId}
@server.tool(
    name='getConfluencePage',
    description="Get a Confluence page or live doc by id, including its body. Use searchConfluenceUsingCql or getPagesInConfluenceSpace to find the id when only a title is known.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'bodyFormat': {'type': 'string',
                                   'enum': ['storage', 'atlas_doc_format', 'view'],
                                   'description': '(Optional) Body representation to '
                                                  "return. 'storage' is Confluence's "
                                                  "XHTML, 'view' is rendered HTML.",
                                   'default': 'storage'}},
     'required': ['pageId']})
async def getConfluencePage(credentials: Dict[str, str], pageId: str, bodyFormat: str = "storage"
) -> dict:
    """Fetch one page with its body."""
    _, site_url = await resolve_cloud_id(credentials)
    page = await get(credentials, f"/wiki/api/v2/pages/{pageId}", **{"body-format": bodyFormat})
    return _shape_page(page or {}, site_url, include_body=True)



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:attachment:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/pages/{pageId}/attachments
@server.tool(
    name='getConfluencePageAttachments',
    description="List the attachments on a Confluence page: filename, media type, size, and a link. createConfluenceAttachment adds one; deleteConfluenceAttachment removes one by its id.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:attachment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum attachments to return '
                                             '(1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}},
     'required': ['pageId']})
async def getConfluencePageAttachments(credentials: Dict[str, str],
    pageId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Attachments currently on this page."""
    _, site_url = await resolve_cloud_id(credentials)
    payload = await get(
        credentials,
        f"/wiki/api/v2/pages/{pageId}/attachments",
        limit=clamp(limit),
        cursor=cursor,
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "attachments": [_shape_attachment(a, site_url) for a in results if isinstance(a, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:hierarchical-content:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/pages/{pageId}/descendants
@server.tool(
    name='getConfluencePageDescendants',
    description="List the pages nested under a Confluence page, at any depth. Use this to walk a documentation tree from its root.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:hierarchical-content:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string',
                               'description': 'Numeric id of the parent page'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum descendants to return '
                                             '(1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'. It is null once "
                                              'there are no further results.'}},
     'required': ['pageId']})
async def getConfluencePageDescendants(credentials: Dict[str, str],
    pageId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Walk everything nested under one page."""
    _, site_url = await resolve_cloud_id(credentials)
    payload = await get(
        credentials,
        f"/wiki/api/v2/pages/{pageId}/descendants",
        limit=clamp(limit),
        cursor=cursor,
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "pages": [_shape_page(p, site_url) for p in results if isinstance(p, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:comment:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/pages/{pageId}/footer-comments
@server.tool(
    name='getConfluencePageFooterComments',
    description="List the footer comments on a Confluence page. These are the page-level discussion, distinct from inline comments anchored to selected text.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:comment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum comments to return '
                                             '(1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}},
     'required': ['pageId']})
async def getConfluencePageFooterComments(credentials: Dict[str, str],
    pageId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Page-level comments."""
    payload = await get(
        credentials,
        f"/wiki/api/v2/pages/{pageId}/footer-comments",
        limit=clamp(limit),
        cursor=cursor,
        **{"body-format": "storage"},
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "comments": [_shape_comment(c) for c in results if isinstance(c, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:comment:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/pages/{pageId}/inline-comments
@server.tool(
    name='getConfluencePageInlineComments',
    description="List the inline comments on a Confluence page. Each is anchored to a selection of the page text, unlike footer comments.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:comment:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum comments to return '
                                             '(1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}},
     'required': ['pageId']})
async def getConfluencePageInlineComments(credentials: Dict[str, str],
    pageId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Comments anchored to page text."""
    payload = await get(
        credentials,
        f"/wiki/api/v2/pages/{pageId}/inline-comments",
        limit=clamp(limit),
        cursor=cursor,
        **{"body-format": "storage"},
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "comments": [_shape_comment(c) for c in results if isinstance(c, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence_label'  required_scopes: ['read:page:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/pages/{pageId}/labels
@server.tool(
    name='getConfluencePageLabels',
    description="List the labels on a Confluence page. addConfluenceLabel adds one; removeConfluenceLabel removes one by name.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence_label',
    required_scopes=['read:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum labels to return (1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}},
     'required': ['pageId']})
async def getConfluencePageLabels(credentials: Dict[str, str],
    pageId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Labels currently on this page."""
    payload = await get(
        credentials, f"/wiki/api/v2/pages/{pageId}/labels", limit=clamp(limit), cursor=cursor
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "labels": [_shape_label(label) for label in results if isinstance(label, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:space:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/spaces
@server.tool(
    name='getConfluenceSpaces',
    description="List the Confluence spaces this user can see. Returns each space's numeric id and its key; other tools need the id, while people usually know the key.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:space:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'keys': {'type': 'string',
                             'description': '(Optional) Comma-separated space keys to '
                                            "filter by, e.g. 'DEV,DOCS'"},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum spaces to return (1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}}})
async def getConfluenceSpaces(credentials: Dict[str, str],
    keys: Optional[str] = None,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Spaces visible to the connected user."""
    _, site_url = await resolve_cloud_id(credentials)
    payload = await get(
        credentials, "/wiki/api/v2/spaces", keys=keys, limit=clamp(limit), cursor=cursor
    )
    results = (payload or {}).get("results") or []
    base = site_url.rstrip("/")
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "spaces": [
            {
                "id": s.get("id"),
                "key": s.get("key"),
                "name": s.get("name"),
                "type": s.get("type"),
                "status": s.get("status"),
                "url": f"{base}/wiki/spaces/{s.get('key')}" if base and s.get("key") else "",
            }
            for s in results
            if isinstance(s, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence_template'  required_scopes: ['read:confluence-content.summary']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/rest/api/template/page
@server.tool(
    name='getConfluenceTemplates',
    description="List the page templates available in a Confluence space (or globally, when spaceKey is omitted), each with its body so it can be passed straight to createConfluencePage.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence_template',
    required_scopes=['read:confluence-content.summary'],
)
@declare_schema({'type': 'object',
     'properties': {'spaceKey': {'type': 'string',
                                 'description': '(Optional) Restrict to templates in this '
                                                'space, by key'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum templates to return '
                                             '(1-100)',
                              'default': 25},
                    'start': {'type': 'integer',
                              'description': '(Optional) 0-based offset of the first '
                                             "result. Pass 'next_start' from the previous "
                                             'response; it is null once there are no '
                                             'further results.',
                              'default': 0}}})
async def getConfluenceTemplates(credentials: Dict[str, str],
    spaceKey: Optional[str] = None,
    limit: int = 25,
    start: int = 0,
) -> dict:
    """Page templates, with their body, ready for createConfluencePage."""
    size = max(1, min(limit or 25, 100))
    payload = await get(
        credentials,
        "/wiki/rest/api/template/page",
        spaceKey=spaceKey,
        expand="body.storage",
        limit=size,
        start=start,
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "start": start,
        "next_start": start + len(results) if len(results) == size else None,
        "templates": [
            {
                "id": t.get("templateId") or t.get("id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "body": ((t.get("body") or {}).get("storage") or {}).get("value"),
            }
            for t in results
            if isinstance(t, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_confluence'  required_scopes: ['read:page:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/api/v2/spaces/{spaceId}/pages
@server.tool(
    name='getPagesInConfluenceSpace',
    description="List the pages in a Confluence space. Takes the space's numeric id, which getConfluenceSpaces returns; a space KEY will not work here.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_confluence',
    required_scopes=['read:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'spaceId': {'type': 'string',
                                'description': 'Numeric space id from getConfluenceSpaces'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum pages to return (1-250)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': '(Optional) Cursor from a previous '
                                              "response's 'next_cursor'"}},
     'required': ['spaceId']})
async def getPagesInConfluenceSpace(credentials: Dict[str, str],
    spaceId: str,
    limit: int = 25,
    cursor: Optional[str] = None,
) -> dict:
    """Pages within one space."""
    _, site_url = await resolve_cloud_id(credentials)
    payload = await get(
        credentials,
        f"/wiki/api/v2/spaces/{spaceId}/pages",
        limit=clamp(limit),
        cursor=cursor,
    )
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "next_cursor": next_cursor(payload),
        "pages": [_shape_page(p, site_url) for p in results if isinstance(p, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence_label'  required_scopes: ['write:confluence-content']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /wiki/rest/api/content/{pageId}/label/{label}
@server.tool(
    name='removeConfluenceLabel',
    description="Remove a label from a Confluence page by name.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence_label',
    required_scopes=['write:confluence-content'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'label': {'type': 'string', 'description': 'Label name to remove'}},
     'required': ['pageId', 'label']})
async def removeConfluenceLabel(credentials: Dict[str, str], pageId: str, label: str
) -> dict:
    """Detach one label."""
    await delete(credentials, f"/wiki/rest/api/content/{pageId}/label/{label}")
    return {"success": True, "page_id": pageId, "label": label}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'search_confluence'  required_scopes: ['search:confluence']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /wiki/rest/api/search
@server.tool(
    name='searchConfluenceUsingCql',
    description="Search Confluence content with a CQL query, for example 'space = DEV AND type = page AND text ~ \"onboarding\"'. Returns a page of results plus an offset for the next page.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='search_confluence',
    required_scopes=['search:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'cql': {'type': 'string',
                            'description': 'CQL query string. Quote values containing '
                                           'spaces.'},
                    'limit': {'type': 'integer',
                              'description': '(Optional) Maximum results to return (1-250)',
                              'default': 25},
                    'start': {'type': 'integer',
                              'description': '(Optional) 0-based offset of the first '
                                             "result. Pass 'next_start' from the previous "
                                             'response; it is null once there are no '
                                             'further results.',
                              'default': 0}},
     'required': ['cql']})
async def searchConfluenceUsingCql(credentials: Dict[str, str],
    cql: str,
    limit: int = 25,
    start: int = 0,
) -> dict:
    """Run one CQL query."""
    _, site_url = await resolve_cloud_id(credentials)
    size = clamp(limit)
    payload = await get(credentials, "/wiki/rest/api/search", cql=cql, limit=size, start=start)
    results = (payload or {}).get("results") or []
    return {
        "count": len(results),
        "start": start,
        # v1 omits a reliable total on this endpoint, so a full page is taken
        # as "there may be more". Claiming a total we do not have would be
        # worse than making the caller ask again.
        "next_start": start + len(results) if len(results) == size else None,
        "results": [_shape_result(r, site_url) for r in results if isinstance(r, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_confluence'  required_scopes: ['write:page:confluence']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PUT /wiki/api/v2/pages/{id} replaces title and body
# at version+1. The handler's own comment calls the omit-body path
# "a destructive way to answer 'just change the title'".
# Evidence: PUT /wiki/api/v2/pages/{id}. Overwrites the page body, but
# Confluence versions every update, so nothing is lost: destructiveHint
# stays False.
@server.tool(
    name='updateConfluencePage',
    description="Update a Confluence page's title and/or body. Confluence requires the next version number, so the current version is read first and incremented. Anything not supplied is carried forward unchanged. Send at least one of title or body.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_confluence',
    required_scopes=['write:page:confluence'],
)
@declare_schema({'type': 'object',
     'properties': {'pageId': {'type': 'string', 'description': 'Numeric page id'},
                    'title': {'type': 'string',
                              'description': '(Optional) New title. Keeps the existing one '
                                             'if omitted.'},
                    'body': {'type': 'string',
                             'description': '(Optional) New body, replacing the existing '
                                            'one entirely. Keeps the existing body if '
                                            'omitted.'},
                    'bodyFormat': {'type': 'string',
                                   'enum': ['storage', 'atlas_doc_format', 'wiki'],
                                   'description': "(Optional) Format of 'body'",
                                   'default': 'storage'},
                    'versionMessage': {'type': 'string',
                                       'description': '(Optional) Note recorded against '
                                                      'this version'}},
     'required': ['pageId'],
     'anyOf': [{'required': ['title']}, {'required': ['body']}]})
async def updateConfluencePage(credentials: Dict[str, str],
    pageId: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    bodyFormat: str = "storage",
    versionMessage: Optional[str] = None,
) -> dict:
    """Update a page, carrying forward whatever was not supplied."""
    if title is None and body is None:
        raise ValueError("Provide 'title', 'body', or both. Nothing to update.")

    _, site_url = await resolve_cloud_id(credentials)
    # What v2 accepts on a write is not what it returns on a read: the write
    # side takes storage/atlas_doc_format/wiki, the read side has no 'wiki'
    # at all. Reading back with body-format=wiki therefore yields no body,
    # and carrying that forward would blank the page on a title-only edit.
    read_format = _READ_FORMAT.get(bodyFormat, bodyFormat)
    current = await get(
        credentials, f"/wiki/api/v2/pages/{pageId}", **{"body-format": read_format}
    )
    current = current or {}
    current_version = (current.get("version") or {}).get("number", 1)

    if body is not None:
        new_body = {"representation": bodyFormat, "value": body}
    else:
        # Carry the existing body forward. Omitting it would blank the page,
        # which is a destructive way to answer "just change the title". The
        # representation has to be the one actually read, not the one the
        # caller named, or the value and its declared format disagree.
        existing = (current.get("body") or {}).get(read_format) or {}
        new_body = {"representation": read_format, "value": existing.get("value", "")}

    payload = {
        "id": pageId,
        "status": current.get("status", "current"),
        "title": title if title is not None else current.get("title"),
        "body": new_body,
        "version": {"number": current_version + 1, "message": versionMessage or ""},
    }
    updated = await put(credentials, f"/wiki/api/v2/pages/{pageId}", payload)
    return {
        "id": (updated or {}).get("id", pageId),
        "title": (updated or {}).get("title"),
        "version": ((updated or {}).get("version") or {}).get("number"),
        "url": page_url(site_url, updated or {}),
    }
