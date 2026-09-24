"""Cloudflare R2 (S3-compatible) storage helpers: upload + shareable URL.

Credentials arrive per request from the platform (or the playground
credentials file), never from environment. Each helper takes an
``R2Config`` built from the tool's ``credentials`` dict via
``build_r2_config``. R2 uses the ``auto`` region and rejects the newer
default request checksums, so the client config disables them (mirrors the
platform storage backend).
"""

import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import aioboto3
from botocore.config import Config

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


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str | None
    endpoint_url: str


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
