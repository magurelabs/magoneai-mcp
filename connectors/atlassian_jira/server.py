"""The atlassian-jira connector.

Ported from the v1 mixin form by a codemod.

40 tools, 23 of them proposed read-only. Every input schema
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

SERVER_NAME = "atlassian-jira"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

# addJiraIssueAttachment takes up to 10 files per call as base64, which
# inflates them by a third: 50 MB of files is about 67 MB of request body plus
# the JSON-RPC envelope. The framework's 10 MiB default refused any call whose
# files added up to more than about 7.5 MB, before the tool ever saw it.
# v1 ran this connector at 70 MB. MCP_MAX_BODY_BYTES still overrides it.
MAX_REQUEST_BODY_BYTES = 70 * 1024 * 1024

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
    max_body_bytes=MAX_REQUEST_BODY_BYTES,
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

import sys
from pathlib import Path
from typing import Any, Dict, Optional
import httpx
from typing import Dict, List, Optional
from ._shared.atlassian_auth import (
    JIRA_SOFTWARE_READ_BOARD,
    JIRA_SOFTWARE_READ_EPIC,
    JIRA_SOFTWARE_READ_SPRINT,
    JIRA_SOFTWARE_WRITE,
)
from ._shared.atlassian_auth import JIRA_MANAGE_PROJECT, JIRA_READ
from typing import Dict, Optional
from ._shared.atlassian_auth import JIRA_READ
from ._shared.atlassian_auth import JIRA_SEARCH
import base64
import binascii
import re
from ._shared.atlassian_auth import JIRA_WRITE
from typing import Dict, Iterable, List
from typing import Any, Dict, Tuple


PRODUCT = "Jira"

async def resolve_cloud_id(credentials: Dict[str, str]):
    """(cloud_id, site_url) for the Jira site this connection targets."""
    return await _resolve_cloud_id(credentials, PRODUCT)

def _error_message(response: httpx.Response, method: str, path: str) -> str:
    """Jira's own message where it has one, so the caller can act on it."""
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        messages = payload.get("errorMessages")
        if isinstance(messages, list) and messages:
            detail = "; ".join(str(m) for m in messages if m)
        errors = payload.get("errors")
        if not detail and isinstance(errors, dict) and errors:
            detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        if not detail:
            detail = str(payload.get("message") or "")
    if not detail:
        detail = (response.text or "")[:300]

    hint = ""
    if response.status_code in (401, 403):
        hint = (
            " The connection may lack the scope this endpoint needs; "
            "api.atlassian.com answers 401 for a scope that was never granted."
        )
    elif response.status_code == 429:
        hint = " Jira is rate limiting; retry after a pause."
    return (
        f"Jira API error {response.status_code} on {method} {path}: "
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
    """One authenticated Jira REST call. Returns parsed JSON, or None for 204.

    `path` is relative to the Jira API root, e.g. "/rest/api/3/issue/ABC-1".
    """
    cloud_id, _ = await resolve_cloud_id(credentials)
    url = f"{GATEWAY}/ex/jira/{cloud_id}{path}"
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
            "user can see it: Jira answers 404 rather than 403 for issues the "
            "caller has no permission on."
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
    files: List[Tuple[str, bytes, Optional[str]]],
) -> Any:
    """Multipart upload, for the one endpoint that takes files rather than JSON.

    *files* is ``(file_name, content, mime_type)`` per file. Jira accepts any
    number of ``file`` parts in one request and returns one entry per file, so a
    batch is one round trip and either all of it lands or none does.

    Jira's attachment endpoints reject a JSON content-type outright and require
    ``X-Atlassian-Token: no-check`` to bypass their XSRF check, so this bypasses
    ``request()`` rather than bending it to send both shapes.
    """
    cloud_id, _ = await resolve_cloud_id(credentials)
    url = f"{GATEWAY}/ex/jira/{cloud_id}{path}"
    headers = {**auth_headers(credentials), "X-Atlassian-Token": "no-check"}
    parts = [
        ("file", (name, content, mime_type or "application/octet-stream"))
        for name, content, mime_type in files
    ]

    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
        response = await client.post(url, files=parts, headers=headers)

    if response.status_code >= 400:
        raise ValueError(_error_message(response, "POST", path))
    try:
        return response.json()
    except ValueError:
        return None

def issue_url(site_url: str, key: str) -> str:
    """A link a human can actually open, built from the site rather than the gateway."""
    return f"{site_url.rstrip('/')}/browse/{key}" if site_url and key else ""

READ_GROUP = "read_jira_software"

WRITE_GROUP = "write_jira_software"


_ISSUE_FIELDS = (
    "summary,status,assignee,reporter,priority,issuetype,project,created,"
    "updated,duedate,labels,parent,issuelinks"
)

def _person(field: Optional[dict]) -> Optional[dict]:
    if not isinstance(field, dict):
        return None
    return {
        "account_id": field.get("accountId"),
        "display_name": field.get("displayName"),
        "email": field.get("emailAddress"),
    }

def _shape_links(fields: dict) -> list:
    """Issue-to-issue links, each with the id removeJiraIssueLink needs.

    Without this the link id is unreachable: it appears in no other response,
    and getJiraIssueRemoteIssueLinks returns EXTERNAL links, which are a
    different thing with different ids.
    """
    links = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        # A link points either inward or outward, never both, and which one it
        # is determines what the relationship actually means.
        if link.get("outwardIssue"):
            direction, other = "outward", link["outwardIssue"]
            relationship = link_type.get("outward")
        elif link.get("inwardIssue"):
            direction, other = "inward", link["inwardIssue"]
            relationship = link_type.get("inward")
        else:
            continue
        links.append(
            {
                "link_id": link.get("id"),
                "type": link_type.get("name"),
                "direction": direction,
                "relationship": relationship,
                "issue_key": (other or {}).get("key"),
            }
        )
    return links

def _shape_issue(issue: dict, site_url: str = "") -> dict:
    """Flatten a Jira issue into the shape every tool here returns."""
    fields = issue.get("fields") or {}
    key = issue.get("key")
    return {
        "key": key,
        "id": issue.get("id"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "project": (fields.get("project") or {}).get("key"),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "labels": fields.get("labels") or [],
        "parent": (issue.get("fields", {}).get("parent") or {}).get("key"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "due_date": fields.get("duedate"),
        "links": _shape_links(fields),
        "url": issue_url(site_url, key),
    }


_MAX_RESULTS = 100


def to_adf(text: str) -> Dict[str, Any]:
    """Wrap plain text as an ADF document.

    Each blank-line-separated block becomes a paragraph, so multi-paragraph text
    survives instead of collapsing into one run-on line.
    """
    blocks = [block for block in (text or "").split("\n\n") if block.strip()]
    if not blocks:
        blocks = [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": block}]} for block in blocks
        ],
    }

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
# Attachments are minutes of upload, not seconds: a platform-resolved file can be
# tens of megabytes and the read/write budget above is sized for JSON.
UPLOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=5.0)

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
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PUT .../comment/{commentId} when commentId is
# given, replacing an existing comment's body.
# Evidence: PUT /rest/api/3/issue/{issueIdOrKey}/comment/{commentId}
@server.tool(
    name='addCommentToJiraIssue',
    description="Add a comment to a Jira issue, or update an existing comment when commentId is given.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'body': {'type': 'string', 'description': 'Comment text'},
                    'commentId': {'type': 'string',
                                  'description': '(Optional) Id of an existing comment to '
                                                 'update instead of adding a new one'}},
     'required': ['issueIdOrKey', 'body']})
