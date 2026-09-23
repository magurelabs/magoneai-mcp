"""The microsoft-sharepoint connector.

Ported from the v1 mixin form by a codemod.

6 tools, 6 of them read-only, all on the app-only (client credentials) flow
with a per-connection site id, which is the only flow a SharePoint
connection carries. They are the part of what v1 served that needs nothing
but the caller's own SharePoint credentials; the two knowledge-base sync tools
v1 also served depended on platform storage and are not here. Every input
schema is carried across verbatim via `declare_schema` rather than derived
from type hints, so the wire contract is byte-identical to v1 and no live
agent sees a changed parameter schema.

v1's repository also held five delegated-flow tools (`sites_tools.py`: get
root site, site by path, drives, drive items, search sites) that its server
never composed, so no deployment ever offered them. The port registered
them, on a flow no SharePoint connection can satisfy, so each failed on every
call. They are not ported.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

SERVER_NAME = "microsoft-sharepoint"
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

import re
from typing import Any, Dict, Optional
from urllib.parse import quote
from .tools._constants import CREDENTIAL_HEADERS, MAX_RESULTS_CAP, MAX_SEARCH_LEN
import base64
import hashlib
import os
from typing import Any, Dict, List, Optional
import httpx
from ._shared import SHAREPOINT_APP_ONLY_CREDENTIAL_HEADERS
from .tools._constants import MAX_RESULTS_CAP
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


MAX_RESULTS_CAP = 200

MAX_ID_LENGTH = 500

MAX_SEARCH_LEN = 256

CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}$")

def validate_drive_item_id(value: str, label: str = "ID") -> str | None:
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if "/" in value or ".." in value:
        return f"{label} contains invalid characters"
    if len(value) > MAX_ID_LENGTH:
        return f"{label} is too long (max {MAX_ID_LENGTH} chars)"
    return None

def validate_site_id(value: str, label: str = "site_id") -> str | None:
    """Site ids from Graph often contain commas; still block path traversal."""
    if not value or not value.strip():
        return f"{label} cannot be empty"
    if ".." in value or "\n" in value or "\r" in value:
        return f"{label} contains invalid characters"
    if len(value) > 300:
        return f"{label} is too long"
    return None

def validate_hostname(hostname: str) -> str | None:
    h = (hostname or "").strip().lower()
    if not h:
        return "hostname cannot be empty"
    if not _HOSTNAME_RE.match(h):
        return "hostname must look like tenant.sharepoint.com"
    if ".." in h or "/" in h:
        return "invalid hostname"
    return None

def validate_site_path(site_path: str) -> str | None:
    if site_path is None:
        return None
    p = site_path.strip()
    if not p:
        return None
    if ".." in p:
        return "site_path cannot contain .."
    if len(p) > 500:
        return "site_path is too long"
    return None

DEFAULT_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

GRAPH_API_URL = "https://graph.microsoft.com/v1.0"

MAX_WALK_ITEMS = 10000

_ITEM_SELECT = (
    "id,name,size,file,folder,eTag,cTag,webUrl,lastModifiedDateTime,"
    "createdBy,lastModifiedBy,parentReference"
)

def _file_entry(it: Dict[str, Any]) -> Dict[str, Any]:
    """Project a Graph DriveItem (file) into the flat inventory shape the sync
    workflow diffs against the KB registry."""
    created = (it.get("createdBy") or {}).get("user") or {}
    modified = (it.get("lastModifiedBy") or {}).get("user") or {}
    name = it.get("name") or ""
    return {
        "id": it.get("id"),
        "name": name,
        "size": it.get("size") or 0,
        "mime_type": (it.get("file") or {}).get("mimeType") or "application/octet-stream",
        # cTag changes only on CONTENT change (eTag also bumps on metadata edits);
        # the platform uses cTag as the cheap "re-download needed?" gate.
        "ctag": it.get("cTag"),
        "etag": it.get("eTag"),
        "web_url": it.get("webUrl") or "",
        "last_modified_at": it.get("lastModifiedDateTime"),
        "created_by_name": created.get("displayName"),
        "created_by_email": created.get("email"),
        "modified_by_name": modified.get("displayName"),
        "modified_by_email": modified.get("email"),
        "doc_type": (name.rsplit(".", 1)[-1].lower() if "." in name else None),
        "parent_path": (it.get("parentReference") or {}).get("path"),
    }

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

_ALLOWED_GRAPH_HOSTS = frozenset({"graph.microsoft.com"})

_APP_ONLY_REFRESH_LEEWAY = 60  # seconds

# App-only access tokens by (tenant_id, client_id), refreshed under one lock
# so concurrent calls collapse into a single token request. v1 held both as
# class attributes of `MicrosoftGraph` (`shared/microsoft_graph.py`); the port
# flattened the methods and left the attributes behind, so every tool raised
# NameError on its first call.
_app_only_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
_app_only_token_lock = asyncio.Lock()


class GraphNotFoundError(ValueError):
    """An actual Graph 404, as distinct from any error that mentions "not found".

    v1's, from `shared/microsoft_graph.py`. Raised by the request helper and
    never defined here, so every 404 became a NameError.
    """

_MAX_THROTTLE_RETRIES = 5

_MAX_BACKOFF_SECONDS = 60.0

_UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024

def _throttle_delay_seconds(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429/503: honor Retry-After when present,
    else exponential backoff (2**attempt) capped at _MAX_BACKOFF_SECONDS."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(int(retry_after)), _MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    return min(2.0**attempt, _MAX_BACKOFF_SECONDS)




async def _get_app_only_token(credentials: Dict[str, str]) -> str:
    """Acquire an app-only access token via client credentials grant.

    Used for service-to-service flows (e.g., the scheduled SharePoint
    sync use case) where no user is in the loop. Requires an Azure AD
    app with admin-consented application permissions on the target
    Graph scopes (e.g., Sites.Read.All, Files.Read.All).

    Tokens are cached per (tenant_id, client_id) until ~1 minute before
    their reported expiry, so a multi-thousand-call sync does not hammer
    Azure AD's token endpoint and trigger throttling.
    """
    client_id = credentials.get("microsoft_client_id")
    client_secret = credentials.get("microsoft_client_secret")
    tenant_id = credentials.get("microsoft_tenant_id")

    if not client_id or not client_secret:
        raise ValueError(
            "Missing app-only credentials. Pass client_id and client_secret via headers."
        )
    if not tenant_id:
        raise ValueError("Missing tenant_id. App-only OAuth requires an explicit tenant id.")

    cache_key = (tenant_id, client_id)
    now = time.monotonic()

    # Fast path: cache hit.
    cached = _app_only_token_cache.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    # Slow path: refresh under the shared lock so concurrent callers collapse.
    async with _app_only_token_lock:
        # Re-check under the lock — another coroutine may have refreshed.
        cached = _app_only_token_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL.format(tenant=tenant_id),
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )

            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {}
                error_desc = error_data.get(
                    "error_description", response.text[:500] or "Token acquisition failed"
                )
                raise ValueError(
                    f"App-only token acquisition failed ({response.status_code}): {error_desc}"
                )

            token_data = response.json()
            if "access_token" not in token_data:
                raise ValueError(
                    "Token endpoint returned unexpected response (no access_token)."
                )

            token = token_data["access_token"]
            # `expires_in` is seconds-from-now per OAuth2. Default to 3600
            # if Azure ever omits it (shouldn't, but be defensive). Refresh
            # _APP_ONLY_REFRESH_LEEWAY seconds early so we never present an
            # expired token mid-flight.
            expires_in = int(token_data.get("expires_in", 3600))
            expires_at = time.monotonic() + max(0, expires_in - _APP_ONLY_REFRESH_LEEWAY)
            _app_only_token_cache[cache_key] = (token, expires_at)
            return token

async def _graph_request_app_only(credentials: Dict[str, str],
    method: str,
    endpoint: str,
    json_data: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Make a Graph API request using app-only OAuth.

    `endpoint` accepts either a relative path (e.g., "/drives/{id}/root/delta")
    or an absolute URL (e.g., a returned @odata.deltaLink). Both are supported
    so that delta pagination works naturally.

    Returns {"_error": "delta_expired", "_status": 410} on HTTP 410 — callers
    should reset their deltaLink and re-list from scratch rather than treat it
    as a hard failure.
    """
    # SSRF guard: pin absolute URLs to graph.microsoft.com before the
    # bearer token is acquired. A poisoned delta_link must not be able to
    # cause the app-only Graph token to be sent to an attacker-controlled
    # host. Relative paths always join GRAPH_API_URL and are safe.
    if endpoint.startswith("http"):
        host = (urlparse(endpoint).hostname or "").lower()
        if host not in _ALLOWED_GRAPH_HOSTS:
            raise ValueError(
                f"App-only Graph request rejected: absolute URL must target "
                f"graph.microsoft.com (got host={host!r})."
            )
        url = endpoint
    else:
        url = f"{GRAPH_API_URL}{endpoint}"

    access_token = await _get_app_only_token(credentials)

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        attempt = 0
        while True:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
            )
            # Retry throttling / transient errors with backoff before
            # surfacing them. Token stays valid across retries (~1h life).
            if response.status_code in (429, 503) and attempt < _MAX_THROTTLE_RETRIES:
                await asyncio.sleep(_throttle_delay_seconds(response, attempt))
                attempt += 1
                continue
            break

        if response.status_code in (401, 403):
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            detail = error_data.get("error", {}).get("message", response.text[:500])
            if response.status_code == 401:
                raise ValueError(
                    f"Authentication failed. Check app credentials and admin consent. ({detail})"
                )
            raise ValueError(
                "Access denied. Verify the app has the required admin-consented "
                "application permission for this resource (e.g. Mail.Read for mailbox "
                "access, or a Sites.Selected per-site grant for SharePoint). "
                f"({detail})"
            )
        elif response.status_code == 404:
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            detail = error_data.get("error", {}).get("message", response.text[:500])
            raise GraphNotFoundError(
                f"Resource not found. ({detail})" if detail else "Resource not found."
            )
        elif response.status_code == 410:
            # deltaLink expired — caller should reset and re-list.
            return {"_error": "delta_expired", "_status": 410}
        elif response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise ValueError(
                f"Rate limited after {attempt} retries. Retry-After: {retry_after}s"
            )
        elif response.status_code >= 400:
            try:
                error_data = response.json() if response.content else {}
            except Exception:
                error_data = {}
            error_msg = error_data.get("error", {}).get("message", response.text[:500])
            raise ValueError(f"Graph API error ({response.status_code}): {error_msg}")

        if response.status_code in (204, 202):
            return {}
        return response.json()



