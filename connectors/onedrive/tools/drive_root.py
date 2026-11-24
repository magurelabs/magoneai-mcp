"""The `parentReference` for a copy or move destination, with the root resolved.

Graph does not accept the `"root"` alias as a destination. The move docs say
so outright ("your app can't use the `"id": "root"` syntax. Your app needs to
provide the actual ID of the root folder"), and the copy docs say the
`parentReference` "should include the driveId and id parameters for the target
folder". So a destination of None or `"root"` is looked up once per call with
`GET /me/drive/root?$select=id,parentReference`, whose `id` is the root folder's
real id and whose `parentReference.driveId` is the drive's id.
"""

from typing import Any

import httpx

from .._shared import GRAPH_API_URL, graph_error

ROOT_ALIAS = "root"
_LOOKUP_TIMEOUT = 30.0


def is_root(folder_id: str | None) -> bool:
    """True for an omitted destination or the `"root"` alias, in any case."""
    return folder_id is None or folder_id.strip().lower() == ROOT_ALIAS


async def _root(token: str) -> tuple[str, str | None]:
    """The drive root's real item id and its drive id."""
    async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT) as client:
        response = await client.get(
            f"{GRAPH_API_URL}/me/drive/root",
            params={"$select": "id,parentReference"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise graph_error(response)
    root = response.json()
    root_id = root.get("id")
    if not root_id:
        raise ValueError("Could not resolve the id of the drive root")
    return root_id, (root.get("parentReference") or {}).get("driveId")


async def parent_reference(
    token: str, folder_id: str | None, *, with_drive_id: bool
) -> dict[str, Any]:
    """`{"id": ..., "driveId": ...}` for the destination folder.

    The root is always resolved to its real id, with its drive id. Another
    folder's id is sent as given, plus the drive id when `with_drive_id` is
    set (copy asks for it; move documents only the id).
    """
    if not is_root(folder_id) and not with_drive_id:
        return {"id": folder_id}
    root_id, drive_id = await _root(token)
    reference: dict[str, Any] = {"id": root_id if is_root(folder_id) else folder_id}
    if drive_id:
        reference["driveId"] = drive_id
    return reference