async def addCommentToJiraIssue(credentials: Dict[str, str],
    issueIdOrKey: str,
    body: str,
    commentId: Optional[str] = None,
) -> dict:
    """Add or update one comment."""
    payload = {"body": to_adf(body)}
    if commentId:
        result = await put(
            credentials, f"/rest/api/3/issue/{issueIdOrKey}/comment/{commentId}", payload
        )
        action = "updated"
    else:
        result = await post(credentials, f"/rest/api/3/issue/{issueIdOrKey}/comment", payload)
        action = "created"
    return {
        "success": True,
        "action": action,
        "comment_id": (result or {}).get("id", commentId),
        "issue": issueIdOrKey,
    }



MAX_ATTACHMENTS = 10

# Wrappings producers commonly put around base64: a data: URI prefix and
# MIME-style line breaks. Stripped before the strict decode, not tolerated by it.
_DATA_URI_RE = re.compile(r"^data:[\w.+-]*/?[\w.+-]*(;[\w.+-]+=[\w.+-]+)*;base64,", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def _decode_base64(value: str, label: str) -> bytes:
    text = value.strip()
    match = _DATA_URI_RE.match(text)
    if match:
        text = text[match.end() :]
    text = _WHITESPACE_RE.sub("", text)
    try:
        content = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label}: content is not valid base64: {exc}") from exc
    if not content:
        raise ValueError(f"{label}: content is empty")
    return content


def _attachment_file(item: Any, position: int) -> Tuple[str, bytes, Optional[str]]:
    """Turn one resolved ``attachments`` item into ``(name, bytes, mime)``.

    A calling platform rewrites a file reference into the inline shape before
    the call arrives, so this connector never resolves one and holds no
    platform credentials - which is why the capability belongs in a public
    connector. A reference that reaches us un-rewritten means no
    resolver ran (a direct MCP call), and there are no bytes to upload.
    """
    label = f"attachments[{position}]"
    if not isinstance(item, dict) or "content_base64" not in item:
        raise ValueError(
            f"{label} is an unresolved file reference; it must arrive as "
            "{filename, content_base64, content_type}. A file reference is resolved "
            "only by a host that supports it, such as MagOneAI; when calling the tool "
            "directly, send the file's bytes inline in that form."
        )
    content_b64 = item.get("content_base64")
    if not isinstance(content_b64, str) or not content_b64.strip():
        raise ValueError(f"{label} is missing content_base64")
    name = item.get("filename")
    name = name if isinstance(name, str) and name.strip() else "attachment"
    mime = item.get("content_type")
    return name, _decode_base64(content_b64, label), mime if isinstance(mime, str) else None


# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/issue/{key}/attachments as multipart.
@server.tool(
    name='addJiraIssueAttachment',
    description=(
        "Attach one or more files to a Jira issue. Pass files uploaded to the "
        "workflow (e.g. a Start node file or image field) or produced by "
        "another tool through `attachments`, as their platform file_id or "
        "storage key; the platform fetches the bytes from its storage, so "
        "large files work. fileName + fileContentBase64 is only for a direct "
        "caller that already holds the bytes."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'attachments': {'type': 'array',
                                    'items': {'anyOf': [{'type': 'string',
                                                         'description': 'Platform file_id '
                                                                        '(UUID) or storage '
                                                                        'key'},
                                                        {'type': 'object',
                                                         'properties': {'file_id': {'type': 'string'},
                                                                        'filename': {'type': 'string'}},
                                                         'required': ['file_id']},
                                                        {'type': 'object',
                                                         'properties': {'filename': {'type': 'string'},
                                                                        'content_base64': {'type': 'string'},
                                                                        'content_type': {'type': 'string'}},
                                                         'required': ['content_base64']}]},
                                    'maxItems': 10,
                                    'description': 'Files to attach (max 10). Pass a '
                                                   'platform file reference: the file_id '
                                                   'UUID of a file uploaded to the '
                                                   'workflow (e.g. a Start node file or '
                                                   'image field) or an exports/... storage '
                                                   'key from a tool such as '
                                                   'render_document, either as a plain '
                                                   'string or as {"file_id": '
                                                   '"<uuid-or-key>"}. The platform reads '
                                                   'the file from its own storage and '
                                                   'replaces the reference with {filename, '
                                                   'content_base64, content_type} before '
                                                   'this tool runs, so the file never '
                                                   'passes through the model and its size '
                                                   'is not limited by it. Add a "filename" '
                                                   'key next to file_id to rename the '
                                                   'attachment. DO NOT ENCODE '
                                                   'content_base64 YOURSELF: a '
                                                   'hand-encoded file arrives silently '
                                                   'corrupt. If you hold no file_id or '
                                                   'storage key, do not improvise one.'},
                    'fileName': {'type': 'string',
                                 'description': '(Optional) Name the attachment is saved '
                                                "as, e.g. 'report.pdf'. Only with "
                                                'fileContentBase64.'},
                    'fileContentBase64': {'type': 'string',
                                          'description': '(Optional) File content, '
                                                         'base64-encoded, for a caller '
                                                         'that already holds the bytes. '
                                                         'Prefer `attachments`.'},
                    'mimeType': {'type': 'string',
                                 'description': '(Optional) MIME type, e.g. '
                                                "'application/pdf'. Defaults to "
                                                'application/octet-stream when omitted.'}},
     'required': ['issueIdOrKey']})
async def addJiraIssueAttachment(credentials: Dict[str, str],
    issueIdOrKey: str,
    attachments: Optional[List[Any]] = None,
    fileName: Optional[str] = None,
    fileContentBase64: Optional[str] = None,
    mimeType: Optional[str] = None,
) -> dict:
    """Upload every file in one multipart request."""
    items = attachments or []
    if len(items) > MAX_ATTACHMENTS:
        raise ValueError(f"At most {MAX_ATTACHMENTS} attachments per call")

    # Everything is decoded before the request, so one bad item fails the call
    # without leaving the rest half-attached to the issue.
    files = [_attachment_file(item, i) for i, item in enumerate(items)]
    if fileContentBase64:
        if not fileName:
            raise ValueError("fileName is required with fileContentBase64")
        files.append((fileName, _decode_base64(fileContentBase64, "fileContentBase64"), mimeType))
    if not files:
        raise ValueError("Nothing to attach: pass attachments (a file_id) or fileContentBase64")

    result = await upload_attachment(
        credentials, f"/rest/api/3/issue/{issueIdOrKey}/attachments", files
    )
    # This endpoint returns a list, one entry per attachment created.
    created = [
        {"attachment_id": a.get("id"), "filename": a.get("filename"), "size": a.get("size")}
        for a in (result if isinstance(result, list) else [])
    ]
    first = created[0] if created else {}
    return {
        "success": True,
        "issue": issueIdOrKey,
        "count": len(created),
        "attachments": created,
        # Kept so a caller written against the single-file shape still works.
        "attachment_id": first.get("attachment_id"),
        "filename": first.get("filename"),
        "size": first.get("size"),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/issue/{issueIdOrKey}/watchers
@server.tool(
    name='addJiraIssueWatcher',
    description="Add a user as a watcher on a Jira issue, so they are notified of changes. Takes an account id: use lookupJiraAccountId to turn a name or email into one.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'accountId': {'type': 'string',
                                  'description': 'Account id of the user to add as a '
                                                 'watcher'}},
     'required': ['issueIdOrKey', 'accountId']})
