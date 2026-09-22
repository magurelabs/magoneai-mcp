"""A connection is served, and may call, only the tools it selected.

The platform sends `X-MCP-Enabled-Tools` or `X-MCP-Enabled-Groups` on every
call. v1 narrowed itself to them; the port dropped them while the per-tool
scope enforcement meant to replace them was still unbuilt, so a connection that
selected read-only Jira was served `deleteJiraIssue` and could call it.

Over real sockets, because the narrowing reads request headers and because the
listing half rides on an SDK middleware seam the SDK calls semi-public. If that
seam moves, these fail here rather than the filter going quietly missing.
"""

import json
import logging

import httpx
import pytest
from mcp.types import ToolAnnotations
from test_custom_route import serving

from magone_mcp_core import ConnectorServer
from magone_mcp_core.selection import NotEnabled, assert_enabled, enabled_tool_names

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

MODERN = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}
INIT_META = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}


def build() -> ConnectorServer:
    server = ConnectorServer(name="selection-test", version="0.0.0", env={})

    @server.tool(
        name="getIssue", description="Read one.", annotations=READ, permission_group="read_jira"
    )
    async def get_issue() -> dict:
        """A read."""
        return {"ok": "read"}

    @server.tool(
        name="searchIssues",
        description="Search.",
        annotations=READ,
        permission_group="read_jira",
    )
    async def search_issues() -> dict:
        """A read."""
        return {"ok": "search"}

    @server.tool(
        name="deleteIssue",
        description="Delete one.",
        annotations=WRITE,
        permission_group="write_jira",
    )
    async def delete_issue() -> dict:
        """A write that must not be reachable from a read-only selection."""
        return {"ok": "deleted"}

    @server.tool(
        name="deleteProject",
        description="Delete a project.",
        annotations=WRITE,
        permission_group="write_jira",
        credential_headers=["X-Atlassian-Access-Token"],
    )
    async def delete_project(credentials: dict) -> dict:
        """A write whose handler asks for credentials, unlike its siblings."""
        return {"ok": "deleted", "had": bool(credentials)}

    return server


async def rpc(url: str, method: str, params: dict, **headers: str) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**INIT_META, **params}}
    # `Mcp-Name` is not optional on a call: the SDK refuses a `tools/call`
    # whose header and body name disagree, and a missing one counts as a
    # disagreement (-32020).
    routing = {"Mcp-Method": method}
    if "name" in params:
        routing["Mcp-Name"] = str(params["name"])
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:
        response = await client.post("/mcp", headers={**MODERN, **routing, **headers}, json=body)
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        raise AssertionError(f"no data frame in {response.text!r}")
    return response.json()


async def listed(url: str, **headers: str) -> list[str]:
    answer = await rpc(url, "tools/list", {}, **headers)
    return sorted(t["name"] for t in answer["result"]["tools"])


async def called(url: str, name: str, **headers: str) -> dict:
    answer = await rpc(url, "tools/call", {"name": name, "arguments": {}}, **headers)
    return answer["result"]


def text_of(result: dict) -> str:
    return "".join(b.get("text", "") for b in result.get("content", []))


EVERYTHING = ["deleteIssue", "deleteProject", "getIssue", "searchIssues"]


class TestNoSelectionChangesNothing:
    """A server predating the headers, and a connection that selected nothing,
    behave exactly as before: the whole catalogue."""

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-MCP-Enabled-Tools": ""},
            {"X-MCP-Enabled-Groups": ",,"},
        ],
        ids=["absent", "empty", "separators-only"],
    )
    async def test_the_whole_catalogue_is_served(self, headers):
        with serving(build()) as url:
            assert await listed(url, **headers) == EVERYTHING

    async def test_and_every_tool_is_callable(self):
        with serving(build()) as url:
            result = await called(url, "deleteIssue")

        assert result.get("isError") is not True


