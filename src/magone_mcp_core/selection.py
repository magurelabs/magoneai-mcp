"""Which tools one connection may use, out of everything the server can do.

A connection rarely wants a connector's whole surface. The platform asks for a
selection when a connection is made, and sends it on every call:

- ``X-MCP-Enabled-Tools``  - explicit tool names, comma-separated
- ``X-MCP-Enabled-Groups`` - permission groups, comma-separated

v1 narrowed itself to those two headers in its protocol layer. This repository
dropped them in the port while per-tool scope enforcement was still unbuilt,
which is the one ordering that must never happen: the headers are the only
narrowing that exists today, and removing them first leaves every tool exposed
on every connection.

The platform never stopped sending them, so the gap was silent in both
directions: a connection that selected read-only Jira was served
``deleteJiraIssue``, and nothing in a log said so. This carries the headers
forward, unchanged in meaning, until scope enforcement replaces them.

## Absent is not empty

A header that is missing, blank, or all separators means "no selection was
made", and nothing is filtered. It deliberately does not mean "selected
nothing". An unset platform config value arrives looking exactly like the
latter, and serving zero tools is a very confusing way to report a missing
setting: every call fails, the catalogue looks empty, and the connector is
blamed for it.

## Names beat groups

Naming tools is the more specific statement, so it wins outright when both
headers arrive. Honouring both would mean guessing which the caller meant when
they disagree, and a union of the two is the one reading that can only ever
widen the surface.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

ENABLED_TOOLS_HEADER = "x-mcp-enabled-tools"
ENABLED_GROUPS_HEADER = "x-mcp-enabled-groups"

DEFAULT_GROUP = "default"


class NotEnabled(ToolError):
    """A tool outside this connection's selection was called.

    A `ToolError` for the same reason `ReadOnlyViolation` is one: the SDK
    reports any other exception as a bare "Error executing tool X" and logs an
    ERROR traceback, so a control doing its job reads as a crash and the caller
    never learns why it was refused.
    """


def _selected(headers: Mapping | None, name: str) -> set[str] | None:
    """The comma-separated values of one header, or None when it says nothing."""
    if not headers:
        return None
    for key, value in dict(headers).items():
        if str(key).lower() != name:
            continue
        values = {piece.strip() for piece in str(value).split(",") if piece.strip()}
        return values or None
    return None


def enabled_tool_names(
    headers: Mapping | None,
    groups_by_tool: Mapping[str, str],
    known: Iterable[str] | None = None,
) -> set[str] | None:
    """Tool names this connection may use, or None when it is unrestricted.

    `groups_by_tool` maps every registered tool to its permission group.
    `known` defaults to its keys and exists so a caller holding a fuller
    catalogue than the group map can still intersect against the real one.

    A name in the header that this server does not serve is dropped rather
    than reported: the selection is a filter over what exists, and the
    platform may hold a selection made before a tool was renamed.
    """
    catalogue = set(known) if known is not None else set(groups_by_tool)

    by_name = _selected(headers, ENABLED_TOOLS_HEADER)
    if by_name is not None:
        return {name for name in catalogue if name in by_name}

    by_group = _selected(headers, ENABLED_GROUPS_HEADER)
    if by_group is not None:
        return {name for name in catalogue if groups_by_tool.get(name, DEFAULT_GROUP) in by_group}
    return None


def narrow_listing(result: Any, allowed: set[str] | None) -> tuple[Any, int, int]:
    """A `tools/list` result holding only `allowed`, with the two counts.

    Takes the result in either shape the SDK may hand a middleware: the
    `ListToolsResult` model, or the already-serialized dict. It was a dict in
    2.3.0 and the annotation says `BaseModel | dict | None`, so reading only
    the model silently filtered nothing - which is how a listing filter goes
    missing while every test that mocks the model passes.
    """
    if allowed is None or result is None:
        return result, 0, 0

    if isinstance(result, Mapping):
        tools = result.get("tools")
        if not isinstance(tools, list):
            return result, 0, 0
        kept = [t for t in tools if _name_of(t) in allowed]
        return {**result, "tools": kept}, len(kept), len(tools)

    tools = getattr(result, "tools", None)
    if not isinstance(tools, list):
        return result, 0, 0
    kept = [t for t in tools if _name_of(t) in allowed]
    return result.model_copy(update={"tools": kept}), len(kept), len(tools)


def _name_of(tool: Any) -> str | None:
    """A listed tool's name, whether it is a model or a dict."""
    if isinstance(tool, Mapping):
        name = tool.get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def assert_enabled(name: str, headers: Mapping | None, groups_by_tool: Mapping[str, str]) -> None:
    """Refuse a tool this connection did not select.

    Filtering `tools/list` is a convenience. This is the control: a client that
    cached an older listing, or that guessed a plausible name, still cannot
    call outside the selection.
    """
    allowed = enabled_tool_names(headers, groups_by_tool)
    if allowed is None or name in allowed:
        return
    raise NotEnabled(
        f"{name!r} is not enabled for this connection. "
        f"Enabled: {', '.join(sorted(allowed)) or 'none'}."
    )
