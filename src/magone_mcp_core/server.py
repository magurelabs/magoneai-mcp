"""Building a connector server on the official SDK.

Thin on purpose. The SDK owns the protocol, the transport, revision negotiation
and every conformance obligation; this adds the things a connector catalogue
needs and the SDK has no opinion about: a declared input schema, an outbound
budget, a read-only mode, and a bind that refuses to expose an unauthenticated
server.

What this deliberately does **not** do is wrap the SDK's types or hide its API.
An outside contributor should be able to read the SDK's documentation and have
it apply. Every escape hatch stays open: `server.mcp` is the real `MCPServer`,
and `custom_route` is available for the non-MCP endpoints a downstream
deployment still has to serve.
"""

import os
from collections.abc import Callable
from typing import Any

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .budget import Budget, budget_call_result
from .readonly import assert_callable, is_read_tool, read_only_enabled
from .schema import register_tool
from .selection import DEFAULT_GROUP, assert_enabled, enabled_tool_names, narrow_listing

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

VALID_MODES = ("platform", "hosted", "local")


def _request_headers(ctx: Any) -> dict:
    """The inbound HTTP headers, or nothing.

    Reached defensively on purpose. `ctx` is absent when a tool is called
    outside a request (a test, a direct import), and the transport may not
    attach an HTTP request at all. A tool that needs credentials and cannot
    find them should fail in its own words about the credential it wanted, not
    with an AttributeError from the framework.
    """
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
        return dict(request.headers) if request is not None else {}
    except (AttributeError, ValueError, LookupError):
        return {}


# Every header any tool in this process declared in `credential_headers=`,
# lower-cased. `request_header` refuses these, so the one-header reader cannot
# hand a connector a credential that was declared for a different tool.
_DECLARED_CREDENTIAL_HEADERS: set[str] = set()


def is_credential_header(name: str) -> bool:
    """A header `request_header` will not read.

    ``Authorization``, ``Cookie``, ``Proxy-Authorization``, the service-token
    header and anything else whose name the log redaction treats as secret
    (``token``, ``secret``, ``api_key``, ``password``, ``session``, ...), plus
    every header some tool declared with ``credential_headers=``.
    """
    from .auth import SERVICE_TOKEN_HEADER
    from .redact import is_secret_key

    lowered = str(name).strip().lower()
    return (
        lowered == SERVICE_TOKEN_HEADER
        or is_secret_key(lowered)
        or lowered in _DECLARED_CREDENTIAL_HEADERS
    )


def request_header(ctx: Any, name: str) -> str | None:
    """One inbound non-credential request header by name (case-insensitive), or None.

    The public reader a relaying connector uses to key its upstream session off a
    routing header such as ``X-Execution-Id``. It never returns a credential:
    for a name `is_credential_header` matches (``Authorization``, ``Cookie``,
    the service token, any secret-looking name, any header a tool declared with
    ``credential_headers=``) it returns None whether or not the header was sent.
    Credentials reach a tool only through its own ``credential_headers=``.
    """
    if is_credential_header(name):
        return None
    headers = {str(k).lower(): v for k, v in _request_headers(ctx).items()}
    return headers.get(name.lower())


