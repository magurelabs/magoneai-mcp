"""An outbound size budget for tool results.

The v1 framework had a 10MB limit and it applied to *inbound* request bodies
only. Nothing bounded what a tool returned, so a single `search_web` over a
large page, or a Jira query matching ten thousand issues, could spend the whole
model context on one call. Most read-style tools declared no bound at all.

The budget lives here rather than in each connector because "every connector
author remembers to paginate" is not a property a catalogue can have. A tool
that exceeds the budget is truncated at a character boundary and told so, in
the result, so the model can narrow its query rather than silently receiving a
partial answer it believes is complete.
"""

import json
import os
from dataclasses import dataclass
from typing import Any

from mcp.types import CallToolResult, TextContent

# One million characters, roughly 250k tokens at 4 characters per token.
#
# The default was 100,000 and that was wrong for this catalogue: v1 had no
# outbound cap at all, and real results from tools that worked there (a Jira
# search, a thread with its bodies, a page of rows) measure around 220,000
# characters. A tool with a declared output schema cannot be cut and still
# conform, so a result over the budget comes back as an error, which turned
# those working calls into failures. The bound still matters, so a runaway
# result cannot take a whole context window or a process's memory with it;
# a deployment that wants a tighter one sets MCP_MAX_RESULT_CHARS.
DEFAULT_MAX_RESULT_CHARS = 1_000_000

# Never truncate below this, or the notice is longer than the content.
MIN_MAX_RESULT_CHARS = 1_000


class BudgetExceeded(Exception):
    """A tool result was larger than the configured budget."""


@dataclass(frozen=True)
class Budget:
    """How much a single tool result may return."""

    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Budget":
        """Read MCP_MAX_RESULT_CHARS, falling back to the default.

        An unparseable value falls back rather than raising: a misconfigured
        budget should not stop a server from starting. A value below the floor
        is raised to the floor, which keeps the truncation notice meaningful.
        """
        source = env if env is not None else os.environ
        raw = str(source.get("MCP_MAX_RESULT_CHARS", "")).strip()
        if not raw:
            return cls()
        try:
            value = int(raw)
        except ValueError:
            return cls()
        # Clamped UP to the floor, never reset to the default. Someone who set
        # 500 wanted a tight budget; silently giving them the default is the
        # one outcome that is certainly wrong.
        return cls(max_result_chars=max(value, MIN_MAX_RESULT_CHARS))


def _render(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def apply_budget(result: Any, budget: Budget) -> Any:
    """Return the result, truncated if it exceeds the budget.

    Truncation is visible by design. A result that has been cut carries
    ``truncated: true``, the byte counts, and a sentence telling the caller to
    narrow the request. A silently shortened list is worse than an error,
    because the model treats it as the complete answer.
    """
    rendered = _render(result)
    if len(rendered) <= budget.max_result_chars:
        return result

    notice = (
        f"Result truncated at {budget.max_result_chars} characters "
        f"(full result was {len(rendered)}). Narrow the request, request fewer "
        "items, or paginate. The content below is incomplete."
    )

    # The metadata is part of the budget, not additional to it. Measure the
    # envelope with empty content first, then give the content whatever is
    # left. Truncating to the budget and *then* adding a notice is how a
    # truncated result ends up larger than the limit it was truncated to.
    envelope = {
        "_truncated": True,
        "_truncation_notice": notice,
        "_original_length": len(rendered),
        "content": "",
    }
    available = budget.max_result_chars - len(_render(envelope))
    if available <= 0:
        # The budget is smaller than the notice itself. Say what happened and
        # nothing else; a fragment of content would not be useful anyway.
        return {
            "_truncated": True,
            "_original_length": len(rendered),
            "_truncation_notice": "Result truncated; the configured budget is "
            "too small to include any content.",
        }

    # Find the longest prefix that still fits once escaped.
    #
    # Slicing to `available` is not enough on its own: the slice is re-embedded
    # as a JSON string, so a quote or backslash in it becomes two characters
    # and the envelope comes out over budget. Subtracting the overshoot is not
    # enough either, because with heavily escaped content one removed character
    # removes several output characters, and the correction overshoots
    # downwards - measured, that left 7 characters of content inside a 5,000
    # character budget.
    #
    # Binary search is exact and bounded at about 17 iterations for the default
    # budget, because the rendered length grows monotonically with the prefix.
    low, high, best = 0, min(available, len(rendered)), 0
    while low <= high:
        mid = (low + high) // 2
        envelope["content"] = rendered[:mid]
        if len(_render(envelope)) <= budget.max_result_chars:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    envelope["content"] = rendered[:best]
    return envelope


def budget_call_result(result: CallToolResult, budget: Budget) -> CallToolResult:
    """Apply the budget to what is actually sent: the result's text blocks.

    Measured after shaping, on the exact text a client receives. Measuring the
    payload before shaping counted compact JSON while the wire carried
    `indent=2`, so a result reported as within budget went out at about twice
    the limit. And budgeting before shaping replaced a refusal with an envelope
    that had no `error` key, so a large refusal arrived as a success.

    What is kept when a result is cut: `isError`, and every non-text block (an
    attachment is not truncated into a fragment of base64). What is dropped:
    `structuredContent`, which would otherwise carry the full payload past the
    budget the text was just cut to.

    A tool with a declared output schema cannot be cut and still conform: a
    conforming client rejects a result from such a tool that carries no
    structured content (the SDK's own client raises). So that cut is reported
    as `isError`, which is also the truth: the declared result did not fit,
    and the notice tells the caller to narrow the request.
    """
    texts = [b for b in result.content if isinstance(b, TextContent)]
    joined = "".join(b.text for b in texts)
    if len(joined) <= budget.max_result_chars:
        return result

    envelope = apply_budget(joined, budget)
    cut = TextContent(type="text", text=_render(envelope))
    others = [b for b in result.content if not isinstance(b, TextContent)]
    return CallToolResult(
        content=[cut, *others],
        structuredContent=None,
        isError=result.is_error or result.structured_content is not None,
    )