async def addJiraIssueWatcher(credentials: Dict[str, str], issueIdOrKey: str, accountId: str
) -> dict:
    """Start watching an issue on someone's behalf."""
    # This endpoint takes the bare account id as the JSON body, not an
    # object. Wrapping it in {"accountId": ...} is rejected.
    await post(credentials, f"/rest/api/3/issue/{issueIdOrKey}/watchers", accountId)
    return {"success": True, "issue": issueIdOrKey, "account_id": accountId}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/issue/{issueIdOrKey}/worklog
@server.tool(
    name='addWorklogToJiraIssue',
    description="Log time against a Jira issue. timeSpent uses Jira's own duration wording, for example '3h 30m', '1d', '45m'.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'timeSpent': {'type': 'string',
                                  'description': "Duration in Jira format, e.g. '3h 30m', "
                                                 "'1d', '45m'"},
                    'comment': {'type': 'string',
                                'description': '(Optional) What the time was spent on'},
                    'started': {'type': 'string',
                                'description': '(Optional) When the work started, ISO 8601 '
                                               'with offset, e.g. '
                                               "'2026-08-21T09:00:00.000+0000'. Defaults "
                                               'to now.'}},
     'required': ['issueIdOrKey', 'timeSpent']})
async def addWorklogToJiraIssue(credentials: Dict[str, str],
    issueIdOrKey: str,
    timeSpent: str,
    comment: Optional[str] = None,
    started: Optional[str] = None,
) -> dict:
    """Record a worklog entry."""
    payload: Dict[str, Any] = {"timeSpent": timeSpent}
    if comment:
        payload["comment"] = to_adf(comment)
    if started:
        payload["started"] = started

    result = await post(credentials, f"/rest/api/3/issue/{issueIdOrKey}/worklog", payload)
    return {
        "success": True,
        "worklog_id": (result or {}).get("id"),
        "issue": issueIdOrKey,
        "time_spent": (result or {}).get("timeSpent", timeSpent),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PUT .../assignee: overwrites the current assignee, and
# writes null when unassign is set.
# Evidence: PUT /rest/api/3/issue/{issueIdOrKey}/assignee
@server.tool(
    name='assignJiraIssue',
    description="Assign a Jira issue to a user, or unassign it. Takes an account id: use lookupJiraAccountId to turn a name or email into one. Send either accountId or unassign, not neither.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'accountId': {'type': 'string',
                                  'description': '(Optional) Account id to assign to'},
                    'unassign': {'type': 'boolean',
                                 'description': '(Optional) Remove the current assignee '
                                                'instead of setting a new one',
                                 'default': False}},
     'required': ['issueIdOrKey'],
     'anyOf': [{'required': ['accountId']}, {'required': ['unassign']}]})
async def assignJiraIssue(credentials: Dict[str, str],
    issueIdOrKey: str,
    accountId: Optional[str] = None,
    unassign: bool = False,
) -> dict:
    """Set or clear the assignee."""
    if not accountId and not unassign:
        raise ValueError("Provide 'accountId' to assign, or 'unassign: true' to clear it.")

    target = None if unassign else accountId
    await put(credentials, f"/rest/api/3/issue/{issueIdOrKey}/assignee", {"accountId": target})
    return {"success": True, "issue": issueIdOrKey, "assignee_account_id": target}



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
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/issue
@server.tool(
    name='createJiraIssue',
    description="Create a Jira issue in a project. When the project's required fields are not known, call getJiraIssueTypeMetaWithFields first: a project can require fields this tool does not name explicitly, and those go in additionalFields.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'projectKey': {'type': 'string',
                                   'description': "Project key, e.g. 'PROJ'"},
                    'summary': {'type': 'string', 'description': 'Issue title'},
                    'issueTypeName': {'type': 'string',
                                      'description': "Issue type name, e.g. 'Task', 'Bug', "
                                                     "'Story'"},
                    'description': {'type': 'string',
                                    'description': '(Optional) Issue description as plain '
                                                   'text'},
                    'assigneeAccountId': {'type': 'string',
                                          'description': '(Optional) Account id to assign '
                                                         'to. Use lookupJiraAccountId to '
                                                         'turn a name or email into one.'},
                    'additionalFields': {'type': 'object',
                                         'description': '(Optional) Any other fields, in '
                                                        "Jira's own field format, e.g. "
                                                        '{"labels": ["urgent"], '
                                                        '"priority": {"name": "High"}}',
                                         'additionalProperties': True}},
     'required': ['projectKey', 'summary', 'issueTypeName']})
async def createJiraIssue(credentials: Dict[str, str],
    projectKey: str,
    summary: str,
    issueTypeName: str,
    description: Optional[str] = None,
    assigneeAccountId: Optional[str] = None,
    additionalFields: Optional[Dict[str, Any]] = None,
) -> dict:
    """Create one issue."""
    fields: Dict[str, Any] = {
        "project": {"key": projectKey},
        "summary": summary,
        "issuetype": {"name": issueTypeName},
    }
    if description:
        fields["description"] = to_adf(description)
    if assigneeAccountId:
        fields["assignee"] = {"accountId": assigneeAccountId}
    if additionalFields:
        fields.update(additionalFields)

    created = await post(credentials, "/rest/api/3/issue", {"fields": fields})
    _, site_url = await resolve_cloud_id(credentials)
    key = (created or {}).get("key")
    return {
        "key": key,
        "id": (created or {}).get("id"),
        "url": issue_url(site_url, key),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/issueLink
@server.tool(
    name='createJiraIssueLink',
    description="Link two Jira issues, for example 'PROJ-1 blocks PROJ-2'. The link type name must be one getIssueLinkTypes returns, and direction matters: inwardIssue is the one the relationship points at.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'linkTypeName': {'type': 'string',
                                     'description': 'Link type name from '
                                                    "getIssueLinkTypes, e.g. 'Blocks'"},
                    'inwardIssueKey': {'type': 'string',
                                       'description': 'Key of the inward issue. For a '
                                                      "'Blocks' link this is the issue "
                                                      'that IS blocked.'},
                    'outwardIssueKey': {'type': 'string',
                                        'description': 'Key of the outward issue. For a '
                                                       "'Blocks' link this is the issue "
                                                       'doing the blocking.'},
                    'comment': {'type': 'string',
                                'description': '(Optional) Comment to add alongside the '
                                               'link'}},
     'required': ['linkTypeName', 'inwardIssueKey', 'outwardIssueKey']})
