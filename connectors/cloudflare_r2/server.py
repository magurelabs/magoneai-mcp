"""The cloudflare-r2 connector.

Ported from the v1 mixin form by a codemod.

2 tools, 0 of them proposed read-only. Every input schema
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

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema
from magone_mcp_core.net import is_blocked_address

SERVER_NAME = "cloudflare-r2"
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

import os
from dataclasses import dataclass
import aioboto3
from botocore.config import Config
import base64
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
import structlog
# R2Config is defined in tools/storage.py and was used here without being
# imported: an unresolved name the port left behind, masked until the
# platform-storage residue above stopped shadowing it at import time.
from .tools.storage import MAX_UPLOAD_BYTES, R2Config
from .tools.url_fetch import RemoteFetchError
import mimetypes
import re
import uuid
from datetime import datetime, timezone
import ipaddress
import socket
from urllib.parse import urlparse
import httpx







# `_split_storage_path`, `_engine` and the platform_files import were removed.
# They resolved a PLATFORM file id to an object key, which moved to the private
# overlay with `upload_file_by_id`. Left here they were dead, and one of them
# built a SQLAlchemy engine to the platform's own database from inside a public
# connector - the exact coupling the boundary gate exists to refuse.




_R2_HEADERS = [
    "X-R2-Account-Id",
    "X-R2-Access-Key-Id",
    "X-R2-Secret-Access-Key",
    "X-R2-Bucket",
    "X-R2-Public-Base-Url",
    "X-R2-Endpoint-Url",
]

_OUTPUT_SCHEMA = {
    "type": "object",
    "description": "The stored object's shareable URL and metadata.",
    "properties": {
        "url": {"type": "string", "description": "Public or presigned download URL."},
        "is_public": {
            "type": "boolean",
            "description": "True for a permanent public URL, False for a time-limited presigned URL.",
        },
        "key": {"type": "string", "description": "Object key within the bucket."},
        "bucket": {"type": "string"},
        "content_type": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "file_id": {
            "type": "string",
            "description": "Source workflow file id, when uploaded via upload_file_by_id.",
        },
        "expires_in": {
            "type": ["integer", "null"],
            "description": "Presigned URL lifetime in seconds, or null when the URL is public.",
        },
    },
}

_COMMON_PROPS = {
    "content_type": {
        "type": "string",
        "description": "MIME type override, e.g. 'application/pdf'. Inferred from the filename when omitted.",
    },
    "prefix": {
        "type": "string",
        "description": "Optional key prefix / folder inside the bucket (default 'uploads').",
    },
    "expires_in": {
        "type": "integer",
        "description": "Presigned URL lifetime in seconds (60 to 604800, default 3600). Ignored when the bucket has a public base URL configured.",
    },
}

def _filename_from_url(source_url: str) -> str:
    path = urlparse(source_url).path
    tail = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return sanitize_filename(tail, fallback="download")

def _decode_base64(file_base64: str) -> bytes:
    value = (file_base64 or "").strip()
    if value.startswith("data:"):
        _, _, value = value.partition(",")
        value = value.strip()
    if not value:
        raise ValueError("file_base64 is empty.")
    # Cheap length pre-check before allocating the decoded buffer.
    if len(value) > (MAX_UPLOAD_BYTES * 4 // 3) + 100:
        raise ValueError("File exceeds the 100 MB upload limit.")
    try:
        content = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("file_base64 must be valid base64.") from exc
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the 100 MB upload limit.")
    return content

async def _store(credentials: dict,
    content: bytes,
    filename: str,
    content_type: str | None,
    prefix: str | None,
    expires_in: int | None,
) -> dict:
    cfg = build_r2_config(credentials)
    key = build_object_key(filename, prefix)
    ctype = guess_content_type(filename, content_type)
    url, is_public, expiry = await upload_and_link(cfg, key, content, ctype, expires_in)
    logger.info(
        "r2_upload_ok",
        bucket=cfg.bucket,
        key=key,
        size_bytes=len(content),
        is_public=is_public,
    )
    return {
        "url": url,
        "is_public": is_public,
        "key": key,
        "bucket": cfg.bucket,
        "content_type": ctype,
        "size_bytes": len(content),
        "expires_in": expiry,
    }

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

MIN_PRESIGNED_EXPIRES = 60

MAX_PRESIGNED_EXPIRES = 604800  # 7 days (S3/R2 upper bound)

DEFAULT_PRESIGNED_EXPIRES = 3600

_R2_REGION = "auto"

_S3_CONFIG = Config(
    connect_timeout=10,
    read_timeout=60,
    s3={"addressing_style": "path"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

def _cred(credentials: dict, key: str, env_key: str) -> str:
    """Read a credential from the per-request dict, falling back to an env var.

    Per-connection headers (platform mode) take precedence; env vars let the
    server run standalone from a .env for local testing against any
    S3-compatible endpoint (e.g. MinIO).
    """
    return (credentials.get(key) or os.getenv(env_key) or "").strip()

def build_r2_config(credentials: dict) -> R2Config:
    """Build a validated R2Config from a tool credentials dict (env fallback).

    The endpoint is taken from an explicit override (``r2_endpoint_url`` /
    ``R2_ENDPOINT_URL``) when set - useful for R2 jurisdiction endpoints or a
    local MinIO - otherwise derived from the Cloudflare account id.

    Raises ValueError listing any missing required credential.
    """
    account_id = _cred(credentials, "r2_account_id", "R2_ACCOUNT_ID")
    access_key_id = _cred(credentials, "r2_access_key_id", "R2_ACCESS_KEY_ID")
    secret_access_key = _cred(credentials, "r2_secret_access_key", "R2_SECRET_ACCESS_KEY")
    bucket = _cred(credentials, "r2_bucket", "R2_BUCKET")
    public_base_url = _cred(credentials, "r2_public_base_url", "R2_PUBLIC_BASE_URL") or None
    endpoint_override = _cred(credentials, "r2_endpoint_url", "R2_ENDPOINT_URL") or None

    missing = [
        label
        for label, value in (
            ("access_key_id", access_key_id),
            ("secret_access_key", secret_access_key),
            ("bucket", bucket),
        )
        if not value
    ]
    if not endpoint_override and not account_id:
        missing.append("account_id or endpoint_url")
    if missing:
        raise ValueError("Missing Cloudflare R2 credentials: " + ", ".join(missing))

    endpoint_url = endpoint_override or f"https://{account_id}.r2.cloudflarestorage.com"
    return R2Config(
        account_id=account_id,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        bucket=bucket,
        public_base_url=public_base_url.rstrip("/") if public_base_url else None,
        endpoint_url=endpoint_url,
    )

def _session(cfg: R2Config) -> aioboto3.Session:
    return aioboto3.Session(
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name=_R2_REGION,
    )

def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Return an object-key-safe filename (no slashes, control chars, spaces)."""
    cleaned = _FILENAME_SAFE_RE.sub("-", (name or "").strip()).strip("-.")
    return cleaned[:200] or fallback

