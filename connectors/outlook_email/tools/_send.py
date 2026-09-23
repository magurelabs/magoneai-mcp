"""Send orchestration for the Outlook send_email tool.

Split out of send_email.py to keep it under the 200-line limit: send_email.py
does validation + dispatch, this module implements the two Graph send
strategies (draft-then-send vs sendMail), attachment gathering, and
orphan-draft cleanup.

`graph` is the MicrosoftGraph mixin instance (the tool's `self`), which
provides `_graph_request`, `_get_access_token`, `_upload_large_attachment`,
and `logger`.
"""

import binascii
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ._budget import AttachmentBudgetExceeded, SizeBudget
from ._constants import MAX_ATTACHMENTS, build_inline_attachment
from .download import download_attachments

# Request Graph immutable IDs on draft creation so the returned message id
# survives the Drafts to Sent Items move and stays resolvable after send.
IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'


async def gather_attachments(
    attachment_urls: Optional[List[str]],
    attachment_filename: Optional[str],
    attachments: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Merge attachment_urls (fetched here) and attachments (pre-fetched
    base64, e.g. from the platform's file_id resolver) into one Graph-ready
    result, sharing a single count/size budget across both.

    The budget is charged as bytes arrive, not tallied at the end, so an
    over-limit batch aborts before the rest of it is resident.

    Returns (inline_attachments, large_attachments, attachments_info, error).
    On error the first three are empty and the caller should return it as-is.
    """
    if (len(attachment_urls or []) + len(attachments or [])) > MAX_ATTACHMENTS:
        return [], [], [], f"Too many attachments, max {MAX_ATTACHMENTS}"

    inline: List[Dict[str, Any]] = []
    large: List[Dict[str, Any]] = []
    info: List[Dict[str, Any]] = []
    budget = SizeBudget()

    if attachment_urls:
        try:
            url_inline, url_large, url_info = await download_attachments(
                attachment_urls, attachment_filename, budget
            )
        except AttachmentBudgetExceeded as e:
            return [], [], [], str(e)
        except (ValueError, httpx.HTTPError) as e:
            return [], [], [], f"Failed to download attachment: {e}"
        inline += url_inline
        large += url_large
        info += url_info

    for position, item in enumerate(attachments or [], start=1):
        label = _attachment_label(item, position)
        try:
            graph_attachment, large_attachment, item_info = build_inline_attachment(item)
            budget.take(item_info["size_bytes"])
        except AttachmentBudgetExceeded as e:
            return [], [], [], str(e)
        except (binascii.Error, ValueError) as e:
            return [], [], [], f"Invalid attachment ({label}): {e}"
        info.append(item_info)
        if graph_attachment is not None:
            inline.append(graph_attachment)
        if large_attachment is not None:
            large.append(large_attachment)

    return inline, large, info, None


def _attachment_label(item: Any, position: int) -> str:
    """Name an attachment for error messages, by filename when it has a usable
    one and by position otherwise - with several attachments in a call, "one of
    them failed" is not actionable."""
    if isinstance(item, str) and item:
        return item[:80]
    if isinstance(item, dict):
        for key in ("filename", "file_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value[:80]
    return f"#{position}"


async def _cleanup_draft(graph: Any, credentials: Dict[str, str], draft_id: str) -> None:
    """Best-effort delete of an unsent draft after a failed send."""
    try:
        await graph._graph_request(credentials, "DELETE", f"/me/messages/{draft_id}")
    except Exception:
        graph.logger.warning("send_email_orphan_draft_cleanup_failed", draft_id=draft_id)


async def send_via_sendmail(
    graph: Any,
    credentials: Dict[str, str],
    message_body: Dict[str, Any],
) -> Optional[str]:
    """Send via /me/sendMail (used when save_to_sent is False).

    Graph returns 202 with no body, so no message id is available; returns
    None. saveToSentItems is False here by construction.
    """
    await graph._graph_request(
        credentials,
        "POST",
        "/me/sendMail",
        json_data={"message": message_body, "saveToSentItems": False},
    )
    return None


async def send_via_draft(
    graph: Any,
    credentials: Dict[str, str],
    message_body: Dict[str, Any],
    large_attachments: List[Dict[str, Any]],
) -> Optional[str]:
    """Create a draft (requesting an immutable id), stream any large
    attachments into it, then send it. Returns the immutable message id.

    On any failure the partially-built draft is deleted before re-raising, so
    no orphan draft is left behind. Sending a draft always lands it in Sent
    Items, so this path is only used when save_to_sent is True.
    """
    # Fetch the token once and reuse it across create + upload(s) + send
    # instead of refreshing on each call.
    access_token = await graph._get_access_token(credentials)
    draft_id: Optional[str] = None
    try:
        created = await graph._graph_request(
            credentials,
            "POST",
            "/me/messages",
            json_data=message_body,
            access_token=access_token,
            prefer=IMMUTABLE_ID_PREFER,
        )
        draft_id = created.get("id")
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
            f"/me/messages/{draft_id}/send",
            access_token=access_token,
        )
        return draft_id
    except (ValueError, httpx.HTTPError):
        if draft_id:
            await _cleanup_draft(graph, credentials, draft_id)
        raise