# credential_headers: ['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /drives/{drive_id}/items/{item_id}
@server.tool(
    name='sharepoint_download_file',
    description="Download a SharePoint DriveItem's content by drive_id + item_id. Returns base64-encoded bytes plus size, mime_type, and a SHA-256 hash for change-detection. Rejects files larger than max_size_bytes (default 50 MB) with too_large=true and no content.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'drive_id': {'type': 'string'},
                    'item_id': {'type': 'string'},
                    'max_size_bytes': {'type': 'integer',
                                       'description': 'Maximum allowed file size. Default '
                                                      '50 MB.'}},
     'required': ['drive_id', 'item_id']})
@declare_output_schema({'properties': {'content_base64': {'type': 'string'},
                                       'content_hash_sha256': {'type': 'string'},
                                       'last_modified_at': {'type': 'string'},
                                       'mime_type': {'type': 'string'},
                                       'name': {'type': 'string'},
                                       'size': {'type': 'integer'},
                                       'too_large': {'type': 'boolean'},
                                       'web_url': {'type': 'string'}},
                        'required': ['too_large'],
                        'type': 'object'})
async def sharepoint_download_file(credentials: Dict[str, str],
    drive_id: str,
    item_id: str,
    max_size_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
) -> Dict[str, Any]:
    derr = validate_drive_item_id(drive_id, "drive_id")
    if derr:
        return {"success": False, "error": derr}
    ierr = validate_drive_item_id(item_id, "item_id")
    if ierr:
        return {"success": False, "error": ierr}

    try:
        # Fetch metadata first to check size without downloading bytes.
        metadata = await _graph_request_app_only(
            credentials, "GET", f"/drives/{drive_id}/items/{item_id}"
        )
        size = metadata.get("size", 0)
        name = metadata.get("name", "")
        mime_type = metadata.get("file", {}).get("mimeType", "")
        web_url = metadata.get("webUrl", "")
        last_modified = metadata.get("lastModifiedDateTime", "")

        if size > max_size_bytes:
            return {
                "success": True,
                "name": name,
                "size": size,
                "mime_type": mime_type,
                "content_hash_sha256": "",
                "content_base64": "",
                "web_url": web_url,
                "last_modified_at": last_modified,
                "too_large": True,
            }

        # Download bytes. /content returns 302 to Azure Storage — httpx
        # follows the redirect transparently (and strips the bearer header
        # on the cross-host hop, which is correct: the redirect URL is
        # pre-signed).
        access_token = await _get_app_only_token(credentials)
        url = f"{GRAPH_API_URL}/drives/{drive_id}/items/{item_id}/content"

        # Stream the body and abort the moment it crosses the cap, so a
        # file whose metadata under-reported its size (or reported 0)
        # cannot OOM the server — the pre-check above is necessary but not
        # sufficient when Graph metadata is wrong/missing.
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers={"Authorization": f"Bearer {access_token}"}
            ) as resp:
                if resp.status_code == 401:
                    return {"success": False, "error": "Authentication failed during download."}
                if resp.status_code == 403:
                    return {
                        "success": False,
                        "error": "Access denied. Verify the Sites.Selected read grant.",
                    }
                if resp.status_code == 404:
                    return {
                        "success": False,
                        "error": f"File not found: drive={drive_id}, item={item_id}",
                    }
                resp.raise_for_status()
                buf = bytearray()
                too_large = False
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_size_bytes:
                        too_large = True
                        break

        if too_large:
            return {
                "success": True,
                "name": name,
                "size": size or len(buf),
                "mime_type": mime_type,
                "content_hash_sha256": "",
                "content_base64": "",
                "web_url": web_url,
                "last_modified_at": last_modified,
                "too_large": True,
            }

        content_bytes = bytes(buf)
        return {
            "success": True,
            "name": name,
            "size": len(content_bytes),
            "mime_type": mime_type,
            "content_hash_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "content_base64": base64.b64encode(content_bytes).decode("ascii"),
            "web_url": web_url,
            "last_modified_at": last_modified,
            "too_large": False,
        }
    except Exception as e:
        logger.exception("sharepoint_download_file_error", error=str(e))
        return {"success": False, "error": str(e)}