def guess_content_type(filename: str, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"

def build_object_key(filename: str, prefix: str | None = None) -> str:
    """Build a collision-free object key: ``<prefix>/<YYYY/MM/DD>/<uuid>_<name>``."""
    safe_prefix = (prefix or "uploads").strip("/") or "uploads"
    safe_name = sanitize_filename(filename)
    date = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    return f"{safe_prefix}/{date}/{uuid.uuid4().hex}_{safe_name}"

def clamp_expiry(expires_in: int | None) -> int:
    if not expires_in:
        return DEFAULT_PRESIGNED_EXPIRES
    return max(MIN_PRESIGNED_EXPIRES, min(int(expires_in), MAX_PRESIGNED_EXPIRES))

def public_url(cfg: R2Config, key: str) -> str | None:
    """Permanent public URL when a public base is configured, else None."""
    if not cfg.public_base_url:
        return None
    return f"{cfg.public_base_url}/{key}"

async def upload_and_link(
    cfg: R2Config,
    key: str,
    content: bytes,
    content_type: str,
    expires_in: int | None,
) -> tuple[str, bool, int | None]:
    """Upload bytes to R2 and return ``(url, is_public, expires_in)``.

    A single client session does both the PUT and the presign. When a public
    base URL is configured the object is served publicly (no signing) and
    ``expires_in`` is None; otherwise a presigned GET URL is returned.
    """
    session = _session(cfg)
    async with session.client("s3", endpoint_url=cfg.endpoint_url, config=_S3_CONFIG) as s3:
        await s3.put_object(
            Bucket=cfg.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            ContentLength=len(content),
        )
        pub = public_url(cfg, key)
        if pub:
            return pub, True, None
        expiry = clamp_expiry(expires_in)
        url = await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.bucket, "Key": key},
            ExpiresIn=expiry,
        )
        return url, False, expiry

