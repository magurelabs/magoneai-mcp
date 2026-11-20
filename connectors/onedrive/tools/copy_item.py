"""Graph's asynchronous copy: start it, then poll the monitor it hands back.

Graph answers `POST .../copy` with 202 and a `Location` monitor URL; the copy
only exists once the monitor says `completed`. The monitor URL is
pre-authenticated, like an upload URL, so it never receives the bearer token.
"""

from typing import Any

import httpx

from .._shared import GRAPH_API_URL
from . import _constants, upload_graph
from ._constants import encode_id

_COPY_TIMEOUT = 60.0


def _failure(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 409:
        return {
            "success": False,
            "error": "An item with this name already exists in the destination",
        }
    try:
        message = (response.json().get("error") or {}).get("message") or ""
    except ValueError:
        message = response.text[:300]
    return {"success": False, "error": f"Copy failed ({response.status_code}): {message}"}


async def _poll(client: httpx.AsyncClient, monitor_url: str) -> dict[str, Any] | None:
    """The finished copy, or None if it is still running after the poll budget."""
    for _ in range(_constants.COPY_POLL_ATTEMPTS):
        await upload_graph.sleep(_constants.COPY_POLL_INTERVAL_SECONDS)
        poll = await client.get(monitor_url)
        if poll.status_code != 200:
            continue
        status = poll.json().get("status")
        if status == "completed":
            resource = poll.json().get("resourceId", "")
            return {"success": True, "status": "completed", "resource_id": resource}
        if status == "failed":
            return {"success": False, "error": "Copy operation failed on server"}
    return None


async def copy(token: str, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_COPY_TIMEOUT) as client:
        response = await client.post(
            f"{GRAPH_API_URL}/me/drive/items/{encode_id(item_id)}/copy",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "respond-async",
            },
        )
        if response.status_code not in (200, 201, 202):
            return _failure(response)
        monitor_url = response.headers.get("Location")
        if not monitor_url:
            return {"success": True, "status": "accepted", "note": "Copy started asynchronously"}
        finished = await _poll(client, monitor_url)
    if finished is not None:
        return finished
    return {"success": True, "status": "in_progress", "note": "Copy is still in progress"}