# credential_headers: ['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /drives/{drive_id}/items/{item_id}
@server.tool(
    name='sharepoint_get_drive_item',
    description="Fetch a single DriveItem's metadata by drive_id + item_id. Returns the full Microsoft Graph DriveItem JSON (name, size, eTag, createdBy, lastModifiedBy, parentReference, webUrl, mimeType, etc.). Useful for citation enrichment and ad-hoc lookups.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'drive_id': {'type': 'string'}, 'item_id': {'type': 'string'}},
     'required': ['drive_id', 'item_id']})
@declare_output_schema({'description': 'DriveItem JSON from Microsoft Graph (passthrough).', 'type': 'object'})
async def sharepoint_get_drive_item(credentials: Dict[str, str],
    drive_id: str,
    item_id: str,
) -> Dict[str, Any]:
    derr = validate_drive_item_id(drive_id, "drive_id")
    if derr:
        return {"success": False, "error": derr}
    ierr = validate_drive_item_id(item_id, "item_id")
    if ierr:
        return {"success": False, "error": ierr}
    try:
        data = await _graph_request_app_only(
            credentials, "GET", f"/drives/{drive_id}/items/{item_id}"
        )
    except Exception as e:
        logger.exception("sharepoint_get_drive_item_error", error=str(e))
        return {"success": False, "error": str(e)}
    return {"success": True, **data}