async def createJiraIssueLink(credentials: Dict[str, str],
    linkTypeName: str,
    inwardIssueKey: str,
    outwardIssueKey: str,
    comment: Optional[str] = None,
) -> dict:
    """Link two issues."""
    payload: Dict[str, Any] = {
        "type": {"name": linkTypeName},
        "inwardIssue": {"key": inwardIssueKey},
        "outwardIssue": {"key": outwardIssueKey},
    }
    if comment:
        payload["comment"] = {"body": to_adf(comment)}

    await post(credentials, "/rest/api/3/issueLink", payload)
    return {
        "success": True,
        "link_type": linkTypeName,
        "inward_issue": inwardIssueKey,
        "outward_issue": outwardIssueKey,
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'manage_jira_project'  required_scopes: ['manage:jira-project']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/component
@server.tool(
    name='createJiraProjectComponent',
    description="Create a component in a Jira project, so issues can be assigned to it. leadAccountId sets who owns it; use lookupJiraAccountId to turn a name or email into one.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='manage_jira_project',
    required_scopes=['manage:jira-project'],
)
@declare_schema({'type': 'object',
     'properties': {'projectKey': {'type': 'string',
                                   'description': "Project key, e.g. 'PROJ'"},
                    'name': {'type': 'string',
                             'description': "Component name, e.g. 'Backend'"},
                    'description': {'type': 'string',
                                    'description': '(Optional) Component description'},
                    'leadAccountId': {'type': 'string',
                                      'description': '(Optional) Account id of the '
                                                     "component's owner"}},
     'required': ['projectKey', 'name']})
async def createJiraProjectComponent(credentials: Dict[str, str],
    projectKey: str,
    name: str,
    description: Optional[str] = None,
    leadAccountId: Optional[str] = None,
) -> dict:
    """Create one component."""
    payload: Dict[str, Any] = {"project": projectKey, "name": name}
    if description:
        payload["description"] = description
    if leadAccountId:
        payload["leadAccountId"] = leadAccountId

    created = await post(credentials, "/rest/api/3/component", payload)
    return {
        "id": (created or {}).get("id"),
        "name": (created or {}).get("name", name),
        "project": projectKey,
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'manage_jira_project'  required_scopes: ['manage:jira-project']
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /rest/api/3/version
@server.tool(
    name='createJiraProjectVersion',
    description="Create a version (release) in a Jira project, so issues can be assigned to it via fixVersions.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='manage_jira_project',
    required_scopes=['manage:jira-project'],
)
@declare_schema({'type': 'object',
     'properties': {'projectKey': {'type': 'string',
                                   'description': "Project key, e.g. 'PROJ'"},
                    'name': {'type': 'string', 'description': "Version name, e.g. '2.4.0'"},
                    'description': {'type': 'string',
                                    'description': '(Optional) Version description'},
                    'releaseDate': {'type': 'string',
                                    'description': '(Optional) Planned release date, '
                                                   "'YYYY-MM-DD'"}},
     'required': ['projectKey', 'name']})
async def createJiraProjectVersion(credentials: Dict[str, str],
    projectKey: str,
    name: str,
    description: Optional[str] = None,
    releaseDate: Optional[str] = None,
) -> dict:
    """Create one version."""
    payload: Dict[str, Any] = {"project": projectKey, "name": name}
    if description:
        payload["description"] = description
    if releaseDate:
        payload["releaseDate"] = releaseDate

    created = await post(credentials, "/rest/api/3/version", payload)
    return {
        "id": (created or {}).get("id"),
        "name": (created or {}).get("name", name),
        "project": projectKey,
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /rest/api/3/issue/{issueIdOrKey}
@server.tool(
    name='deleteJiraIssue',
    description="Permanently delete a Jira issue. This cannot be undone. Jira rejects the call rather than leaving orphaned subtasks when deleteSubtasks is false and subtasks exist.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'deleteSubtasks': {'type': 'boolean',
                                       'description': "(Optional) Also delete the issue's "
                                                      'subtasks. Off by default, matching '
                                                      "Jira's own default: widening one "
                                                      'delete into several is something to '
                                                      'ask for, not to inherit.',
                                       'default': False}},
     'required': ['issueIdOrKey']})
async def deleteJiraIssue(credentials: Dict[str, str], issueIdOrKey: str, deleteSubtasks: bool = False
) -> dict:
    """Permanently delete one issue."""
    await delete(
        credentials,
        f"/rest/api/3/issue/{issueIdOrKey}",
        deleteSubtasks="true" if deleteSubtasks else "false",
    )
    return {"success": True, "issue": issueIdOrKey}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /rest/api/3/attachment/{attachmentId}
@server.tool(
    name='deleteJiraIssueAttachment',
    description="Delete an attachment from a Jira issue by its attachment id.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'attachmentId': {'type': 'string',
                                     'description': 'Attachment id, from '
                                                    'getJiraIssueAttachments'}},
     'required': ['attachmentId']})
async def deleteJiraIssueAttachment(credentials: Dict[str, str], attachmentId: str
) -> dict:
    """Remove one attachment."""
    await delete(credentials, f"/rest/api/3/attachment/{attachmentId}")
    return {"success": True, "attachment_id": attachmentId}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PUT /rest/api/3/issue/{key}. Its own schema says the
# description "Replaces the existing description entirely".
# Evidence: PUT /rest/api/3/issue/{issueIdOrKey}
@server.tool(
    name='editJiraIssue',
    description="Update fields on an existing Jira issue. Only the fields supplied are changed; everything else is left as it is. Send at least one of summary, description, assigneeAccountId or additionalFields.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'summary': {'type': 'string', 'description': '(Optional) New title'},
                    'description': {'type': 'string',
                                    'description': '(Optional) New description as plain '
                                                   'text. Replaces the existing '
                                                   'description entirely.'},
                    'assigneeAccountId': {'type': 'string',
                                          'description': '(Optional) Account id to assign '
                                                         'to'},
                    'additionalFields': {'type': 'object',
                                         'description': '(Optional) Any other fields in '
                                                        "Jira's field format",
                                         'additionalProperties': True}},
     'required': ['issueIdOrKey'],
     'anyOf': [{'required': ['summary']},
               {'required': ['description']},
               {'required': ['assigneeAccountId']},
               {'required': ['additionalFields']}]})
