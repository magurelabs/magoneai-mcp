"""Credentials are supplied by the framework, never sent by the caller.

Every connector here takes its credentials per request, as headers, and the
handler receives them as a `credentials` dict. Before this existed, the port
carried the handler signature across and nothing supplied the argument, so the
SDK's argument model and the advertised schema disagreed and **every tool in
the catalogue failed on every call**:

    Error executing tool search_web: 1 validation error for search_webArguments
    credentials  Field required

267 of 267. Every gate was green: the advertised schemas are correct precisely
because a client does not send credentials, the modules import, conformance
exercises the protocol rather than a vendor call, and the ported unit tests
pass `credentials=` explicitly because the pre-port tests did.

So these tests assert the two halves that have to stay true together: a caller
cannot supply it, and the handler still receives it.
"""

import inspect

import pytest
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema
from magone_mcp_core.credentials import build, header_to_key

# These import real connectors, so they need the connector dependencies.
# `pip install -e ".[dev]"` deliberately installs only the framework's, so an
# outside contributor can run most of the suite with almost nothing. The
# Connector tests job installs `.[dev,all]` and runs exactly these.
pytestmark = pytest.mark.requires_connectors

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "Q"}},
    "required": ["query"],
}


def a_server() -> ConnectorServer:
    return ConnectorServer(name="cred-test", version="0.0.1", env={"MCP_MODE": "local"})


class TestHeaderToKey:
    def test_strips_the_leading_x_and_lowercases(self):
        """`google_refresh_token`, not `refresh_token`.

        v1's own comment says "X-Google-Refresh-Token -> refresh_token" and
        its code produces `google_refresh_token`. The code is what the whole
        catalogue is written against - every handler reads
        `credentials["google_refresh_token"]` - so the code is what this
        matches. The comment was wrong and is not carried across.
        """
        assert header_to_key("X-Google-Refresh-Token") == "google_refresh_token"

    def test_only_the_leading_x_is_stripped(self):
        """A substring replace would eat the `x-` inside `Pptx-Template-Url`
        and produce `ppttemplate_url`. That is a real bug, not a hypothetical."""
        assert header_to_key("Pptx-Template-Url") == "pptx_template_url"

    def test_a_header_without_an_x_prefix_is_untouched_but_normalised(self):
        assert header_to_key("Authorization-Scope") == "authorization_scope"


class TestBuild:
    def test_only_declared_headers_are_passed(self):
        """The declaration is a security statement. A Zoom tool has no
        business receiving a Microsoft refresh token that happens to be on the
        same request."""
        headers = {
            "X-Zoom-Client-Id": "zoom-id",
            "X-Microsoft-Refresh-Token": "not-yours",
        }

        creds = build(headers, ["X-Zoom-Client-Id"])

        assert creds == {"zoom_client_id": "zoom-id"}

    def test_header_matching_is_case_insensitive(self):
        """HTTP header names are case-insensitive and proxies rewrite them."""
        creds = build({"x-zoom-client-id": "v"}, ["X-Zoom-Client-Id"])

        assert creds == {"zoom_client_id": "v"}

    def test_an_empty_header_value_is_not_treated_as_a_credential(self):
        assert build({"X-Zoom-Client-Id": ""}, ["X-Zoom-Client-Id"]) == {}

    def test_no_headers_at_all_is_not_an_error(self):
        assert build(None, ["X-Zoom-Client-Id"]) == {}

    def test_a_tool_that_does_not_declare_the_project_does_not_receive_it(self):
        """An unverified tenancy claim reached every public connector."""
        assert build({"X-Project-Id": "p"}, []) == {}
        assert build({"X-Project-Id": "p"}, ["X-Zoom-Client-Id"]) == {}

    def test_a_tool_that_declares_the_project_receives_it(self):
        assert build({"X-Project-Id": "p"}, ["X-Project-Id"]) == {"project_id": "p"}


