"""Report a refusal as a failure, and carry an extra content block.

Two behaviours the v1 framework had and the port lost. Both change what a
model sees rather than what a server returns, which is why neither shows up in
a schema diff or a conformance run.

## A refusal is not a success

A tool that returns `{"success": false, "error": "..."}` refused the call.
Reported without `isError`, the platform records a successful tool call, the
model reads "success" and stops, and a refusal written specifically to tell it
what to do instead is never acted on.

v1 recorded the incident that prompted it, and it is worth keeping verbatim
because it is the whole argument:

> Observed: render_document refused to replace a seven slide deck with one and
> said to use revise_document update_slide. The agent made no second call and
> told the user the edit could not be done.

The rule is deliberately narrow: a dict whose `error` is a **non-empty
string**. A tool that legitimately carries an `error` field of another shape,
or a null, is untouched. Widening it would start marking successful calls as
failures, which is the opposite mistake and a worse one.

The payload travels with the flag rather than being replaced by it. Raising
the SDK's `ToolError` would also set `isError`, but it discards the structured
result, and the structured result is frequently where the tool says what to do
instead.

## `_mcp_content`

A dict may carry an `_mcp_content` key holding one extra content block, for a
tool that returns a resource or an image alongside its JSON. The key is
removed from the payload and appended as a second block. One tool in the
catalogue uses it (`outlook_email`), and dropping it would have turned an
attachment into a mention of an attachment.
"""

import json
from typing import Any

from mcp.types import CallToolResult, ContentBlock, TextContent
from pydantic import TypeAdapter, ValidationError

EXTRA_CONTENT_KEY = "_mcp_content"

# Exception types whose message is written for the caller: they describe an
# input problem or a vendor refusal. v1's `_SAFE_EXCEPTIONS`, carried across.
PASS_THROUGH_EXCEPTIONS = (ValueError, TypeError, KeyError)

MAX_ERROR_MESSAGE_CHARS = 500


def client_message(exc: BaseException) -> str:
    """The message a caller sees for a pass-through exception.

    Scrubbed of anything credential-shaped, because vendor messages sometimes
    echo the request, and capped at v1's 500 characters. A `KeyError`'s str()
    is the quoted key, so it is named rather than shown bare.
    """
    from .redact import scrub_text

    message = str(exc) if not isinstance(exc, KeyError) else f"missing key: {exc}"
    message = scrub_text(message or type(exc).__name__)
    if len(message) > MAX_ERROR_MESSAGE_CHARS:
        message = message[:MAX_ERROR_MESSAGE_CHARS] + "..."
    return message


_CONTENT_BLOCK = TypeAdapter(ContentBlock)


def _as_content_block(block: Any) -> Any:
    """Turn a raw `_mcp_content` dict into a typed content block.

    v1 built a raw JSON envelope by hand, so an untyped dict was fine there.
    The SDK validates, so a plain `{"type": "resource", ...}` is rejected
    outright and would take the whole result down with it.

    A block that cannot be parsed becomes text rather than an exception. The
    alternative is a tool that worked in v1 now failing entirely because of the
    shape of an *extra* block, which is a worse outcome than a degraded one -
    and the content is still delivered rather than dropped.
    """
    if isinstance(block, ContentBlock):  # type: ignore[misc, arg-type]
        return block
    try:
        return _CONTENT_BLOCK.validate_python(block)
    except (ValidationError, TypeError):
        return TextContent(type="text", text=json.dumps(block, default=str))


def is_refusal(result: Any) -> bool:
    """Whether a result is a tool declining to do the thing.

    Narrow on purpose: see the module docstring. Anything that is not a dict
    carrying a non-empty string `error` is a success.
    """
    return (
        isinstance(result, dict)
        and isinstance(result.get("error"), str)
        and bool(result["error"].strip())
    )


def to_call_result(result: Any, *, structured: bool = False) -> CallToolResult:
    """Shape a handler's return value for the wire, the way v1 did.

    v1 sent every result as one text block: `json.dumps(result, indent=2,
    default=str)` for a dict or a list, `str(result)` otherwise. Left to the
    SDK, a list becomes one content block **per item**, and a caller reading
    the first block (the platform does) sees one row of a result set. So the
    shape is fixed here, and it is also exactly what the output budget
    measures.

    `structured` is set for a tool that declares an output schema. The
    specification requires such a tool's successful result to carry
    `structuredContent` conforming to it, so its dict payload is sent as both.

    A `CallToolResult` a handler built itself is passed through untouched.
    """
    if isinstance(result, CallToolResult):
        return result

    extra = None
    payload = result
    if isinstance(result, dict) and EXTRA_CONTENT_KEY in result:
        payload = {k: v for k, v in result.items() if k != EXTRA_CONTENT_KEY}
        extra = result[EXTRA_CONTENT_KEY]

    refused = is_refusal(payload)
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, indent=2, default=str)
    else:
        text = str(payload)

    content: list = [TextContent(type="text", text=text)]
    if extra is not None:
        content.append(_as_content_block(extra))

    structured_payload = (
        payload if structured and not refused and isinstance(payload, dict) else None
    )
    return CallToolResult(content=content, structuredContent=structured_payload, isError=refused)
