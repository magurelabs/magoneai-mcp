"""Read-only must be enforced from the same environment it is reported from.

`ConnectorServer` takes an `env` so a deployment can be configured explicitly
rather than through the ambient process environment, and it logs
`read_only=...` from that env at startup.

Enforcement did not use it. `assert_callable` consulted `os.environ` directly,
so a server built with `env={"MCP_READ_ONLY": "1"}` logged `read_only=True`
and then allowed every write tool. The banner and the control disagreed, and
the banner is the half a person reads.

In production the two agree, because the variable really is in the process
environment. The divergence appears exactly where the guarantee gets checked:
a test, or any programmatic construction, which is a supported pattern the
constructor exists for.

These assert the two can never separate again.
"""

import pytest
from mcp.types import ToolAnnotations

from magone_mcp_core.readonly import ReadOnlyViolation, assert_callable, read_only_enabled
from magone_mcp_core.server import ConnectorServer

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """The ambient variable must not decide these; the injected env must."""
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)


class TestTheBannerAndTheControlAgree:
    def test_an_injected_read_only_env_refuses_a_write(self):
        """The defect: this passed, and the write went through."""
        server = ConnectorServer(name="t", version="0", env={"MCP_READ_ONLY": "1"})

        with pytest.raises(ReadOnlyViolation):
            assert_callable("db_insert", WRITE, server._env)

    def test_what_the_server_reports_is_what_it_enforces(self):
        server = ConnectorServer(name="t", version="0", env={"MCP_READ_ONLY": "1"})
        reported = read_only_enabled(server._env)

        enforced = True
        try:
            assert_callable("db_insert", WRITE, server._env)
            enforced = False
        except ReadOnlyViolation:
            pass

        assert reported == enforced

    def test_a_read_tool_is_still_allowed(self):
        server = ConnectorServer(name="t", version="0", env={"MCP_READ_ONLY": "1"})

        assert_callable("db_query", READ, server._env)

    def test_a_server_not_in_read_only_allows_writes(self):
        server = ConnectorServer(name="t", version="0", env={})

        assert_callable("db_insert", WRITE, server._env)


class TestTheListingUsesTheSameSource:
    def test_a_read_only_server_offers_only_read_tools(self):
        """The listing filter is a convenience, but it must not contradict the
        call check: a tool offered and then refused is worse than one never
        offered."""
        server = ConnectorServer(name="t", version="0", env={"MCP_READ_ONLY": "1"})

        @server.tool(name="reader", description="Read.", annotations=READ)
        async def reader() -> dict:
            """Read something."""
            return {}

        @server.tool(name="writer", description="Write.", annotations=WRITE)
        async def writer() -> dict:
            """Write something."""
            return {}

        offered = set(server.mcp._tool_manager._tools)
        assert "reader" in offered and "writer" in offered, (
            "registration itself should not filter; the listing handler does"
        )

        for name in offered:
            annotations = server.annotations_for(name)
            allowed = True
            try:
                assert_callable(name, annotations, server._env)
            except ReadOnlyViolation:
                allowed = False
            assert allowed == (name == "reader")


class TestTheFallbackStillWorks:
    def test_no_env_falls_back_to_the_process_environment(self, monkeypatch):
        """Omitting `env` must keep reading os.environ, or every connector
        started normally would stop enforcing."""
        monkeypatch.setenv("MCP_READ_ONLY", "1")

        with pytest.raises(ReadOnlyViolation):
            assert_callable("db_insert", WRITE)
