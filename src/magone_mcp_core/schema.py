"""Declaring a tool's input schema explicitly, instead of deriving it.

The SDK builds a tool's schema from the handler's type hints. That is the right
default for a tool written today, and the wrong one for a tool that already
exists and is already trusted: derivation invents a ``title``, adds a ``title``
to every property, and drops every ``description``.

Measured on one real tool, derivation turned

    {"query": {"type": "string", "description": "Search query"}}

into

    {"query": {"type": "string", "title": "Query"}}

which is a silent behaviour change for any agent reading the description to
decide what to pass. Across the catalogue that would have discarded 842
property descriptions.

So a ported tool declares its schema, and this module is how. The schema is
injected into the SDK's own ``Tool.parameters``, so the wire output is the
declared object byte for byte, verified by a golden-schema test rather than by
review.
"""

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool as SDKTool
from mcp.types import ToolAnnotations

from .text_args import preserve_declared_text

# Attribute stamped on a handler by `declare_schema`, read by `register_tool`.
_SCHEMA_ATTR = "_magone_input_schema"

# Attribute stamped by `declare_output_schema`, read by `register_tool`.
_OUTPUT_ATTR = "_magone_output_schema"


def declare_schema(schema: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach an explicit input schema to a tool handler.

    Use on a ported tool whose schema is already written and already relied on.
    New tools should let the SDK derive from type hints instead, which is why
    this is opt-in rather than the default.

        @declare_schema({"type": "object", "properties": {...}, "required": [...]})
        async def search_web(query: str, num_results: int = 5) -> dict:
            ...
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, _SCHEMA_ATTR, schema)
        return fn

    return decorator


def declared_schema(fn: Callable[..., Any]) -> dict[str, Any] | None:
    """The schema declared on a handler, or None if it derives from type hints."""
    return getattr(fn, _SCHEMA_ATTR, None)


def declare_output_schema(
    schema: dict[str, Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach an explicit output schema to a tool handler.

    The output half of `declare_schema`, for the same reason. v1 declared an
    output schema on 101 tools and published it in `tools/list`; the platform
    reads it to offer a TOOL node's output fields downstream. Without a
    declaration the SDK derives one from `-> Dict[str, Any]`, which is a
    wrapper, `{"result": {...}}`, describing nothing, and it then wraps every
    result to match. So a ported tool carries its v1 output schema verbatim
    and its result is the payload itself.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, _OUTPUT_ATTR, schema)
        return fn

    return decorator


def declared_output_schema(fn: Callable[..., Any]) -> dict[str, Any] | None:
    """The output schema declared on a handler, or None."""
    return getattr(fn, _OUTPUT_ATTR, None)


def register_tool(
    server: MCPServer,
    fn: Callable[..., Any],
    *,
    name: str,
    description: str,
    annotations: ToolAnnotations | None = None,
    title: str | None = None,
    meta: dict[str, Any] | None = None,
) -> SDKTool:
    """Register a tool, honouring an explicitly declared schema.

    Returns the registered SDK tool so a caller can assert on it.

    The SDK exposes no public way to supply `parameters`, so the tool is built
    through the SDK's own ``from_function`` and its schema replaced before
    registration. That keeps every other property the SDK computes (async
    detection, context argument, structured-output handling) rather than
    reimplementing them.
    """
    # `structured_output=False`: the SDK would otherwise derive an output
    # model from the return annotation, advertise it, and validate every
    # result against it. For `-> Dict[str, Any]`, which is most of the
    # catalogue, that model is a `{"result": ...}` wrapper v1 never had, and a
    # successful CallToolResult with no structured content fails it outright.
    # The output contract is the declared one or none, as in v1.
    tool = SDKTool.from_function(
        fn,
        name=name,
        description=description,
        title=title,
        annotations=annotations,
        structured_output=False,
    )
    schema = declared_schema(fn)
    if schema is not None:
        tool.parameters = schema
    output = declared_output_schema(fn)
    if output is not None:
        tool.fn_metadata.output_schema = output
    # Last of the schema edits, so it is decided against exactly the schema a
    # client is shown: an argument that schema calls text is passed through as
    # sent, never JSON-decoded by the SDK's pre-parse. See `text_args.py`.
    preserve_declared_text(tool)
    if meta is not None:
        # Carried to the client as `_meta` on the listed tool. Set after
        # `from_function` because that is where the SDK would have taken it,
        # and it is not part of the input schema the golden gate pins.
        tool.meta = meta
    server._tool_manager._tools[tool.name] = tool
    return tool