# credential_headers: ['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /sites/{h}
@server.tool(
    name='sharepoint_get_site',
    description="Resolve a SharePoint site to its Graph id by hostname + server-relative path (app-only). Example: hostname=contoso.sharepoint.com, site_path=sites/Marketing. Used when configuring a connection.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'hostname': {'type': 'string',
                                 'description': 'e.g. contoso.sharepoint.com'},
                    'site_path': {'type': 'string',
                                  'description': 'e.g. sites/Marketing (omit for the host '
                                                 'root site)'}},
     'required': ['hostname']})
@declare_output_schema({'properties': {'id': {'type': 'string'},
                                       'name': {'type': 'string'},
                                       'webUrl': {'type': 'string'}},
                        'required': ['id'],
                        'type': 'object'})
async def sharepoint_get_site(credentials: Dict[str, str],
    hostname: str,
    site_path: str = "",
) -> Dict[str, Any]:
    herr = validate_hostname(hostname)
    if herr:
        return {"success": False, "error": herr}
    perr = validate_site_path(site_path)
    if perr:
        return {"success": False, "error": perr}
    h = hostname.strip().lower()
    path = (site_path or "").strip().strip("/")
    endpoint = f"/sites/{h}" if not path else f"/sites/{h}:/{quote(path, safe='/')}:"
    try:
        data = await _graph_request_app_only(credentials, "GET", endpoint)
    except Exception as e:
        logger.exception("sharepoint_get_site_app_only_error", error=str(e))
        return {"success": False, "error": str(e)}
    return {
        "success": True,
        "id": data.get("id"),
        "name": data.get("name"),
        "displayName": data.get("displayName"),
        "webUrl": data.get("webUrl"),
    }