async def editJiraIssue(credentials: Dict[str, str],
    issueIdOrKey: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    assigneeAccountId: Optional[str] = None,
    additionalFields: Optional[Dict[str, Any]] = None,
) -> dict:
    """Patch an issue's fields."""
    fields: Dict[str, Any] = {}
    if summary is not None:
        fields["summary"] = summary
    if description is not None:
        fields["description"] = to_adf(description)
    if assigneeAccountId is not None:
        fields["assignee"] = {"accountId": assigneeAccountId}
    if additionalFields:
        fields.update(additionalFields)
    if not fields:
        raise ValueError(
            "Nothing to update. Provide at least one of summary, description, "
            "assigneeAccountId, or additionalFields."
        )

    await put(credentials, f"/rest/api/3/issue/{issueIdOrKey}", {"fields": fields})
    return {
        "success": True,
        "issue": issueIdOrKey,
        "updated_fields": sorted(fields),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'common'  required_scopes: ['read:me']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET https://api.atlassian.com/oauth/token/accessible-resources.
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
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issueLinkType
@server.tool(
    name='getIssueLinkTypes',
    description="List the issue link types this site defines (for example 'Blocks', 'Relates'), with the inward and outward wording of each.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object', 'properties': {}})
async def getIssueLinkTypes(credentials: Dict[str, str]) -> dict:
    """Link types available on this site."""
    payload = await get(credentials, "/rest/api/3/issueLinkType")
    types = (payload or {}).get("issueLinkTypes") or []
    return {
        "count": len(types),
        "link_types": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "inward": t.get("inward"),
                "outward": t.get("outward"),
            }
            for t in types
            if isinstance(t, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira_software'  required_scopes: ['read:board-scope:jira-software', 'read:project:jira']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/agile/1.0/board
@server.tool(
    name='getJiraBoardsForProject',
    description="List the Scrum/Kanban boards for a Jira project.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira_software',
    required_scopes=['read:board-scope:jira-software', 'read:project:jira'],
)
@declare_schema({'type': 'object',
     'properties': {'projectKeyOrId': {'type': 'string',
                                       'description': 'Project key or numeric id'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum boards to return '
                                                  '(1-50)',
                                   'default': 50},
                    'startAt': {'type': 'integer',
                                'description': '(Optional) 0-based offset. Pass '
                                               "'next_start' from the previous response; "
                                               'it is null once there are no further '
                                               'results.',
                                'default': 0}},
     'required': ['projectKeyOrId']})
async def getJiraBoardsForProject(credentials: Dict[str, str],
    projectKeyOrId: str,
    maxResults: int = 50,
    startAt: int = 0,
) -> dict:
    """Boards scoped to one project."""
    limit = max(1, min(maxResults or 50, 50))
    payload = await get(
        credentials,
        "/rest/agile/1.0/board",
        projectKeyOrId=projectKeyOrId,
        maxResults=limit,
        startAt=startAt,
    )
    values = (payload or {}).get("values") or []
    return {
        "count": len(values),
        "start_at": startAt,
        "next_start": None if (payload or {}).get("isLast", True) else startAt + len(values),
        "boards": [
            {"id": b.get("id"), "name": b.get("name"), "type": b.get("type")}
            for b in values
            if isinstance(b, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira_software'  required_scopes: ['read:epic:jira-software', 'read:issue-details:jira', 'read:jql:jira']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/agile/1.0/epic/{epicIdOrKey}/issue
@server.tool(
    name='getJiraEpicIssues',
    description="List the issues under a Jira epic.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira_software',
    required_scopes=['read:epic:jira-software', 'read:issue-details:jira', 'read:jql:jira'],
)
@declare_schema({'type': 'object',
     'properties': {'epicIdOrKey': {'type': 'string',
                                    'description': "Epic's issue key or numeric id"},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum issues to return '
                                                  '(1-100)',
                                   'default': 50},
                    'startAt': {'type': 'integer',
                                'description': '(Optional) 0-based offset. Pass '
                                               "'next_start' from the previous response; "
                                               'it is null once there are no further '
                                               'results.',
                                'default': 0}},
     'required': ['epicIdOrKey']})
async def getJiraEpicIssues(credentials: Dict[str, str],
    epicIdOrKey: str,
    maxResults: int = 50,
    startAt: int = 0,
) -> dict:
    """Issues under one epic."""
    _, site_url = await resolve_cloud_id(credentials)
    limit = max(1, min(maxResults or 50, 100))
    payload = await get(
        credentials,
        f"/rest/agile/1.0/epic/{epicIdOrKey}/issue",
        maxResults=limit,
        startAt=startAt,
        fields=_ISSUE_FIELDS,
    )
    issues = (payload or {}).get("issues") or []
    # This endpoint reports `total`, not the `isLast` that /board and
    # /board/{id}/sprint return (verified against Atlassian's swagger.v3.json:
    # the response here is expand/issues/maxResults/startAt/total). Reading
    # isLast off it would default to True on every page and strand the caller
    # on the first one.
    total = (payload or {}).get("total", len(issues))
    return {
        "count": len(issues),
        "start_at": startAt,
        "total": total,
        "next_start": startAt + len(issues) if startAt + len(issues) < total else None,
        "issues": [_shape_issue(i, site_url) for i in issues if isinstance(i, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}
@server.tool(
    name='getJiraIssue',
    description="Get a Jira issue by its key (for example 'PROJ-123') or numeric id.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': "Issue key such as 'PROJ-123', or the "
                                                    'numeric issue id'},
                    'includeComments': {'type': 'boolean',
                                        'description': "(Optional) Include the issue's "
                                                       'comments. Off by default because a '
                                                       'long thread can dwarf the issue '
                                                       'itself.',
                                        'default': False}},
     'required': ['issueIdOrKey']})
async def getJiraIssue(credentials: Dict[str, str],
    issueIdOrKey: str,
    includeComments: bool = False,
) -> dict:
    """Fetch one issue."""
    _, site_url = await resolve_cloud_id(credentials)
    fields = _ISSUE_FIELDS + (",comment" if includeComments else "")
    issue = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}", fields=fields)
    shaped = _shape_issue(issue, site_url)
    if includeComments:
        comments = ((issue.get("fields") or {}).get("comment") or {}).get("comments") or []
        shaped["comments"] = [
            {
                "id": c.get("id"),
                "author": _person(c.get("author")),
                "created": c.get("created"),
                "body": c.get("body"),
            }
            for c in comments
            if isinstance(c, dict)
        ]
    return shaped



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}
@server.tool(
    name='getJiraIssueAttachments',
    description="List the attachments on a Jira issue: filename, size, type, and a link. addJiraIssueAttachment adds one; deleteJiraIssueAttachment removes one by its id.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'}},
     'required': ['issueIdOrKey']})
async def getJiraIssueAttachments(credentials: Dict[str, str], issueIdOrKey: str) -> dict:
    """Attachments currently on this issue."""
    _, site_url = await resolve_cloud_id(credentials)
    issue = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}", fields="attachment")
    attachments = ((issue or {}).get("fields") or {}).get("attachment") or []
    base = site_url.rstrip("/")
    return {
        "count": len(attachments),
        "attachments": [
            {
                "id": a.get("id"),
                "filename": a.get("filename"),
                "size": a.get("size"),
                "mime_type": a.get("mimeType"),
                "author": _person(a.get("author")),
                "created": a.get("created"),
                # Built off the site, not the gateway: api.atlassian.com needs
                # the same bearer token this tool used, which a human opening
                # a link in their browser does not have.
                "url": f"{base}/rest/api/3/attachment/content/{a.get('id')}"
                if base and a.get("id")
                else "",
            }
            for a in attachments
            if isinstance(a, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}/changelog
@server.tool(
    name='getJiraIssueChangelog',
    description="List the field changes recorded on a Jira issue: who changed what, from what, to what, and when. This is the issue's audit trail.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum changelog entries to '
                                                  'return (1-100)',
                                   'default': 50},
                    'startAt': {'type': 'integer',
                                'description': '(Optional) 0-based offset. Pass '
                                               "'next_start' from the previous response; "
                                               'it is null once there are no further '
                                               'results.',
                                'default': 0}},
     'required': ['issueIdOrKey']})
