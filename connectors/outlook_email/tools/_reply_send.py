"""Reply-with-attachments, via Graph's createReply draft flow.

Separate from _send.py because the Graph shapes genuinely differ: sending a new
message can inline attachments in the create call, whereas a reply cannot.
`POST /messages/{id}/reply` sends immediately and has nowhere to put a file, so
attaching one means creating the reply as a DRAFT, adding the attachments to
that draft, and then sending it.

That mismatch is why reply_email had no attachments parameter at all, and why
passing one was silently discarded while the tool still answered "Reply sent".

`graph` is the MicrosoftGraph mixin instance (the tool's `self`).
"""

from typing import Any, Dict, List, Optional

import httpx

from ._send import IMMUTABLE_ID_PREFER, _cleanup_draft


async def reply_via_draft(
    graph: Any,
    credentials: Dict[str, str],
    email_id: str,
    reply_all: bool,
    message_patch: Dict[str, Any],
    inline_attachments: List[Dict[str, Any]],
    large_attachments: List[Dict[str, Any]],
    base_path: str = "/me",
) -> Optional[str]:
    """Create a reply draft, attach, send. Returns the sent message id.

    Attachments are POSTed to the draft one at a time rather than PATCHed onto
    it: Graph does not accept an `attachments` collection on a message PATCH,
    it only exposes the child /attachments navigation property. A PATCH that
    includes them is accepted and silently drops them, which is the same class
    of failure this whole function exists to fix.

    Any failure deletes the half-built draft, so a failed reply never leaves an
    orphan sitting in the user's Drafts folder.
    """
    access_token = await graph._get_access_token(credentials)
    action = "createReplyAll" if reply_all else "createReply"
    draft_id: Optional[str] = None
    try:
        created = await graph._graph_request(
            credentials,
            "POST",
            f"{base_path}/messages/{email_id}/{action}",
            access_token=access_token,
            prefer=IMMUTABLE_ID_PREFER,
        )
        draft_id = created.get("id")
        if not draft_id:
            raise ValueError("createReply returned no draft id")

        if message_patch:
            await graph._graph_request(
                credentials,
                "PATCH",
                f"{base_path}/messages/{draft_id}",
                json_data=message_patch,
                access_token=access_token,
            )

        for att in inline_attachments:
            await graph._graph_request(
                credentials,
                "POST",
                f"{base_path}/messages/{draft_id}/attachments",
                json_data=att,
                access_token=access_token,
            )

        for large in large_attachments:
            await graph._upload_large_attachment(
                credentials,
                draft_id,
                large["filename"],
                large["content"],
                large["content_type"],
                access_token=access_token,
            )

        await graph._graph_request(
            credentials,
            "POST",
            f"{base_path}/messages/{draft_id}/send",
            access_token=access_token,
        )
        return draft_id
    except (ValueError, httpx.HTTPError):
        if draft_id:
            await _cleanup_draft(graph, credentials, draft_id)
        raise