_MAX_REDIRECTS = 5

_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; magoneai-cloudflare-r2/1.0)"}

def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """The shared check (`magone_mcp_core.net`), not a private copy.

    This connector carried its own list of blocked ranges, one of three in
    the v1 catalogue with three different gap sets. It keeps what the shared
    check does not do - https only, internal hostnames refused, the connected
    peer re-checked - and delegates the address decision.
    """
    return is_blocked_address(ip)


def _validate_url(url: str) -> None:
    """Validate scheme + host + every resolved IP. Raise on failure."""
    if not url or not isinstance(url, str):
        raise RemoteFetchError("URL is empty")

    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise RemoteFetchError("URL must start with https://")

    host = (parsed.hostname or "").lower()
    if not host:
        raise RemoteFetchError("URL is missing a hostname")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise RemoteFetchError("Internal hostnames are not allowed")

    try:
        literal = ipaddress.ip_address(host)
        if _is_blocked_ip(literal):
            raise RemoteFetchError("URL points to a private or reserved IP")
        return
    except ValueError:
        pass  # not a literal IP, resolve it below

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise RemoteFetchError(f"Could not resolve host: {host}") from exc

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise RemoteFetchError("URL resolves to a private or reserved IP")

def _reject_private_peer(response: httpx.Response) -> None:
    """Re-check the IP we actually connected to (defeats DNS rebinding)."""
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
    if _is_blocked_ip(ip):
        raise RemoteFetchError("URL connected to a private or reserved IP")