def _run_on_shutdown(app: Any, hooks: list[Callable[[], Any]], logger: Any) -> Any:
    """Wrap an ASGI app so `hooks` run before it reports shutdown complete.

    Intercepts the lifespan protocol rather than registering a Starlette
    handler, because an application built with a lifespan (the SDK's is)
    ignores `on_shutdown` handlers. A hook that fails is logged and the rest
    still run: one stuck pool must not keep the others open.
    """
    if not hooks:
        return app

    async def wrapped(scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "lifespan":
            await app(scope, receive, send)
            return

        async def send_after_hooks(message: dict) -> None:
            if message.get("type") == "lifespan.shutdown.complete":
                for hook in hooks:
                    try:
                        outcome = hook()
                        if hasattr(outcome, "__await__"):
                            await outcome
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("shutdown_hook_failed", error_type=type(exc).__name__)
            await send(message)

        await app(scope, receive, send_after_hooks)

    return wrapped


class ConnectorServer:
    """A connector, with the framework guarantees applied to every tool."""

    def __init__(
        self,
        name: str,
        version: str = "0.1.0",
        description: str = "",
        *,
        token_verifier: Any = None,
        resource_server_url: str | None = None,
        issuer_url: str | None = None,
        required_scopes: list[str] | None = None,
        budget: Budget | None = None,
        env: dict[str, str] | None = None,
        max_body_bytes: int | None = None,
    ) -> None:
        self.name = name
        self.version = version
        # The connector's own default for the request body cap, for tools that
        # legitimately carry large base64 payloads. MCP_MAX_BODY_BYTES still
        # overrides it; see `transport.max_body_bytes`.
        if max_body_bytes is not None and (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes <= 0
        ):
            raise ValueError("max_body_bytes must be a positive integer number of bytes")
        from .transport import DEFAULT_MAX_BODY_BYTES

        self._default_max_body_bytes = max_body_bytes or DEFAULT_MAX_BODY_BYTES
        # Two things, deliberately not one. `_env` is a snapshot, which is
        # right for values read once at start-up (mode, bind host, budget).
        # `_injected_env` is None unless a caller supplied an environment, and
        # the read-only checks pass THAT: None makes them fall back to live
        # `os.environ`, preserving the guarantee that the flag is read per call
        # rather than captured at construction.
        #
        # Collapsing them broke that guarantee: enforcement started reading a
        # snapshot, so flipping MCP_READ_ONLY needed a restart. Keeping them
        # separate is what lets an injected env be enforced AND an ambient one
        # stay live.
        self._injected_env = dict(env) if env is not None else None
        self._env = self._injected_env if self._injected_env is not None else dict(os.environ)
        from .logging_setup import install_log_redaction

        # Before anything logs: redaction on structlog and on the stdlib
        # loggers the SDK and uvicorn use, once per process.
        install_log_redaction()
        self.logger = structlog.get_logger(f"mcp.{name}")
        self.budget = budget or Budget.from_env(self._env)
        self.mode = self._resolve_mode()
        self._annotations: dict[str, ToolAnnotations | None] = {}
        # Every tool's permission group, which is what the two selection
        # headers are resolved against. Kept beside the annotations rather
        # than inside them: a group is an authorization fact and an
        # annotation is a hint to a model, and the SDK validates the latter.
        self._permission_groups: dict[str, str] = {}
        self._required_scopes: dict[str, list[str]] = {}
        self._shutdown_hooks: list[Callable[[], Any]] = []
        # Hooks for a connector that relays another MCP server (see relay.py).
        # Installed lazily, so a connector that registers none is untouched.
        self._list_hooks: list[Callable[[], Any]] = []
        self._call_hooks: list[Callable[[str], Any]] = []
        self._dynamic_hooks_installed = False
        # `list_tools` without the before-list hooks, for /healthz: a probe must
        # not wait on an upstream the hooks would refresh (set with the hooks).
        self._list_tools_without_hooks: Callable[[], Any] | None = None
        # Names registered by `register_relayed_tool`, so neither it nor
        # `unregister_relayed_tool` can replace or remove a local tool.
        self._relayed_names: set[str] = set()

        self._token_verifier = token_verifier
        # The SDK refuses a token verifier without auth settings, so the seam
        # could not be used at all. With one, the SDK does the resource-server
        # half itself: bearer check, audience (`validate_token_resource`),
        # expiry, the RFC 9728 metadata route and the 401 challenge.
        auth = None
        if token_verifier is not None:
            from mcp.server.auth.settings import AuthSettings

            resource = resource_server_url or self._env.get("MCP_RESOURCE_URL")
            issuer = issuer_url or self._env.get("MCP_AUTH_ISSUER_URL")
            if not resource or not issuer:
                raise ValueError(
                    "a token verifier needs the server's own resource URL and the "
                    "authorization server's issuer URL (MCP_RESOURCE_URL, MCP_AUTH_ISSUER_URL)"
                )
            auth = AuthSettings(
                issuer_url=issuer,
                resource_server_url=resource,
                required_scopes=required_scopes,
                validate_token_resource=True,
            )
        self.mcp = MCPServer(
            name=name,
            version=version,
            description=description,
            token_verifier=token_verifier,
            auth=auth,
        )
        self._install_read_only_listing()
        self._install_tool_selection_listing()
        self._install_health_route()

    def _read_only(self) -> bool:
        """Read-only if EITHER the injected environment or the live one says so.

        Two requirements meet here and both are right.

        A caller that passes `env={"MCP_READ_ONLY": "1"}` must have it
        enforced; enforcement previously consulted only `os.environ`, so such a
        server logged `read_only=True` and allowed every write.

        A caller that passes an env for some OTHER reason, `{"MCP_MODE":
        "local"}`, must not thereby freeze the flag: it is read per call so
        flipping it does not need a restart.

        Satisfying one alone breaks the other, so either source enabling it
        wins. That is also the right default for a safety control: the failure
        of an OR is refusing a write that could have been allowed, and the
        failure of an AND is allowing one that should not have been.
        """
        return read_only_enabled(self._injected_env) or read_only_enabled()

    def _install_read_only_listing(self) -> None:
        """Hide non-read tools from `tools/list` when the deployment is read-only.

        The call check in `_apply_guarantees` is the control: it refuses a
        write whatever the client believes, including one that cached the tool
        list earlier or guessed a name. This is the convenience half, and it
        was missing.

        Worth being blunt about why it is here. The README stated that
        read-only mode filters `tools/list` **and** refuses on `tools/call`.
        Only the second was wired. A documented control that is not
        implemented is worse than an absent one, because someone reads the
        sentence and stops looking.

        Evaluated per call rather than at construction, so flipping
        MCP_READ_ONLY does not need a restart to take effect, and so a test
        can set it with monkeypatch.
        """
        original = self.mcp.list_tools

        async def list_tools_filtered() -> list:
            tools = await original()
            if not self._read_only():
                return tools
            return [t for t in tools if is_read_tool(self._annotations.get(t.name))]

        self.mcp.list_tools = list_tools_filtered  # type: ignore[method-assign]

    def _install_tool_selection_listing(self) -> None:
        """Narrow `tools/list` to what this connection selected.

        The refusal in `_apply_guarantees` is the control, for the same reason
        it is in read-only mode: a client that cached an earlier listing, or
        that guessed a name, must still be refused. This is the convenience
        half, and a listing that advertises a tool the next call refuses is a
        bad way to tell anyone about a selection.

        Installed as SDK middleware because the selection lives in the request
        headers and `MCPServer.list_tools()` takes no request. Middleware is
        the only seam that sees both. The SDK calls that list semi-public and
        reserves the right to change it in a 2.x minor, so
        `test_tool_selection.py` asserts the narrowing over a real socket: if
        the seam moves, a test fails here rather than a filter going quietly
        missing in production.
        """
        from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

        connector = self

        async def narrow_to_selection(
            ctx: ServerRequestContext, call_next: CallNext
        ) -> HandlerResult:
            result = await call_next(ctx)
            if ctx.method != "tools/list":
                return result
            headers = getattr(getattr(ctx, "request", None), "headers", None)
            allowed = enabled_tool_names(headers, connector._permission_groups)
            narrowed, listed, registered = narrow_listing(result, allowed)
            if listed != registered:
                connector.logger.info("tools_list_narrowed", listed=listed, registered=registered)
            return narrowed

        self.mcp.middleware.append(narrow_to_selection)

    def _install_health_route(self) -> None:
        """`GET /healthz`: is this process up and serving its tools.

        For a container HEALTHCHECK and a Kubernetes probe, which need a cheap
        answer that does not speak MCP. It is not how the platform learns the
        catalogue: the platform discovers a server by calling it over MCP
        (`server/discover`, `tools/list`), which is also the better liveness
        test, since it exercises the path a tool call takes.

        Deliberately says nothing a caller could use: no mode, no environment,
        no credentials, only the name, version and tool count a `tools/list`
        would reveal anyway. Outside the MCP endpoint's Host check, because a
        probe arrives by pod IP.
        """
        from starlette.responses import JSONResponse

        @self.mcp.custom_route("/healthz", methods=["GET"])
        async def healthz(request: Any) -> JSONResponse:
            # Never through the before-list hooks: a relaying connector's hook
            # refreshes its upstream's tool list, and a probe that waited on a
            # slow or hung upstream would fail and restart a healthy process.
            tools = await (self._list_tools_without_hooks or self.mcp.list_tools)()
            return JSONResponse(
                {"status": "ok", "name": self.name, "version": self.version, "tools": len(tools)}
            )

    # -- configuration ----------------------------------------------------

    def _resolve_mode(self) -> str:
        """Which deployment mode this server is running in.

        Defaults to `platform`, and an unrecognised value falls back to
        `platform` rather than to the weakest mode. A typo in this variable
        must not be the thing that disables authentication, which is exactly
        what happened in v1 where the default and the fallback were both the
        unauthenticated mode.
        """
        raw = str(self._env.get("MCP_MODE", "platform")).strip().lower()
        if raw not in VALID_MODES:
            self.logger.warning("unknown_mcp_mode", requested=raw, using="platform")
            return "platform"
        return raw

    def effective_bind_host(self, host: str) -> str:
        """Keep an unauthenticated server on loopback.

        `local` mode has no token verifier, so every request it serves is
        unauthenticated. Binding that to every interface is how a developer
        machine ends up serving tools to whoever can route to it.

        Clamped rather than refused: connectors are started by scripts that
        pass 0.0.0.0 because that is what a container needs, and refusing
        would break the local workflow to protect a case the override already
        covers.
        """
        if self.mode != "local":
            return host
        if host.strip().lower() in LOOPBACK:
            return host
        if str(self._env.get("MCP_ALLOW_INSECURE_BIND", "")).strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            self.logger.warning("local_mode_exposed_beyond_loopback", host=host)
            return host
        self.logger.error(
            "local_bind_clamped_to_loopback",
            requested=host,
            effective="127.0.0.1",
            msg="local mode is unauthenticated; set MCP_ALLOW_INSECURE_BIND=1 to override",
        )
        return "127.0.0.1"

    # -- tools ------------------------------------------------------------

    def tool(
        self,
        *,
        name: str,
        description: str,
        annotations: ToolAnnotations | None = None,
        title: str | None = None,
        untrusted_fields: tuple = (),
        source: str | None = None,
        credential_headers: tuple | list = (),
        permission_group: str = DEFAULT_GROUP,
        required_scopes: tuple | list = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a tool.

        `annotations` is required in practice: the tool-contract linter fails a
        tool without them, and `readOnlyHint` is what the read-only mode keys
        on. It is optional in the signature only so the linter can report a
        missing one as a lint failure with a useful message rather than a
        TypeError at import time.

        `credential_headers` names the request headers this tool is allowed to
        receive, which is a security statement rather than a convenience: a
        Zoom tool has no business seeing a Microsoft refresh token that happens
        to be on the same request. An undeclared header is not passed, so a
        tool that declares none gets only the platform context.

        `permission_group` and `required_scopes` are what a connection selects
        by. The platform groups a connector's tools for selection, sends the
        selection back as `X-MCP-Enabled-Groups` (or names the tools outright),
        and asks the user only for the scopes the selected tools need. A group
        that stays a comment narrows nothing: the port left all 263 of them as
        generated comments, so every connection was served every tool.

        A handler taking a `credentials` parameter has it **supplied by the
        framework** and removed from the client-facing signature. It is not an
        argument a caller sends, it never appears in the advertised schema, and
        before this existed the mismatch between the two made every tool in the
        catalogue fail on every call.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            wrapped = self._apply_guarantees(
                fn,
                tool_name=name,
                annotations=annotations,
                untrusted_fields=untrusted_fields,
                source=source or self.name,
                credential_headers=tuple(credential_headers),
            )
            register_tool(
                self.mcp,
                wrapped,
                name=name,
                description=description,
                annotations=annotations,
                title=title,
                meta={
                    # Namespaced, as the specification asks of `_meta`. The
                    # platform reads the selection surface from its own
                    # manifest; this is so a tool's group is visible to anyone
                    # holding only the server.
                    "io.magone/permission_group": permission_group,
                    "io.magone/required_scopes": list(required_scopes),
                },
            )
            self._annotations[name] = annotations
            self._permission_groups[name] = permission_group
            self._required_scopes[name] = list(required_scopes)
            _DECLARED_CREDENTIAL_HEADERS.update(str(h).lower() for h in credential_headers)
            return fn

        return decorator

    def _apply_guarantees(
        self,
        fn: Callable[..., Any],
        *,
        tool_name: str,
        annotations: ToolAnnotations | None,
        untrusted_fields: tuple,
        source: str,
        credential_headers: tuple = (),
    ) -> Callable[..., Any]:
        """Wrap a handler so the framework's promises hold for every tool."""
        import functools
        import inspect

        from mcp.server.mcpserver import Context
        from mcp.server.mcpserver.exceptions import ToolError

        from .credentials import build as build_credentials
        from .errors import UpstreamError, refusal, transport_refusal
        from .provenance import mark_result
        from .redact import argument_keys
        from .results import PASS_THROUGH_EXCEPTIONS, client_message, to_call_result
        from .schema import declared_output_schema, declared_schema

        budget = self.budget
        logger = self.logger
        credentials_path = self._env.get("MCP_CREDENTIALS_PATH")

        # `eval_str=True` matters and is not a refinement. `guarded` is defined
        # in this module, so its `__globals__` is this module's namespace. Hand
        # pydantic a signature whose annotations are still strings and it
        # resolves them here, where the connector's `Optional`, `Dict` and its
        # own types do not exist:
        #
        #   PydanticUserError: `create_eventArguments` is not fully defined;
        #   you should define `Optional`
        #
        # Resolving against `fn`'s own globals first turns them into objects,
        # which carry their meaning with them. A connector that cannot resolve
        # its own annotations falls back to the raw signature rather than
        # failing to register: it is already counted by the port-status budget.
        try:
            signature = inspect.signature(fn, eval_str=True)
        except (NameError, TypeError, AttributeError):
            signature = inspect.signature(fn)
        takes_credentials = "credentials" in signature.parameters
        passes_ctx = "ctx" in signature.parameters
        declares_output = declared_output_schema(fn) is not None

        @functools.wraps(fn)
        async def guarded(*args: Any, ctx: Context | None = None, **kwargs: Any) -> Any:
            # The call check, not the listing filter, is the control.
            if self._read_only():
                assert_callable(tool_name, annotations, {"MCP_READ_ONLY": "1"})
            # Same reasoning, one layer out: a tool outside this connection's
            # selection is not callable, whatever a stale `tools/list` said.
            assert_enabled(tool_name, _request_headers(ctx), self._permission_groups)
            # Arguments by key only: values are user content, and sometimes a
            # credential passed as an ordinary parameter.
            logger.info("tool_call", tool=tool_name, argument_keys=list(argument_keys(kwargs)))
            if takes_credentials:
                # Supplied here, never by the caller. The handler signature
                # asks for it because that is how the whole catalogue is
                # written; the wire contract does not mention it because a
                # client does not send it.
                kwargs["credentials"] = build_credentials(
                    _request_headers(ctx),
                    credential_headers,
                    file_path=credentials_path,
                )
            if passes_ctx:
                # The handler asked for the request context by the SDK's own
                # name for it. `guarded` has to name the parameter `ctx` to
                # receive it, which also swallowed it: a tool written from the
                # SDK documentation (`ctx: Context`) was called without it.
                kwargs["ctx"] = ctx
            try:
                result = await fn(*args, **kwargs)
            except UpstreamError as exc:
                # A vendor's error response: a refusal that carries its code,
                # so a host can tell "wait and retry" from "stop" (errors.py).
                logger.warning(
                    "tool_execution_failed", tool=tool_name, error_type=type(exc).__name__
                )
                result = refusal(client_message(exc), exc.code, exc.retry_after)
            except PASS_THROUGH_EXCEPTIONS as exc:
                # The SDK reports any exception that is not a ToolError as a
                # bare "Error executing tool X", so the message a handler wrote
                # for the model ("Jira answered 403: the connection lacks the
                # write:jira-work scope") never arrived. v1 passed these three
                # through for exactly that reason (its `_SAFE_EXCEPTIONS`);
                # anything else stays masked.
                logger.warning(
                    "tool_execution_failed", tool=tool_name, error_type=type(exc).__name__
                )
                raise ToolError(client_message(exc)) from exc
            except Exception as exc:
                # A request that never got an answer is worth retrying, and
                # saying so beats the SDK's bare "Error executing tool".
                # Anything else stays masked, as before.
                unreached = transport_refusal(exc)
                if unreached is None:
                    raise
                logger.warning(
                    "tool_execution_failed", tool=tool_name, error_type=type(exc).__name__
                )
                result = unreached
            if untrusted_fields:
                result = mark_result(result, source=source, fields=untrusted_fields)
            # Shape first, then budget what will actually be sent. A refusal
            # is a failure and an extra content block has to survive (v1
            # behaviours, see `results.py`); the budget then cuts the text a
            # client receives, keeping `isError` and any attachment.
            shaped = to_call_result(result, structured=declares_output)
            return budget_call_result(shaped, budget)

        # Two edits to the signature the SDK validates against.
        #
        # `credentials` is hidden. `Tool.from_function` builds its argument
        # model from the signature, and swapping `tool.parameters` afterwards
        # changes only what is advertised. Leaving the parameter visible
        # therefore makes the advertised schema and the validated model
        # disagree, and every call fails with "credentials  Field required" -
        # which is exactly what the whole catalogue did before this.
        #
        # `ctx` is added, annotated, because that is how the SDK knows to
        # inject the request context the headers come from. Unconditionally,
        # not just for a handler that asked for credentials: the selection
        # check reads those headers too, and a control that depends on a
        # handler's own signature is not a control. A tool taking no
        # credentials got no ctx, so no headers, so every selection read as
        # "unrestricted" and the refusal never fired.
        visible = [p for n, p in signature.parameters.items() if n != "credentials"]
        if "ctx" not in signature.parameters:
            ctx_param = inspect.Parameter(
                "ctx",
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Context,
            )
            # Before any `**kwargs`, not after it. A keyword-only parameter
            # following a variadic keyword is not a valid signature, and
            # appending blindly stopped a connector importing at all.
            var_keyword = next(
                (i for i, p in enumerate(visible) if p.kind is inspect.Parameter.VAR_KEYWORD),
                len(visible),
            )
            visible.insert(var_keyword, ctx_param)
        guarded.__signature__ = signature.replace(parameters=visible)  # type: ignore[attr-defined]
        # Built from the resolved signature for the same reason, so no
        # string survives to be evaluated in the wrong namespace.
        annotations_map = {
            p.name: p.annotation for p in visible if p.annotation is not inspect.Parameter.empty
        }
        annotations_map["ctx"] = Context
        if signature.return_annotation is not inspect.Signature.empty:
            annotations_map["return"] = signature.return_annotation
        guarded.__annotations__ = annotations_map

        # The schema declaration lives on the original function; carry it
        # across or register_tool would fall back to type-hint derivation and
        # silently change the wire contract.
        schema = declared_schema(fn)
        if schema is not None:
            from .schema import _SCHEMA_ATTR

            setattr(guarded, _SCHEMA_ATTR, schema)
        output_schema = declared_output_schema(fn)
        if output_schema is not None:
            from .schema import _OUTPUT_ATTR

            setattr(guarded, _OUTPUT_ATTR, output_schema)
        return guarded

    # -- relayed tools -----------------------------------------------------

    def register_relayed_tool(
        self,
        name: str,
        *,
        description: str,
        input_schema: dict,
        annotations: ToolAnnotations | None,
        permission_group: str,
        handler: Callable[[dict, Any], Any],
        required_scopes: tuple | list = (),
        title: str | None = None,
        source: str | None = None,
    ) -> Any:
        """Register a tool whose body is supplied at runtime (a relay to an upstream
        MCP server) with the guarantees a local tool gets.

        ``handler(arguments, ctx)`` runs the relay and returns the upstream's
        ``CallToolResult`` (or a dict shaped like one); the server applies
        read-only mode, per-connection selection and the outbound budget around
        it (``_run_relayed``). Register and unregister as the upstream's tool
        list changes; ``before_list_tools``/``before_call_tool`` are the seams
        for refreshing that list and for intercepting a call to a name not yet
        registered. The connector never has to reach into the SDK tool table or
        this class's bookkeeping, which it would otherwise do unreviewed.

        Every text item of a relayed result is third-party content and is
        wrapped in the ``<<<UNTRUSTED_CONTENT source=...>>>`` envelope
        (``provenance.wrap``), labelled ``source`` (default: this server's
        name); image and resource items are left as they are.

        A name that is already a local tool is refused with ``ValueError``: a
        relay must never replace a tool the connector defines itself, with its
        own annotations, group and scopes.
        """
        from .relay import RelayedTool, _placeholder

        if name in self.mcp._tool_manager._tools and name not in self._relayed_names:
            raise ValueError(f"'{name}' is a local tool of {self.name}; a relay cannot replace it.")

        tool = RelayedTool.from_function(
            _placeholder,
            name=name,
            description=description,
            title=title,
            annotations=annotations,
            structured_output=False,
        )
        tool.parameters = input_schema if isinstance(input_schema, dict) else {"type": "object"}
        tool.meta = {
            "io.magone/permission_group": permission_group,
            "io.magone/required_scopes": list(required_scopes),
        }
        tool.server = self
        tool.handler = handler
        tool.source = source or self.name
        self.mcp._tool_manager._tools[name] = tool
        self._relayed_names.add(name)
        self._annotations[name] = annotations
        self._permission_groups[name] = permission_group
        self._required_scopes[name] = list(required_scopes)
        return tool

    def unregister_relayed_tool(self, name: str) -> None:
        """Remove a tool registered by ``register_relayed_tool``; a local tool is never removed."""
        if name not in self._relayed_names:
            return
        self._relayed_names.discard(name)
        self.mcp._tool_manager._tools.pop(name, None)
        self._annotations.pop(name, None)
        self._permission_groups.pop(name, None)
        self._required_scopes.pop(name, None)

    def tool_names(self) -> frozenset[str]:
        """Every tool name currently registered (local and relayed)."""
        return frozenset(self.mcp._tool_manager._tools)

    async def _run_relayed(self, tool: Any, arguments: dict, context: Any) -> Any:
        """Apply the framework's guarantees around a relayed tool's handler."""
        from mcp.types import CallToolResult

        from .redact import argument_keys
        from .relay import mark_relayed_text

        headers = _request_headers(context)
        # The same checks `_apply_guarantees` makes for a local tool, so a
        # relayed call is held to read-only mode and the connection's selection.
        if self._read_only():
            assert_callable(tool.name, tool.annotations, {"MCP_READ_ONLY": "1"})
        assert_enabled(tool.name, headers, self._permission_groups)
        self.logger.info(
            "relayed_tool_call", tool=tool.name, argument_keys=list(argument_keys(arguments))
        )
        result = await tool.handler(arguments, context)
        if isinstance(result, dict):
            result = CallToolResult.model_validate(result)
        return budget_call_result(mark_relayed_text(result, tool.source), self.budget)

    def before_list_tools(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Run ``fn`` before each ``tools/list`` (e.g. to refresh an upstream manifest)."""
        self._ensure_dynamic_hooks()
        self._list_hooks.append(fn)
        return fn

    def before_call_tool(self, fn: Callable[[str], Any]) -> Callable[[str], Any]:
        """Run ``fn(name)`` before each ``tools/call`` (e.g. to load or refuse a relayed name)."""
        self._ensure_dynamic_hooks()
        self._call_hooks.append(fn)
        return fn

    def _ensure_dynamic_hooks(self) -> None:
        """Wrap ``list_tools``/``call_tool`` to run the registered hooks, once.

        Installed only when a connector registers its first hook, so a connector
        that relays nothing keeps the SDK's own dispatch untouched.
        """
        if self._dynamic_hooks_installed:
            return
        self._dynamic_hooks_installed = True
        list_tools = self.mcp.list_tools
        call_tool = self.mcp.call_tool
        self._list_tools_without_hooks = list_tools

        async def list_with_hooks() -> Any:
            for hook in self._list_hooks:
                await hook()
            return await list_tools()

        async def call_with_hooks(name: str, arguments: dict, context: Any = None) -> Any:
            for hook in self._call_hooks:
                await hook(name)
            return await call_tool(name, arguments, context)

        self.mcp.list_tools = list_with_hooks  # type: ignore[method-assign]
        self.mcp.call_tool = call_with_hooks  # type: ignore[method-assign]

    def annotations_for(self, name: str) -> ToolAnnotations | None:
        return self._annotations.get(name)

    def max_body_bytes(self) -> int:
        """The request body cap this server enforces.

        `MCP_MAX_BODY_BYTES` if set, else the connector's declared default,
        else the framework's 10 MiB.
        """
        from .transport import max_body_bytes

        return max_body_bytes(self._env, self._default_max_body_bytes)

    # -- running ----------------------------------------------------------

    def on_shutdown(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Run `fn` when the served application shuts down.

        For resources a connector holds across calls, such as database pools.
        v1 closed those in `@app.on_event("shutdown")`; the port had no
        equivalent, so a restart abandoned every open pool. Runs from the ASGI
        lifespan rather than the SDK's server lifespan, which is tied to a
        session and would fire far more often than once.
        """
        self._shutdown_hooks.append(fn)
        return fn

    def custom_route(self, path: str, methods: list[str]) -> Callable:
        """Mount a non-MCP HTTP route.

        Needed by any deployment that must keep serving its own non-MCP
        endpoints (an upload route, say) beside the MCP app. Exposed here so
        that requirement is visible rather than discovered later.
        """
        return self.mcp.custom_route(path, methods)

    def app(self) -> Any:
        """The ASGI application.

        Three settings are passed explicitly rather than left to the SDK:

        - `transport_security`: the SDK's default admits loopback Host
          headers only, so a connector reached by its service name answered
          every call with 421. See `transport.py` for why `host="0.0.0.0"` is
          not the fix.
        - `stateless_http=True`: a pre-2026 client that negotiates a session
          otherwise holds it in this process's memory, so a second replica
          answers "Missing session ID". Requests on the `2026-07-28` envelope
          are stateless whatever this says; this makes the older ones so too.
        - `max_request_body_size`: the SDK's 4 MiB default answered 413 to a
          base64 attachment that v1's 10 MB limit accepted. A connector can
          declare a larger default. See `transport.py`.
        """
        from .transport import transport_security

        app = self.mcp.streamable_http_app(
            stateless_http=True,
            transport_security=transport_security(self._env),
            max_request_body_size=self.max_body_bytes(),
        )
        service_token = str(self._env.get("MCP_SERVICE_TOKEN", "")).strip()
        if service_token and self.mode == "platform":
            from .auth import require_service_token

            app = require_service_token(app, service_token)
        return _run_on_shutdown(app, self._shutdown_hooks, self.logger)

    def authentication_problem(self) -> str | None:
        """Why this server would serve unauthenticated, or None if it would not.

        `local` is unauthenticated by design and kept on loopback by
        `effective_bind_host`. `platform` needs the service token (v1's check)
        or a token verifier; `hosted` needs a token verifier. Either may be
        waived only explicitly, with MCP_ALLOW_UNAUTHENTICATED=1.
        """
        if self.mode == "local":
            return None
        if str(self._env.get("MCP_ALLOW_UNAUTHENTICATED", "")).strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return None
        if self._token_verifier is not None:
            return None
        if self.mode == "platform" and str(self._env.get("MCP_SERVICE_TOKEN", "")).strip():
            return None
        if self.mode == "platform":
            return "platform mode needs MCP_SERVICE_TOKEN (or a token verifier)"
        return "hosted mode needs a token verifier"

    def run(self, host: str = "0.0.0.0", port: int = 8000) -> None:  # noqa: S104
        import uvicorn

        from .transport import configured_hosts, host_check_disabled

        problem = self.authentication_problem()
        if problem:
            # Refused at start-up, not discovered as an open endpoint later. v1
            # answered 503 per call for the same misconfiguration; a process
            # that will not start is louder and cannot be half-deployed.
            self.logger.error("refusing_to_start_unauthenticated", reason=problem)
            raise SystemExit(
                f"{self.name}: {problem}. Set MCP_ALLOW_UNAUTHENTICATED=1 only if "
                "something in front of this server authenticates every caller."
            )
        host = self.effective_bind_host(host)
        if host_check_disabled(self._env):
            self.logger.warning(
                "host_check_disabled",
                msg="MCP_ALLOWED_HOSTS=* accepts any Host and Origin header",
            )
        elif host.strip().lower() not in LOOPBACK and not configured_hosts(self._env):
            # Loud at start-up rather than as a 421 on the first call, which is
            # where this was found: every request by service name refused.
            self.logger.warning(
                "only_loopback_hosts_accepted",
                bind=host,
                msg="set MCP_ALLOWED_HOSTS to the names callers use, e.g. the service name",
            )
        self.logger.info(
            "starting",
            server=self.name,
            version=self.version,
            mode=self.mode,
            host=host,
            port=port,
            read_only=self._read_only(),
            max_result_chars=self.budget.max_result_chars,
            max_body_bytes=self.max_body_bytes(),
        )
        uvicorn.run(self.app(), host=host, port=port)