# credential_headers: ['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /drives/{drive_id}/items/{folder_item_id}/children
@server.tool(
    name='sharepoint_list_children',
    description="List one level of a drive folder's children (folders + files), app-only. Omit folder_item_id for the drive root. Powers the folder-picker UI; use sharepoint_list_folder for the recursive sync inventory.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'drive_id': {'type': 'string'},
                    'folder_item_id': {'type': 'string',
                                       'description': 'Folder driveItem id; omit to list '
                                                      'the drive root.'},
                    'max_results': {'type': 'integer',
                                    'minimum': 1,
                                    'maximum': 200,
                                    'default': 100}},
     'required': ['drive_id']})
@declare_output_schema({'properties': {'items': {'items': {'type': 'object'}, 'type': 'array'}},
                        'required': ['items'],
                        'type': 'object'})
async def sharepoint_list_children(credentials: Dict[str, str],
    drive_id: str,
    folder_item_id: Optional[str] = None,
    max_results: int = 100,
) -> Dict[str, Any]:
    derr = validate_drive_item_id(drive_id, "drive_id")
    if derr:
        return {"success": False, "error": derr}
    if folder_item_id:
        ferr = validate_drive_item_id(folder_item_id, "folder_item_id")
        if ferr:
            return {"success": False, "error": ferr}
    top = min(max(int(max_results or 100), 1), MAX_RESULTS_CAP)
    if folder_item_id:
        endpoint = f"/drives/{drive_id}/items/{folder_item_id}/children"
    else:
        endpoint = f"/drives/{drive_id}/root/children"
    try:
        data = await _graph_request_app_only(
            credentials,
            "GET",
            endpoint,
            params={"$top": top, "$select": "id,name,folder,file,size,webUrl,childCount"},
        )
    except Exception as e:
        logger.exception("sharepoint_list_children_error", error=str(e))
        return {"success": False, "error": str(e)}
    items = [
        {
            "id": it.get("id"),
            "name": it.get("name"),
            "is_folder": "folder" in it,
            "child_count": (it.get("folder") or {}).get("childCount"),
            "size": it.get("size"),
            "web_url": it.get("webUrl"),
        }
        for it in data.get("value", [])
    ]
    return {"success": True, "items": items}









# credential_headers: ['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /drives/{drive_id}/items/{folder_item_id}/children
@server.tool(
    name='sharepoint_list_folder',
    description="Recursively list every file under a SharePoint folder (or the drive root) and return a flat inventory: id, name, size, mime_type, cTag/eTag (for change detection), web_url, author, and timestamps. Folder-scoped — point it at a department folder and only that subtree is returned. Pass folder_item_id OR folder_path; omit both for the drive root. Sets truncated=true if the item cap is reached.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'drive_id': {'type': 'string',
                                 'description': 'SharePoint drive (document library) id.'},
                    'folder_item_id': {'type': 'string',
                                       'description': 'Folder driveItem id to walk. '
                                                      'Optional.'},
                    'folder_path': {'type': 'string',
                                    'description': 'Drive-root-relative folder path, e.g. '
                                                   "'Shared Documents/Reports'. "
                                                   'Optional.'},
                    'max_items': {'type': 'integer',
                                  'description': 'Safety cap on files returned (default '
                                                 '10000).'}},
     'required': ['drive_id']})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'files': {'items': {'type': 'object'}, 'type': 'array'},
                                       'truncated': {'type': 'boolean'}},
                        'required': ['files', 'count', 'truncated'],
                        'type': 'object'})