async def getJiraIssueChangelog(credentials: Dict[str, str],
    issueIdOrKey: str,
    maxResults: int = 50,
    startAt: int = 0,
) -> dict:
    """Field-change history for this issue."""
    limit = max(1, min(maxResults or 50, 100))
    payload = await get(
        credentials,
        f"/rest/api/3/issue/{issueIdOrKey}/changelog",
        maxResults=limit,
        startAt=startAt,
    )
    values = (payload or {}).get("values") or []
    total = (payload or {}).get("total", len(values))
    return {
        "count": len(values),
        "total": total,
        "start_at": startAt,
        "next_start": startAt + len(values) if startAt + len(values) < total else None,
        "changes": [
            {
                "id": c.get("id"),
                "author": _person(c.get("author")),
                "created": c.get("created"),
                "items": [
                    {
                        "field": item.get("field"),
                        "from": item.get("fromString"),
                        "to": item.get("toString"),
                    }
                    for item in (c.get("items") or [])
                    if isinstance(item, dict)
                ],
            }
            for c in values
            if isinstance(c, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}/remotelink
@server.tool(
    name='getJiraIssueRemoteIssueLinks',
    description="List remote links on a Jira issue, such as linked Confluence pages or external URLs. These are separate from issue-to-issue links.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'}},
     'required': ['issueIdOrKey']})
async def getJiraIssueRemoteIssueLinks(credentials: Dict[str, str], issueIdOrKey: str
) -> dict:
    """List remote links attached to an issue."""
    links = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}/remotelink")
    items = links if isinstance(links, list) else []
    return {
        "count": len(items),
        "links": [
            {
                "id": link.get("id"),
                "title": (link.get("object") or {}).get("title"),
                "url": (link.get("object") or {}).get("url"),
                "relationship": link.get("relationship"),
            }
            for link in items
            if isinstance(link, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/createmeta/{projectId}/issuetypes/{id}.
# Reads the metadata for creating an issue; it creates nothing.
@server.tool(
    name='getJiraIssueTypeMetaWithFields',
    description="Get the fields available when creating an issue of a given type in a given project, including which are required. Call this before createJiraIssue when the project's required fields are unknown.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'projectIdOrKey': {'type': 'string',
                                       'description': "Project key, e.g. 'PROJ'"},
                    'issueTypeId': {'type': 'string', 'description': 'Issue type id'}},
     'required': ['projectIdOrKey', 'issueTypeId']})
async def getJiraIssueTypeMetaWithFields(credentials: Dict[str, str], projectIdOrKey: str, issueTypeId: str
) -> dict:
    """Create-field metadata for one project and issue type."""
    meta = await get(
        credentials,
        f"/rest/api/3/issue/createmeta/{projectIdOrKey}/issuetypes/{issueTypeId}",
    )
    fields = (meta or {}).get("fields") or []
    return {
        "count": len(fields),
        "fields": [
            {
                "field_id": f.get("fieldId"),
                "name": f.get("name"),
                "required": f.get("required", False),
                "schema_type": (f.get("schema") or {}).get("type"),
                "allowed_values": [
                    v.get("name") or v.get("value") or v.get("id")
                    for v in (f.get("allowedValues") or [])
                    if isinstance(v, dict)
                ][:50],
            }
            for f in fields
            if isinstance(f, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}/watchers
@server.tool(
    name='getJiraIssueWatchers',
    description="List the users watching a Jira issue. Watchers are notified of every change, so this answers who is already following an issue.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'}},
     'required': ['issueIdOrKey']})
async def getJiraIssueWatchers(credentials: Dict[str, str], issueIdOrKey: str) -> dict:
    """Who is watching this issue."""
    payload = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}/watchers")
    watchers = (payload or {}).get("watchers") or []
    return {
        "count": (payload or {}).get("watchCount", len(watchers)),
        "is_watching": (payload or {}).get("isWatching", False),
        "watchers": [_person(w) for w in watchers if isinstance(w, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}/worklog
@server.tool(
    name='getJiraIssueWorklogs',
    description="List the worklog entries on a Jira issue: who logged time, how much, and when. addWorklogToJiraIssue writes these.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum worklog entries to '
                                                  'return (1-1000)',
                                   'default': 100},
                    'startAt': {'type': 'integer',
                                'description': '(Optional) 0-based offset. Pass '
                                               "'next_start' from the previous response; "
                                               'it is null once there are no further '
                                               'results.',
                                'default': 0}},
     'required': ['issueIdOrKey']})
async def getJiraIssueWorklogs(credentials: Dict[str, str],
    issueIdOrKey: str,
    maxResults: int = 100,
    startAt: int = 0,
) -> dict:
    """Time logged against this issue."""
    limit = max(1, min(maxResults or 100, 1000))
    payload = await get(
        credentials,
        f"/rest/api/3/issue/{issueIdOrKey}/worklog",
        maxResults=limit,
        startAt=startAt,
    )
    worklogs = (payload or {}).get("worklogs") or []
    total = (payload or {}).get("total", len(worklogs))
    return {
        "count": len(worklogs),
        "total": total,
        "start_at": startAt,
        "next_start": startAt + len(worklogs) if startAt + len(worklogs) < total else None,
        "worklogs": [
            {
                "id": w.get("id"),
                "author": _person(w.get("author")),
                "time_spent": w.get("timeSpent"),
                "time_spent_seconds": w.get("timeSpentSeconds"),
                "started": w.get("started"),
                "created": w.get("created"),
                "comment": w.get("comment"),
            }
            for w in worklogs
            if isinstance(w, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/priority
@server.tool(
    name='getJiraPriorities',
    description="List the issue priorities this site defines (for example 'High', 'Low').",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object', 'properties': {}})
async def getJiraPriorities(credentials: Dict[str, str]) -> dict:
    """Priorities available on this site."""
    priorities = await get(credentials, "/rest/api/3/priority")
    items = priorities if isinstance(priorities, list) else []
    return {
        "count": len(items),
        "priorities": [
            {"id": p.get("id"), "name": p.get("name")} for p in items if isinstance(p, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/project/{projectIdOrKey}/components
@server.tool(
    name='getJiraProjectComponents',
    description="List the components defined in a Jira project. createJiraProjectComponent adds one.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'projectIdOrKey': {'type': 'string',
                                       'description': 'Project key or numeric id'}},
     'required': ['projectIdOrKey']})
async def getJiraProjectComponents(credentials: Dict[str, str], projectIdOrKey: str
) -> dict:
    """Components defined on one project."""
    components = await get(credentials, f"/rest/api/3/project/{projectIdOrKey}/components")
    items = components if isinstance(components, list) else []
    return {
        "count": len(items),
        "components": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "description": c.get("description"),
                "lead": _person(c.get("lead")),
            }
            for c in items
            if isinstance(c, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/createmeta/{projectIdOrKey}/issuetypes
@server.tool(
    name='getJiraProjectIssueTypesMetadata',
    description="List the issue types available in a Jira project, with their ids.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'projectIdOrKey': {'type': 'string',
                                       'description': "Project key, e.g. 'PROJ'"}},
     'required': ['projectIdOrKey']})
async def getJiraProjectIssueTypesMetadata(credentials: Dict[str, str], projectIdOrKey: str
) -> dict:
    """Issue types for one project."""
    meta = await get(credentials, f"/rest/api/3/issue/createmeta/{projectIdOrKey}/issuetypes")
    types = (meta or {}).get("issueTypes") or (meta or {}).get("values") or []
    return {
        "count": len(types),
        "issue_types": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "subtask": t.get("subtask", False),
            }
            for t in types
            if isinstance(t, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/project/{projectIdOrKey}/versions
@server.tool(
    name='getJiraProjectVersions',
    description="List the versions (releases) defined in a Jira project, with their release status. createJiraProjectVersion adds one.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'projectIdOrKey': {'type': 'string',
                                       'description': 'Project key or numeric id'}},
     'required': ['projectIdOrKey']})