class TestTheCallerCannotSupplyCredentials:
    def test_credentials_is_absent_from_the_validated_argument_model(self):
        """The defect, stated as a test. If `credentials` is in the model, a
        caller must send it, and no caller ever does."""
        server = a_server()

        @server.tool(
            name="search",
            description="Search.",
            annotations=READ,
            credential_headers=["X-Api-Key"],
        )
        @declare_schema(SCHEMA)
        async def search(credentials: dict, query: str) -> dict:
            return {"query": query}

        tool = server.mcp._tool_manager._tools["search"]

        assert "credentials" not in set(tool.fn_metadata.arg_model.model_fields)
        assert "query" in set(tool.fn_metadata.arg_model.model_fields)

    def test_the_advertised_schema_never_mentions_credentials(self):
        server = a_server()

        @server.tool(name="s2", description="S.", annotations=READ)
        @declare_schema(SCHEMA)
        async def s2(credentials: dict, query: str) -> dict:
            return {}

        properties = server.mcp._tool_manager._tools["s2"].parameters["properties"]

        assert "credentials" not in properties
        assert "ctx" not in properties

    def test_ctx_is_injected_before_any_var_keyword(self):
        """A keyword-only parameter after `**kwargs` is not a valid signature,
        and appending blindly stopped a real connector importing at all."""
        server = a_server()

        @server.tool(name="s3", description="S.", annotations=READ)
        @declare_schema(SCHEMA)
        async def s3(credentials: dict, query: str, **kwargs) -> dict:
            return {}

        signature = inspect.signature(server.mcp._tool_manager._tools["s3"].fn)
        kinds = [p.kind for p in signature.parameters.values()]

        assert inspect.Parameter.VAR_KEYWORD in kinds
        assert kinds.index(inspect.Parameter.VAR_KEYWORD) == len(kinds) - 1

    def test_a_handler_without_credentials_is_left_alone(self):
        """Not every tool needs them, and the wrapper must not invent a
        parameter for one that does not ask."""
        server = a_server()

        @server.tool(name="s4", description="S.", annotations=READ)
        @declare_schema(SCHEMA)
        async def s4(query: str) -> dict:
            return {}

        fields = set(server.mcp._tool_manager._tools["s4"].fn_metadata.arg_model.model_fields)

        assert "credentials" not in fields
        assert fields == {"query"}


class TestTheHandlerStillReceivesThem:
    async def test_credentials_reach_the_handler(self):
        server = a_server()
        seen = {}

        @server.tool(
            name="s5",
            description="S.",
            annotations=READ,
            credential_headers=["X-Api-Key"],
        )
        @declare_schema(SCHEMA)
        async def s5(credentials: dict, query: str) -> dict:
            seen.update(credentials)
            return {"ok": True}

        tool = server.mcp._tool_manager._tools["s5"]
        await tool.fn(query="x", ctx=None)

        # No request context in a direct call, so no headers: the point here is
        # that the parameter is supplied at all rather than raising TypeError.
        assert seen == {}

    async def test_a_missing_request_context_does_not_raise_from_the_framework(self):
        """A tool that needs a credential and cannot find one should fail in
        its own words about the credential it wanted, not with an
        AttributeError from the wrapper."""
        server = a_server()

        @server.tool(name="s6", description="S.", annotations=READ)
        @declare_schema(SCHEMA)
        async def s6(credentials: dict, query: str) -> dict:
            if not credentials.get("api_key"):
                return {"success": False, "error": "Missing api_key"}
            return {"success": True}

        result = await server.mcp._tool_manager._tools["s6"].fn(query="x", ctx=None)

        # A refusal now arrives as a CallToolResult carrying isError, because
        # reporting it as a success is how a model reads "success" and stops.
        # See `results.py`. The tool's own message still reaches the caller.
        assert result.is_error is True
        assert "Missing api_key" in result.content[0].text


class TestTheWholeCatalogue:
    def test_no_importable_connector_demands_credentials_from_a_caller(self):
        """The regression test for the original defect, across everything that
        can currently be loaded."""
        import importlib
        import pathlib

        offenders = []
        checked = 0
        root = pathlib.Path(__file__).resolve().parents[1] / "connectors"
        for package in sorted(root.iterdir()):
            if not package.is_dir() or package.name.startswith(("_", ".")):
                continue
            try:
                module = importlib.import_module(f"connectors.{package.name}.server")
            except Exception:  # noqa: S112 - see below
                # A connector that does not import is already counted by the
                # port-status budget. Logging it here would duplicate that and
                # make this test's output about something it does not assert.
                continue
            for name, tool in module.server.mcp._tool_manager._tools.items():
                checked += 1
                fields = set(getattr(tool.fn_metadata.arg_model, "model_fields", {}))
                if "credentials" in fields:
                    offenders.append(f"{package.name}/{name}")

        assert checked > 100, f"only {checked} tools loaded; this test would prove little"
        assert not offenders, f"tools a caller cannot call: {offenders[:10]}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