async def sharepoint_list_folder(credentials: Dict[str, str],
    drive_id: str,
    folder_item_id: Optional[str] = None,
    folder_path: Optional[str] = None,
    max_items: int = MAX_WALK_ITEMS,
) -> Dict[str, Any]:
    derr = validate_drive_item_id(drive_id, "drive_id")
    if derr:
        return {"success": False, "error": derr}
    if folder_item_id:
        ferr = validate_drive_item_id(folder_item_id, "folder_item_id")
        if ferr:
            return {"success": False, "error": ferr}
    if folder_path:
        perr = validate_site_path(folder_path)
        if perr:
            return {"success": False, "error": perr}
    cap = max(1, min(int(max_items or MAX_WALK_ITEMS), MAX_WALK_ITEMS))

    if folder_item_id:
        start = f"/drives/{drive_id}/items/{folder_item_id}/children"
    elif folder_path:
        clean = folder_path.strip().strip("/")
        start = f"/drives/{drive_id}/root:/{quote(clean, safe='/')}:/children"
    else:
        start = f"/drives/{drive_id}/root/children"

    files: List[Dict[str, Any]] = []
    # DFS over the subtree; `stack` holds children-endpoints still to fetch.
    stack: List[str] = [start]
    truncated = False
    try:
        while stack and not truncated:
            url: Optional[str] = stack.pop()
            params: Optional[Dict[str, Any]] = {"$select": _ITEM_SELECT, "$top": 200}
            while url:
                resp = await _graph_request_app_only(
                    credentials, "GET", url, params=params
                )
                for it in resp.get("value", []):
                    if "folder" in it:
                        stack.append(f"/drives/{drive_id}/items/{it['id']}/children")
                    elif "file" in it:
                        files.append(_file_entry(it))
                        if len(files) >= cap:
                            truncated = True
                            break
                if truncated:
                    break
                # nextLink is an absolute URL carrying its own query string.
                url, params = resp.get("@odata.nextLink"), None
    except Exception as e:
        logger.exception("sharepoint_list_folder_error", error=str(e))
        return {"success": False, "error": str(e)}
    return {"success": True, "files": files, "count": len(files), "truncated": truncated}






# credential_headers: ['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False.
# One GET to /sites/{id}/drives, app-only, returning id/name/webUrl.
# The `_libraries` name and the app-only auth mode made it look like the
# write-capable sibling; the delegated sharepoint_list_drives beside it
# does the same read and was already marked read-only.
@server.tool(
    name='sharepoint_site_libraries',
    description="List a site's document libraries (drives), app-only. Use the site id from sharepoint_get_site. Named distinctly from the delegated sharepoint_list_drives so both auth modes can coexist in one server.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id', 'X-Sharepoint-Site-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'site_id': {'type': 'string',
                                'description': 'Graph site id. Optional — defaults to this '
                                               "connection's bound site (one site per "
                                               'connection).'}}})
@declare_output_schema({'properties': {'drives': {'items': {'type': 'object'}, 'type': 'array'}},
                        'required': ['drives'],
                        'type': 'object'})
async def sharepoint_site_libraries(credentials: Dict[str, str],
    site_id: str = "",
) -> Dict[str, Any]:
    # One site per connection: fall back to the credential-bound site_id.:
    site_id = (site_id or credentials.get("sharepoint_site_id") or "").strip()
    serr = validate_site_id(site_id, "site_id")
    if serr:
        return {"success": False, "error": serr}
    sid = quote(site_id.strip(), safe=",")
    try:
        data = await _graph_request_app_only(credentials, "GET", f"/sites/{sid}/drives")
    except Exception as e:
        logger.exception("sharepoint_list_drives_app_only_error", error=str(e))
        return {"success": False, "error": str(e)}
    drives = [
        {"id": d.get("id"), "name": d.get("name"), "webUrl": d.get("webUrl")}
        for d in data.get("value", [])
    ]
    return {"success": True, "drives": drives}






