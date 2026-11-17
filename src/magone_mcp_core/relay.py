"""Public support for relaying another MCP server's tools through a connector.

A connector that fronts an upstream MCP server — the ``playwright`` connector
relays Microsoft's Playwright MCP — has to register the upstream's tools at
runtime, once it has fetched their list, rather than at import. Doing that by
hand means reaching into the SDK's tool table and the framework's own
bookkeeping, and relaying a call without the guarantees ``@server.tool`` gives a
local one.

``ConnectorServer.register_relayed_tool`` is the public seam for it. A relayed
tool is answered by a handler the connector supplies, but the server applies the
same guarantees it applies to a local tool — read-only mode, per-connection tool
selection and the outbound budget — in ``_run_relayed``. ``before_list_tools``
and ``before_call_tool`` are the matching seams for refreshing the upstream's
tool list before a listing and for intercepting a call to a name not yet
registered, so a connector never has to replace ``list_tools`` or ``call_tool``
itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.tools.base import Tool as SDKTool
from pydantic import Field

# A relayed tool's body: (arguments, request context) -> the upstream's
# CallToolResult (or a dict shaped like one). The server applies the budget to
# whatever it returns, so a handler returns the result unbudgeted.
RelayHandler = Callable[[dict, Any], Awaitable[Any]]


def _placeholder() -> None:
    """Never called: a RelayedTool answers in ``run``."""


class RelayedTool(SDKTool):
    """An upstream tool in the SDK's tool table, answered by its server's handler."""

    server: Any = Field(default=None, exclude=True)
    handler: Any = Field(default=None, exclude=True)

    async def run(self, arguments: dict, context: Any, convert_result: bool = False) -> Any:
        return await self.server._run_relayed(self, arguments, context)
