"""Graph upload transport: simple PUT for small files, upload sessions for large.

Upload sessions follow Graph's resumable protocol: createUploadSession returns
a pre-authorized uploadUrl, fragments go up with Content-Range, a 202 names
the next expected range, and the final fragment returns the driveItem. On a
5xx or 429 the session is queried (GET uploadUrl) and resumed from the range
Graph still expects, after waiting as long as Retry-After asks.

The uploadUrl is pre-authorized and must never receive the bearer token.
"""

import asyncio
from typing import Any
from urllib.parse import urlparse

import httpx

from .._shared import GRAPH_API_URL
from . import _constants
from .upload_input import UploadPayload

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 60.0
UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

_STATUS_MESSAGES = {
    401: "Authentication failed. Reconnect OneDrive.",
    403: "Access denied. Reconnect OneDrive to grant Files.ReadWrite.",
    409: "A file with this name already exists (conflict_behavior=fail).",
}


class UploadError(Exception):
    """An upload failed; the message is safe to show the model."""


async def sleep(seconds: float) -> None:
    """Indirection so tests can record waits instead of sleeping."""
    await asyncio.sleep(seconds)


def retry_delay(response: httpx.Response, attempt: int) -> float:
    """Retry-After in seconds when Graph sends it, else capped exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(int(retry_after)), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return min(2.0**attempt, MAX_BACKOFF_SECONDS)


def _error(response: httpx.Response) -> UploadError:
    known = _STATUS_MESSAGES.get(response.status_code)
    if known:
        return UploadError(known)
    try:
        detail = (response.json().get("error") or {}).get("message") or ""
    except ValueError:
        detail = response.text[:300]
    return UploadError(f"Upload failed ({response.status_code}): {detail}".rstrip(": "))


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """One Graph request, retried on throttling and transient server errors."""
    for attempt in range(MAX_RETRIES + 1):
        response = await client.request(method, url, **kwargs)
        if response.status_code not in RETRYABLE_STATUSES or attempt == MAX_RETRIES:
            return response
        await sleep(retry_delay(response, attempt + 1))
    return response


async def simple_put(
    client: httpx.AsyncClient, token: str, path: str, payload: UploadPayload, conflict: str
) -> dict[str, Any]:
    response = await _send(
        client,
        "PUT",
        f"{GRAPH_API_URL}{path}/content",
        content=payload.content,
        headers={"Authorization": f"Bearer {token}", "Content-Type": payload.content_type},
        params={"@microsoft.graph.conflictBehavior": conflict},
    )
    if response.status_code not in (200, 201):
        raise _error(response)
    return response.json()


async def create_session(
    client: httpx.AsyncClient, token: str, path: str, filename: str, conflict: str
) -> str:
    body = {"item": {"@microsoft.graph.conflictBehavior": conflict, "name": filename}}
    response = await _send(
        client,
        "POST",
        f"{GRAPH_API_URL}{path}/createUploadSession",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code != 200:
        raise _error(response)
    upload_url = str(response.json().get("uploadUrl") or "")
    if urlparse(upload_url).scheme != "https":
        raise UploadError("Graph returned no usable upload URL.")
    return upload_url


def _next_offset(response: httpx.Response, default: int) -> int:
    """Start of the first range Graph still expects ("26214400-" form)."""
    try:
        ranges = response.json().get("nextExpectedRanges") or []
        return int(str(ranges[0]).split("-")[0])
    except (ValueError, IndexError, AttributeError):
        return default


async def _resume_offset(client: httpx.AsyncClient, upload_url: str, current: int) -> int:
    response = await client.get(upload_url)
    if response.status_code == 404:
        raise UploadError("The upload session expired. Try the upload again.")
    if response.status_code != 200:
        return current
    return _next_offset(response, current)


async def upload_chunks(
    client: httpx.AsyncClient, upload_url: str, content: bytes, chunk_size: int
) -> dict[str, Any]:
    """PUT `content` in Content-Range fragments, resuming after transient failures."""
    total, offset, failures = len(content), 0, 0
    while True:
        end = min(offset + chunk_size, total) - 1
        response = await client.put(
            upload_url,
            content=content[offset : end + 1],
            headers={"Content-Range": f"bytes {offset}-{end}/{total}"},
        )
        if response.status_code in (200, 201):
            return response.json()
        if response.status_code == 202:
            expected = _next_offset(response, end + 1)
            if expected <= offset or expected > total:
                raise UploadError("Graph stopped accepting fragments. Try the upload again.")
            offset, failures = expected, 0
            continue
        if response.status_code not in RETRYABLE_STATUSES or failures >= MAX_RETRIES:
            raise _error(response)
        failures += 1
        await sleep(retry_delay(response, failures))
        offset = await _resume_offset(client, upload_url, offset)


async def _cancel(client: httpx.AsyncClient, upload_url: str) -> None:
    """Best effort: drop the half-written session so it does not linger."""
    try:
        await client.delete(upload_url)
    except httpx.HTTPError:
        pass


async def upload_bytes(
    client: httpx.AsyncClient,
    token: str,
    path: str,
    payload: UploadPayload,
    conflict: str,
) -> tuple[dict[str, Any], str]:
    """Upload and return (driveItem, method), method being "simple" or "session"."""
    if len(payload.content) <= _constants.SIMPLE_UPLOAD_MAX_BYTES:
        return await simple_put(client, token, path, payload, conflict), "simple"
    upload_url = await create_session(client, token, path, payload.filename, conflict)
    try:
        item = await upload_chunks(
            client, upload_url, payload.content, _constants.UPLOAD_CHUNK_BYTES
        )
    except Exception:
        await _cancel(client, upload_url)
        raise
    return item, "session"