async def fetch_remote_file(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Fetch ``url`` over https with SSRF guards and a hard size cap.

    Returns the response body as bytes. Raises RemoteFetchError on any
    validation failure, transport error, oversize body, or too many
    redirects.
    """
    current = url.strip()
    request_headers = {**_DEFAULT_HEADERS, **(headers or {})}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, headers=request_headers
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                _validate_url(current)
                async with client.stream("GET", current) as response:
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
        raise RemoteFetchError(f"Failed to fetch URL: {exc}") from exc
    raise RemoteFetchError("Too many redirects")



# credential_headers: ['X-R2-Account-Id', 'X-R2-Access-Key-Id', 'X-R2-Secret-Access-Key', 'X-R2-Bucket', 'X-R2-Public-Base-Url', 'X-R2-Endpoint-Url']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: _store() -> upload_and_link() -> object upload to R2. Writes a new
# key; destructiveHint stays False because it does not delete.
@server.tool(
    name='upload_file',
    description="Upload a file to Cloudflare R2 and return a shareable download URL. Pass the file content as base64 (or a data: URL). NOTE: if you already have a file_id, use upload_file_by_id instead - passing file content as base64 through the model can corrupt it. Use this only for content you generated inline. Returns a permanent public URL when the connection has a public base URL configured, otherwise a presigned URL. Max 100 MB.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-R2-Account-Id', 'X-R2-Access-Key-Id', 'X-R2-Secret-Access-Key', 'X-R2-Bucket', 'X-R2-Public-Base-Url', 'X-R2-Endpoint-Url'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'required': ['filename', 'file_base64'],
     'properties': {'filename': {'type': 'string',
                                 'description': 'Filename with extension, e.g. '
                                                "'report.pdf'. Drives the stored name and "
                                                'inferred content type.'},
                    'file_base64': {'type': 'string',
                                    'description': 'File content as base64, optionally a '
                                                   "'data:<mime>;base64,...' URL."},
                    'content_type': {'type': 'string',
                                     'description': 'MIME type override, e.g. '
                                                    "'application/pdf'. Inferred from the "
                                                    'filename when omitted.'},
                    'prefix': {'type': 'string',
                               'description': 'Optional key prefix / folder inside the '
                                              "bucket (default 'uploads')."},
                    'expires_in': {'type': 'integer',
                                   'description': 'Presigned URL lifetime in seconds (60 '
                                                  'to 604800, default 3600). Ignored when '
                                                  'the bucket has a public base URL '
                                                  'configured.'}}})
@declare_output_schema({'description': "The stored object's shareable URL and metadata.",
                        'properties': {'bucket': {'type': 'string'},
                                       'content_type': {'type': 'string'},
                                       'expires_in': {'description': 'Presigned URL lifetime in seconds, or null when '
                                                                     'the URL is public.',
                                                      'type': ['integer', 'null']},
                                       'file_id': {'description': 'Source workflow file id, when uploaded via '
                                                                  'upload_file_by_id.',
                                                   'type': 'string'},
                                       'is_public': {'description': 'True for a permanent public URL, False for a '
                                                                    'time-limited presigned URL.',
                                                     'type': 'boolean'},
                                       'key': {'description': 'Object key within the bucket.', 'type': 'string'},
                                       'size_bytes': {'type': 'integer'},
                                       'url': {'description': 'Public or presigned download URL.', 'type': 'string'}},
                        'type': 'object'})
async def upload_file(credentials: dict,
    filename: str,
    file_base64: str,
    content_type: str | None = None,
    prefix: str | None = None,
    expires_in: int | None = None,
) -> dict:
    try:
        content = _decode_base64(file_base64)
        return await _store(
            credentials, content, filename, content_type, prefix, expires_in
        )
    except ValueError as exc:
        return {"error": str(exc)}






# credential_headers: ['X-R2-Account-Id', 'X-R2-Access-Key-Id', 'X-R2-Secret-Access-Key', 'X-R2-Bucket', 'X-R2-Public-Base-Url', 'X-R2-Endpoint-Url']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: fetch_remote_file() then _store() -> upload_and_link(). The fetch is
# a read of someone else's URL; the upload is the write.
@server.tool(
    name='upload_from_url',
    description="Fetch a file from a public https URL and upload it to Cloudflare R2, returning a shareable download URL. The source URL is fetched with SSRF protection (https only, no private/internal hosts). Max 100 MB.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-R2-Account-Id', 'X-R2-Access-Key-Id', 'X-R2-Secret-Access-Key', 'X-R2-Bucket', 'X-R2-Public-Base-Url', 'X-R2-Endpoint-Url'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'required': ['source_url'],
     'properties': {'source_url': {'type': 'string',
                                   'description': 'Public https URL of the file to mirror '
                                                  'into R2.'},
                    'filename': {'type': 'string',
                                 'description': 'Optional stored filename. Derived from '
                                                'the source URL when omitted.'},
                    'content_type': {'type': 'string',
                                     'description': 'MIME type override, e.g. '
                                                    "'application/pdf'. Inferred from the "
                                                    'filename when omitted.'},
                    'prefix': {'type': 'string',
                               'description': 'Optional key prefix / folder inside the '
                                              "bucket (default 'uploads')."},
                    'expires_in': {'type': 'integer',
                                   'description': 'Presigned URL lifetime in seconds (60 '
                                                  'to 604800, default 3600). Ignored when '
                                                  'the bucket has a public base URL '
                                                  'configured.'}}})
@declare_output_schema({'description': "The stored object's shareable URL and metadata.",
                        'properties': {'bucket': {'type': 'string'},
                                       'content_type': {'type': 'string'},
                                       'expires_in': {'description': 'Presigned URL lifetime in seconds, or null when '
                                                                     'the URL is public.',
                                                      'type': ['integer', 'null']},
                                       'file_id': {'description': 'Source workflow file id, when uploaded via '
                                                                  'upload_file_by_id.',
                                                   'type': 'string'},
                                       'is_public': {'description': 'True for a permanent public URL, False for a '
                                                                    'time-limited presigned URL.',
                                                     'type': 'boolean'},
                                       'key': {'description': 'Object key within the bucket.', 'type': 'string'},
                                       'size_bytes': {'type': 'integer'},
                                       'url': {'description': 'Public or presigned download URL.', 'type': 'string'}},
                        'type': 'object'})
async def upload_from_url(credentials: dict,
    source_url: str,
    filename: str | None = None,
    content_type: str | None = None,
    prefix: str | None = None,
    expires_in: int | None = None,
) -> dict:
    try:
        content = await fetch_remote_file(source_url, max_bytes=MAX_UPLOAD_BYTES)
        name = filename or _filename_from_url(source_url)
        return await _store(credentials, content, name, content_type, prefix, expires_in)
    except (ValueError, RemoteFetchError) as exc:
        return {"error": str(exc)}