class TestAGroupSelection:
    async def test_the_listing_holds_only_the_selected_group(self):
        with serving(build()) as url:
            assert await listed(url, **{"X-MCP-Enabled-Groups": "read_jira"}) == [
                "getIssue",
                "searchIssues",
            ]

    async def test_a_tool_outside_it_is_refused_on_the_call(self):
        """The defect this closes: read-only Jira could call deleteJiraIssue."""
        with serving(build()) as url:
            result = await called(url, "deleteIssue", **{"X-MCP-Enabled-Groups": "read_jira"})

        assert result["isError"] is True
        assert "not enabled for this connection" in text_of(result)

    async def test_a_tool_inside_it_still_works(self):
        with serving(build()) as url:
            result = await called(url, "getIssue", **{"X-MCP-Enabled-Groups": "read_jira"})

        assert result.get("isError") is not True
        assert "read" in text_of(result)

    @pytest.mark.parametrize("tool", ["deleteIssue", "deleteProject"])
    async def test_the_control_holds_whether_or_not_the_handler_takes_credentials(self, tool):
        """`ctx` is how the refusal sees the headers, and the framework used to
        inject it only into a handler that asked for `credentials`. A tool
        without them got no ctx, so no headers, so every selection read as
        unrestricted - a control that fails open depending on a handler's own
        signature is not a control. `deleteIssue` takes none; `deleteProject`
        does."""
        with serving(build()) as url:
            result = await called(url, tool, **{"X-MCP-Enabled-Groups": "read_jira"})

        assert result["isError"] is True

    async def test_the_refusal_logs_no_error_traceback(self, caplog):
        """A control doing its job must not read as a crash, or it trips error
        alerting on every refused call. Same reason `ReadOnlyViolation` is a
        `ToolError`."""
        with caplog.at_level(logging.DEBUG):
            with serving(build()) as url:
                await called(url, "deleteIssue", **{"X-MCP-Enabled-Groups": "read_jira"})

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, [r.getMessage() for r in errors]

    async def test_an_unknown_group_selects_nothing(self):
        with serving(build()) as url:
            assert await listed(url, **{"X-MCP-Enabled-Groups": "read_confluence"}) == []


class TestANameSelection:
    async def test_only_the_named_tools_are_served(self):
        with serving(build()) as url:
            assert await listed(url, **{"X-MCP-Enabled-Tools": "getIssue, deleteIssue"}) == [
                "deleteIssue",
                "getIssue",
            ]

    async def test_an_unnamed_tool_is_refused_on_the_call(self):
        with serving(build()) as url:
            result = await called(url, "searchIssues", **{"X-MCP-Enabled-Tools": "getIssue"})

        assert result["isError"] is True

    async def test_names_win_over_groups_when_both_arrive(self):
        """Naming tools is the more specific statement. A union of the two is
        the one reading that can only widen the surface."""
        selection = {"X-MCP-Enabled-Tools": "getIssue", "X-MCP-Enabled-Groups": "write_jira"}
        with serving(build()) as url:
            assert await listed(url, **selection) == ["getIssue"]
            result = await called(url, "deleteIssue", **selection)

        assert result["isError"] is True

    async def test_a_name_this_server_does_not_serve_is_dropped_not_fatal(self):
        """The platform may hold a selection made before a tool was renamed."""
        with serving(build()) as url:
            assert await listed(url, **{"X-MCP-Enabled-Tools": "getIssue,gone"}) == ["getIssue"]


class TestTheResolution:
    """The unit the two halves share."""

    GROUPS = {"a": "read", "b": "read", "c": "write"}

    def test_absent_is_not_empty(self):
        assert enabled_tool_names({}, self.GROUPS) is None
        assert enabled_tool_names({"x-mcp-enabled-groups": "read"}, self.GROUPS) == {"a", "b"}

    @pytest.mark.parametrize("blank", ["", "   ", " , , ", ",,", "\t"])
    def test_a_blank_header_selects_nothing_rather_than_no_tools(self, blank):
        """An unset platform config value arrives looking exactly like this."""
        assert enabled_tool_names({"x-mcp-enabled-groups": blank}, self.GROUPS) is None

    def test_the_header_name_is_matched_whatever_its_case(self):
        assert enabled_tool_names({"X-MCP-ENABLED-TOOLS": "c"}, self.GROUPS) == {"c"}

    def test_a_tool_with_no_group_falls_in_the_default_group(self):
        assert enabled_tool_names({"x-mcp-enabled-groups": "default"}, {"a": "default"}) == {"a"}

    def test_the_refusal_names_what_is_enabled(self):
        with pytest.raises(NotEnabled) as refused:
            assert_enabled("c", {"x-mcp-enabled-groups": "read"}, self.GROUPS)

        assert "a, b" in str(refused.value)

    def test_nothing_is_refused_when_nothing_was_selected(self):
        assert_enabled("c", {}, self.GROUPS)
        assert_enabled("c", None, self.GROUPS)
