"""File download helper with SSRF protection."""

import asyncio
import base64
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from magone_mcp_core import SSRFBlocked, validate_url
from magone_mcp_core.net import reject_blocked_peer

from ._budget import SizeBudget
from ._constants import INLINE_ATTACHMENT_LIMIT, MAX_DOWNLOAD_SIZE

MAX_REDIRECTS = 5
# Ceiling on downloads in flight at once. Without it, MAX_ATTACHMENTS URLs at
# MAX_DOWNLOAD_SIZE each can all be resident before the combined-size check
# runs, and the server sets no uvicorn concurrency limit to bound that.
MAX_CONCURRENT_DOWNLOADS = 3
# Many hosts (Cloudflare-fronted CDNs, Wikimedia) reject requests whose
# User-Agent is missing or a scripting-library default (e.g. python-httpx),
# returning 403. Present a browser-like UA so legitimate attachment URLs
# download instead of being blocked at the edge.
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; MagOneAI/1.0; +https://magureinc.com)"


def _sanitize_filename(filename: str) -> str:
    """Strip path components and non-safe characters; fall back to a default name."""
    filename = os.path.basename(filename)
    filename = re.sub(r"[^\w.\-]", "_", filename)
    return filename or "attachment"


def _check_url(url: str) -> None:
    """Refuse a URL that is not http(s) or that resolves to an internal address.

    The address decision is the framework's `validate_url`, the one SSRF check
    in this repository. Its two exception types become `ValueError`, which
    `gather_attachments` reports to the caller as a failed download.
    """
    try:
        validate_url(url)
    except SSRFBlocked as exc:
        raise ValueError(f"Access to internal addresses is not allowed: {exc}") from exc


async def download_file(url: str, budget: Optional[SizeBudget] = None) -> tuple[bytes, str, str]:
    """Download file from URL, return (content, filename, content_type).

    Retries once on a transient RemoteProtocolError (server dropped the
    connection without responding); GET is idempotent so the retry is safe.
    The first attempt's partial bytes are rewound off *budget* before the
    retry re-charges them.
    """
    mark = budget.mark() if budget else 0
    try:
        return await _fetch_file(url, budget)
    except httpx.RemoteProtocolError:
        if budget:
            budget.rewind(mark)
        return await _fetch_file(url, budget)


async def _fetch_file(url: str, budget: Optional[SizeBudget] = None) -> tuple[bytes, str, str]:
    """Single download attempt. Handles redirects manually, validating each hop
    before connecting and the address actually connected to once it is."""
    # Disable auto-follow - we check each redirect target for SSRF before following
    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=False,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    ) as client:
        current_url = url
        for _ in range(MAX_REDIRECTS):
            _check_url(current_url)
            # Use streaming to enforce size limit during download
            async with client.stream("GET", current_url) as response:
                # The client resolved the name again when it connected; judge
                # the address it really reached before reading anything or
                # following a redirect.
                try:
                    reject_blocked_peer(response)
                except SSRFBlocked as exc:
                    raise ValueError(f"Access to internal addresses is not allowed: {exc}") from exc
                if response.is_redirect:
                    next_url = str(response.next_request.url) if response.next_request else None
                    if not next_url:
                        raise ValueError("Redirect without Location header")
                    current_url = next_url
                    continue

                response.raise_for_status()

                # Stream body with size enforcement, against this file's own
                # ceiling and the call-wide budget shared with sibling
                # downloads, so an over-budget batch aborts mid-transfer
                # instead of after every byte is resident.
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(8192):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_SIZE:
                        raise ValueError(
                            f"File exceeds {MAX_DOWNLOAD_SIZE // (1024 * 1024)}MB limit"
                        )
                    if budget:
                        budget.take(len(chunk))
                    chunks.append(chunk)
                content = b"".join(chunks)

                content_disposition = response.headers.get("content-disposition", "")
                filename = None

                if "filename=" in content_disposition:
                    parts = content_disposition.split("filename=")
                    if len(parts) > 1:
                        filename = parts[1].strip("\"'").split(";")[0]

                if not filename:
                    # Use final URL (after redirects) for filename
                    path = urlparse(current_url).path
                    filename = os.path.basename(path) or "attachment"
                    if "?" in filename:
                        filename = filename.split("?")[0]

                filename = _sanitize_filename(filename)

                content_type = response.headers.get(
                    "content-type", "application/octet-stream"
                ).split(";")[0]
                return content, filename, content_type

        raise ValueError("Too many redirects")


async def download_attachments(
    urls: List[str],
    custom_filename: Optional[str] = None,
    budget: Optional[SizeBudget] = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Download attachment URLs and split them by size.

    Files <= INLINE_ATTACHMENT_LIMIT come back ready to embed directly as Graph
    fileAttachment dicts. Larger files (up to MAX_DOWNLOAD_SIZE) come back as
    raw content for the caller to send through Graph's chunked upload-session
    API instead, since Graph can't take them base64-inlined in one request.

    *budget* is the caller's combined-size ceiling, shared with any pre-fetched
    base64 attachments in the same call.

    Returns (inline_attachments, large_attachments, attachments_info).
    """
    inline_attachments: List[Dict[str, str]] = []
    large_attachments: List[Dict[str, Any]] = []
    attachments_info: List[Dict[str, Any]] = []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    async def fetch(url: str) -> tuple[bytes, str, str]:
        async with semaphore:
            return await download_file(url, budget)

    # Downloads are independent - fan them out concurrently instead of paying
    # each URL's latency in sequence, capped by the semaphore above so peak
    # memory stays bounded. return_exceptions=True lets every task finish
    # (success or fail) before we raise, so a failure partway through doesn't
    # leave sibling downloads running unawaited in the background.
    results = await asyncio.gather(*(fetch(url) for url in urls), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result

    for i, (content, filename, content_type) in enumerate(results):
        if i == 0 and custom_filename:
            filename = _sanitize_filename(custom_filename)

        if len(content) <= INLINE_ATTACHMENT_LIMIT:
            inline_attachments.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": content_type,
                    "contentBytes": base64.b64encode(content).decode("utf-8"),
                }
            )
        else:
            large_attachments.append(
                {"filename": filename, "content": content, "content_type": content_type}
            )

        attachments_info.append(
            {"name": filename, "size_bytes": len(content), "content_type": content_type}
        )

    return inline_attachments, large_attachments, attachments_info