async def getJiraProjectVersions(credentials: Dict[str, str], projectIdOrKey: str
) -> dict:
    """Versions defined on one project."""
    versions = await get(credentials, f"/rest/api/3/project/{projectIdOrKey}/versions")
    items = versions if isinstance(versions, list) else []
    return {
        "count": len(items),
        "versions": [
            {
                "id": v.get("id"),
                "name": v.get("name"),
                "description": v.get("description"),
                "released": v.get("released", False),
                "archived": v.get("archived", False),
                "release_date": v.get("releaseDate"),
            }
            for v in items
            if isinstance(v, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/resolution
@server.tool(
    name='getJiraResolutions',
    description="List the issue resolutions this site defines (for example 'Fixed', \"Won't Do\").",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object', 'properties': {}})
async def getJiraResolutions(credentials: Dict[str, str]) -> dict:
    """Resolutions available on this site."""
    resolutions = await get(credentials, "/rest/api/3/resolution")
    items = resolutions if isinstance(resolutions, list) else []
    return {
        "count": len(items),
        "resolutions": [
            {"id": r.get("id"), "name": r.get("name")} for r in items if isinstance(r, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira_software'  required_scopes: ['read:sprint:jira-software']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/agile/1.0/board/{boardId}/sprint
@server.tool(
    name='getJiraSprintsForBoard',
    description="List the sprints on a board, newest first. Filter by state to find the active sprint, e.g. state='active'.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira_software',
    required_scopes=['read:sprint:jira-software'],
)
@declare_schema({'type': 'object',
     'properties': {'boardId': {'type': 'string',
                                'description': 'Board id from getJiraBoardsForProject'},
                    'state': {'type': 'string',
                              'enum': ['active', 'future', 'closed'],
                              'description': '(Optional) Filter to sprints in this state'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum sprints to return '
                                                  '(1-50)',
                                   'default': 50},
                    'startAt': {'type': 'integer',
                                'description': '(Optional) 0-based offset. Pass '
                                               "'next_start' from the previous response; "
                                               'it is null once there are no further '
                                               'results.',
                                'default': 0}},
     'required': ['boardId']})
async def getJiraSprintsForBoard(credentials: Dict[str, str],
    boardId: str,
    state: Optional[str] = None,
    maxResults: int = 50,
    startAt: int = 0,
) -> dict:
    """Sprints on one board."""
    limit = max(1, min(maxResults or 50, 50))
    payload = await get(
        credentials,
        f"/rest/agile/1.0/board/{boardId}/sprint",
        state=state,
        maxResults=limit,
        startAt=startAt,
    )
    values = (payload or {}).get("values") or []
    return {
        "count": len(values),
        "start_at": startAt,
        "next_start": None if (payload or {}).get("isLast", True) else startAt + len(values),
        "sprints": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "state": s.get("state"),
                "start_date": s.get("startDate"),
                "end_date": s.get("endDate"),
                "goal": s.get("goal"),
            }
            for s in values
            if isinstance(s, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/status
@server.tool(
    name='getJiraStatuses',
    description="List the issue statuses this site defines (for example 'To Do', 'Done').",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object', 'properties': {}})
async def getJiraStatuses(credentials: Dict[str, str]) -> dict:
    """Statuses available on this site."""
    statuses = await get(credentials, "/rest/api/3/status")
    items = statuses if isinstance(statuses, list) else []
    return {
        "count": len(items),
        "statuses": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "category": (s.get("statusCategory") or {}).get("name"),
            }
            for s in items
            if isinstance(s, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/issue/{issueIdOrKey}/transitions
@server.tool(
    name='getTransitionsForJiraIssue',
    description="List the workflow transitions currently available on an issue. The returned transition id is what transitionJiraIssue needs, and which transitions exist depends on the issue's current status.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'}},
     'required': ['issueIdOrKey']})
async def getTransitionsForJiraIssue(credentials: Dict[str, str], issueIdOrKey: str
) -> dict:
    """Transitions currently legal for this issue."""
    payload = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}/transitions")
    transitions = (payload or {}).get("transitions") or []
    return {
        "count": len(transitions),
        "transitions": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "to_status": (t.get("to") or {}).get("name"),
            }
            for t in transitions
            if isinstance(t, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/project/search
@server.tool(
    name='getVisibleJiraProjects',
    description="List the Jira projects this user can see, newest page first. Use the returned key when another tool asks for a project.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'searchString': {'type': 'string',
                                     'description': '(Optional) Filter by project name or '
                                                    'key'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum projects to return '
                                                  '(1-50)',
                                   'default': 50},
                    'startAt': {'type': 'integer',
                                'description': '(Optional) 0-based offset. Pass '
                                               "'next_start' from the previous response; "
                                               'it is null once there are no further '
                                               'results.',
                                'default': 0}}})
async def getVisibleJiraProjects(credentials: Dict[str, str],
    searchString: Optional[str] = None,
    maxResults: int = 50,
    startAt: int = 0,
) -> dict:
    """Projects visible to the connected user."""
    limit = max(1, min(maxResults or 50, 50))
    payload = await get(
        credentials,
        "/rest/api/3/project/search",
        query=searchString,
        maxResults=limit,
        startAt=startAt,
    )
    values = (payload or {}).get("values") or []
    return {
        "count": len(values),
        "start_at": startAt,
        # Jira reports isLast on this endpoint; trust it over guessing from
        # the page size, which is wrong whenever the total is an exact
        # multiple of the limit.
        "next_start": None if (payload or {}).get("isLast", True) else startAt + len(values),
        "projects": [
            {
                "id": p.get("id"),
                "key": p.get("key"),
                "name": p.get("name"),
                "project_type": p.get("projectTypeKey"),
            }
            for p in values
            if isinstance(p, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'read_jira'  required_scopes: ['read:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/user/search
@server.tool(
    name='lookupJiraAccountId',
    description="Find Jira users by display name or email and return their account ids. Assignment and mention tools need an account id, never a name.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='read_jira',
    required_scopes=['read:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'searchString': {'type': 'string',
                                     'description': 'Display name or email to search for'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum users to return '
                                                  '(1-50)',
                                   'default': 10}},
     'required': ['searchString']})
async def lookupJiraAccountId(credentials: Dict[str, str], searchString: str, maxResults: int = 10
) -> dict:
    """Resolve a name or email to Atlassian account ids."""
    users = await get(
        credentials,
        "/rest/api/3/user/search",
        query=searchString,
        maxResults=max(1, min(maxResults or 10, 50)),
    )
    items = users if isinstance(users, list) else []
    return {
        "count": len(items),
        "users": [
            {
                "account_id": u.get("accountId"),
                "display_name": u.get("displayName"),
                "email": u.get("emailAddress"),
                "active": u.get("active", True),
            }
            for u in items
            if isinstance(u, dict)
        ],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira_software'  required_scopes: ['write:sprint:jira-software']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: POST /rest/agile/1.0/sprint/{id}/issue. Whether
# the issue is detached from its previous sprint is server-side and not
# provable here, and an unproven claim of additive-only is exactly what
# destructiveHint=False must not be.
# Evidence: POST /rest/agile/1.0/sprint/{sprintId}/issue
@server.tool(
    name='moveJiraIssuesToSprint',
    description="Move up to 50 Jira issues into a sprint.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira_software',
    required_scopes=['write:sprint:jira-software'],
)
@declare_schema({'type': 'object',
     'properties': {'sprintId': {'type': 'string', 'description': 'Target sprint id'},
                    'issueKeys': {'type': 'array',
                                  'items': {'type': 'string'},
                                  'description': "Issue keys to move, e.g. ['PROJ-1', "
                                                 "'PROJ-2']. Max 50."}},
     'required': ['sprintId', 'issueKeys']})
async def moveJiraIssuesToSprint(credentials: Dict[str, str], sprintId: str, issueKeys: List[str]
) -> dict:
    """Bulk-move issues into a sprint."""
    if len(issueKeys) > 50:
        raise ValueError("Jira accepts at most 50 issues per call.")
    await post(credentials, f"/rest/agile/1.0/sprint/{sprintId}/issue", {"issues": issueKeys})
    return {"success": True, "sprint_id": sprintId, "issue_count": len(issueKeys)}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /rest/api/3/issueLink/{linkId}
@server.tool(
    name='removeJiraIssueLink',
    description="Delete a link between two Jira issues by its link id. The id comes from an issue's links, not from the issue keys themselves.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'linkId': {'type': 'string', 'description': 'Issue link id'}},
     'required': ['linkId']})
async def removeJiraIssueLink(credentials: Dict[str, str], linkId: str) -> dict:
    """Remove one issue link."""
    await delete(credentials, f"/rest/api/3/issueLink/{linkId}")
    return {"success": True, "link_id": linkId}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /rest/api/3/issue/{issueIdOrKey}/watchers
@server.tool(
    name='removeJiraIssueWatcher',
    description="Remove a user from a Jira issue's watchers.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'accountId': {'type': 'string',
                                  'description': 'Account id of the watcher to remove'}},
     'required': ['issueIdOrKey', 'accountId']})
async def removeJiraIssueWatcher(credentials: Dict[str, str], issueIdOrKey: str, accountId: str
) -> dict:
    """Stop watching an issue on someone's behalf."""
    # Removal identifies the watcher by query parameter, unlike the add.
    await delete(credentials, f"/rest/api/3/issue/{issueIdOrKey}/watchers", accountId=accountId)
    return {"success": True, "issue": issueIdOrKey, "account_id": accountId}



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'search_jira'  required_scopes: ['search:jira-work']
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /rest/api/3/search/jql
@server.tool(
    name='searchJiraIssuesUsingJql',
    description="Search Jira issues with a JQL query, for example 'project = PROJ AND status = \"In Progress\" ORDER BY created DESC'. Returns a page of issues plus a cursor for the next page.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='search_jira',
    required_scopes=['search:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'jql': {'type': 'string',
                            'description': 'JQL query string. Quote values containing '
                                           'spaces.'},
                    'maxResults': {'type': 'integer',
                                   'description': '(Optional) Maximum issues to return '
                                                  '(1-100)',
                                   'default': 50},
                    'nextPageToken': {'type': 'string',
                                      'description': '(Optional) Cursor from a previous '
                                                     "response's 'next_page_token'. It is "
                                                     'null once there are no further '
                                                     'results.'}},
     'required': ['jql']})
async def searchJiraIssuesUsingJql(credentials: Dict[str, str],
    jql: str,
    maxResults: int = 50,
    nextPageToken: Optional[str] = None,
) -> dict:
    """Run one JQL query.

    Uses the cursor-based /search/jql endpoint rather than the offset-based
    /search, which Atlassian has deprecated for Jira Cloud.
    """
    _, site_url = await resolve_cloud_id(credentials)
    payload = await get(
        credentials,
        "/rest/api/3/search/jql",
        jql=jql,
        maxResults=max(1, min(maxResults or 50, _MAX_RESULTS)),
        nextPageToken=nextPageToken,
        fields=_ISSUE_FIELDS,
    )
    issues = (payload or {}).get("issues") or []
    return {
        "count": len(issues),
        "next_page_token": (payload or {}).get("nextPageToken"),
        "issues": [_shape_issue(i, site_url) for i in issues if isinstance(i, dict)],
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: POST .../transitions replaces the issue's status.
# Evidence: POST /rest/api/3/issue/{issueIdOrKey}/transitions
@server.tool(
    name='transitionJiraIssue',
    description="Move a Jira issue through its workflow. Which transitions are legal depends on the issue's current status, so call getTransitionsForJiraIssue first to get a valid transitionId.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'transitionId': {'type': 'string',
                                     'description': 'Transition id from '
                                                    'getTransitionsForJiraIssue'},
                    'comment': {'type': 'string',
                                'description': '(Optional) Comment to add as part of the '
                                               'transition'}},
     'required': ['issueIdOrKey', 'transitionId']})
async def transitionJiraIssue(credentials: Dict[str, str],
    issueIdOrKey: str,
    transitionId: str,
    comment: Optional[str] = None,
) -> dict:
    """Perform one workflow transition."""
    payload: Dict[str, Any] = {"transition": {"id": str(transitionId)}}
    if comment:
        payload["update"] = {"comment": [{"add": {"body": to_adf(comment)}}]}

    await post(credentials, f"/rest/api/3/issue/{issueIdOrKey}/transitions", payload)

    # Report where it actually landed. The transition name is what the caller
    # asked for; the status is what Jira did, and a workflow post-function
    # can make those differ.
    issue = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}", fields="status")
    return {
        "success": True,
        "issue": issueIdOrKey,
        "status": ((issue or {}).get("fields") or {}).get("status", {}).get("name"),
    }



# credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
# permission_group: 'write_jira'  required_scopes: ['write:jira-work']
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: POST .../transitions after resolving the id:
# the same status overwrite.
# Evidence: POST /rest/api/3/issue/{key}/transitions. A transition is a
# state change, not a deletion, so destructiveHint stays False.
@server.tool(
    name='updateJiraIssueStatus',
    description="Move a Jira issue to a status by name, e.g. 'In Progress' or 'Done', instead of a transition id. Looks up the matching transition automatically. Which statuses are reachable depends on the issue's current one and its workflow, so a status that exists on the site can still be unreachable from here; the error lists what is actually reachable when that happens.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id'],
    permission_group='write_jira',
    required_scopes=['write:jira-work'],
)
@declare_schema({'type': 'object',
     'properties': {'issueIdOrKey': {'type': 'string',
                                     'description': 'Issue key or numeric id'},
                    'status': {'type': 'string',
                               'description': "Target status name, e.g. 'Done'. "
                                              'Case-insensitive.'},
                    'comment': {'type': 'string',
                                'description': '(Optional) Comment to add as part of the '
                                               'transition'}},
     'required': ['issueIdOrKey', 'status']})
async def updateJiraIssueStatus(credentials: Dict[str, str],
    issueIdOrKey: str,
    status: str,
    comment: Optional[str] = None,
) -> dict:
    """Transition to a status by name, resolving the transition id first."""
    payload = await get(credentials, f"/rest/api/3/issue/{issueIdOrKey}/transitions")
    transitions = (payload or {}).get("transitions") or []
    match = next(
        (
            t
            for t in transitions
            if isinstance(t, dict)
            and (t.get("to") or {}).get("name", "").lower() == status.lower()
        ),
        None,
    )
    if not match:
        available = sorted(
            {
                (t.get("to") or {}).get("name")
                for t in transitions
                if isinstance(t, dict) and (t.get("to") or {}).get("name")
            }
        )
        raise ValueError(
            f"No transition to status '{status}' from here. Reachable from the "
            f"current status: {', '.join(available) or 'none'}."
        )

    body: Dict[str, Any] = {"transition": {"id": str(match["id"])}}
    if comment:
        body["update"] = {"comment": [{"add": {"body": to_adf(comment)}}]}
    await post(credentials, f"/rest/api/3/issue/{issueIdOrKey}/transitions", body)
    return {
        "success": True,
        "issue": issueIdOrKey,
        "status": (match.get("to") or {}).get("name"),
    }
