# Architecture of `magone-mcp-core`

`magone-mcp-core` (`src/magone_mcp_core/`) is a thin layer over the
[official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
The SDK owns the protocol: JSON-RPC, revision negotiation, the Streamable HTTP
transport and every conformance obligation. This layer adds the handful of
things a catalogue of connectors needs and the SDK has no opinion about, and
otherwise gets out of the way.

It deliberately does not wrap or hide the SDK. `ConnectorServer.mcp` is the
real SDK `MCPServer`, and anything the SDK documentation says applies.

This page explains how a request moves through a connector and where each
guarantee is enforced. For every setting mentioned here, see
[configuration.md](configuration.md). For building a connector, see
[writing-a-connector.md](writing-a-connector.md).

## The shape of a connector

```text
connectors/<name>/server.py
    server = ConnectorServer(name=..., version=...)

    @server.tool(name=..., annotations=..., credential_headers=..., permission_group=...)
    async def some_tool(credentials: dict, ...) -> dict: ...

connectors/<name>/__main__.py
    server.run(host=MCP_HOST or 0.0.0.0, port=PORT or 8000)
```

One connector is one process serving one MCP endpoint, `/mcp`, plus
`GET /healthz`. There is no shared state between connectors and no shared
process.

## Request flow

```text
HTTP request
  |
  |  uvicorn
  v
service-token check          (platform mode with MCP_SERVICE_TOKEN set; /mcp only)
  |
  v
SDK Streamable HTTP app      Host / Origin allowlist (DNS-rebinding protection)
  |                          request body cap (MCP_MAX_BODY_BYTES)
  |                          bearer-token verification (when a token verifier is configured)
  v
SDK MCPServer                JSON-RPC, initialize, tools/list, tools/call
  |
  |  tools/list  -> read-only filter -> per-connection selection filter -> client
  |
  |  tools/call
  v
guarded handler              (one per tool, built by ConnectorServer.tool)
  1. read-only check         refuse a non-read tool when MCP_READ_ONLY is set
  2. selection check         refuse a tool this connection did not select
  3. log the call            tool name and argument KEYS only
  4. build `credentials`     from MCP_CREDENTIALS_PATH, then the tool's declared headers
  5. call the handler
  6. provenance marking      for fields declared untrusted
  7. shape the result        refusal -> isError, extra content block, structured output
  8. outbound budget         cut the text to MCP_MAX_RESULT_CHARS, say so in the result
  v
CallToolResult -> client
```

Every step in the guarded handler is applied by the framework to every tool.
A connector author cannot forget one, and cannot opt out of one by accident.

## Transport

`server.py`, `transport.py`

The server speaks MCP over **Streamable HTTP** at `/mcp`. There is no stdio
transport yet. Three settings are passed to the SDK explicitly rather than
left to its defaults:

- **Stateless HTTP.** A client that negotiates a session would otherwise have
  it held in one process's memory, and a second replica would answer
  "Missing session ID". Stateless mode lets a connector scale horizontally.
- **Host and Origin allowlist.** DNS-rebinding protection is always on.
  Loopback names are always accepted; every other name a caller uses (a
  service name, an ingress host) must be listed in `MCP_ALLOWED_HOSTS`, or the
  request is answered `421`. Browser origins are listed in
  `MCP_ALLOWED_ORIGINS`. `MCP_ALLOWED_HOSTS=*` turns the check off, and the
  server logs a warning when it starts that way.
- **Request body cap.** The SDK buffers the whole body before parsing it, so
  the body is bounded: 10 MiB by default, answered `413` when exceeded. Base64
  attachments are about a third larger than the file they carry, so a
  connector whose tools carry several of them declares a larger default with
  `ConnectorServer(max_body_bytes=...)`. `MCP_MAX_BODY_BYTES` overrides
  either.

`GET /healthz` answers `{status, name, version, tools}` for a container or
Kubernetes probe. It sits outside the Host check, because a probe arrives by
pod IP, and outside the service-token check. It reveals nothing a
`tools/list` would not.

`ConnectorServer.custom_route` mounts other non-MCP routes, and
`ConnectorServer.on_shutdown` registers hooks (for example closing a
connection pool) that run once when the process shuts down.

## Authentication modes

`server.py`, `auth.py`

`MCP_MODE` selects one of three modes. The default is `platform`, and an
unrecognised value falls back to `platform`, so a typo can never be what
disables authentication.

| Mode | Who the caller is | What authenticates them |
|---|---|---|
| `local` | the developer who started the process | nothing, so the server is held to loopback |
| `platform` | a platform that runs the connector on a user's behalf | `X-Service-Token` matching `MCP_SERVICE_TOKEN`, or a token verifier |
| `hosted` | any OAuth 2.1 client | a token verifier (bearer token, audience and expiry checked) |

The rules that hold in every mode:

- **A server that would serve unauthenticated refuses to start.** `platform`
  without a service token or verifier, and `hosted` without a verifier, exit
  at start-up with a message naming the missing setting.
  `MCP_ALLOW_UNAUTHENTICATED=1` waives this, for a deployment where something
  in front of the server authenticates every request.
- **`local` mode stays on loopback.** If asked to bind any other interface it
  binds `127.0.0.1` instead and logs an error, unless
  `MCP_ALLOW_INSECURE_BIND=1` is set (a container needs this, on a network you
  control).
- **The service token is compared in constant time**, protects `/mcp` only,
  and is answered `403` when wrong or missing. It is an interim control: a
  shared secret proves the caller holds a string, not which tenant it is.
- **A token verifier makes the server an OAuth resource server.** Pass
  `token_verifier=` to `ConnectorServer` with `MCP_RESOURCE_URL` (this
  server's canonical URL, which tokens must name as their audience) and
  `MCP_AUTH_ISSUER_URL`. The SDK then checks the bearer token, its audience
  (RFC 8707) and expiry, serves the RFC 9728 protected-resource metadata, and
  answers `401` with a challenge. `PlatformTokenVerifier` is one
  implementation, backed by a token-introspection callable you supply.
- **The inbound token is never forwarded upstream.** The credential presented
  to a vendor API is a separate credential. `assert_no_passthrough` turns
  that rule into something a test can assert.

### Credentials for the upstream API

Caller authentication and vendor credentials are different things. A tool
that needs a vendor credential declares the request headers it may receive:

```python
@server.tool(..., credential_headers=["X-Linear-Api-Key"])
async def get_issue(credentials: dict, issue: str) -> dict:
    key = credentials["linear_api_key"]
```

The framework builds `credentials` per call (`credentials.py`):

1. the JSON object in the file at `MCP_CREDENTIALS_PATH`, if set (for a server
   you run yourself);
2. then each **declared** header present on the request, keyed by the header
   name without the leading `X-`, lower-cased, with `-` replaced by `_`.

A header overrides the file. An undeclared header is never passed, so a tool
that needs one vendor's key never sees another vendor's token that happens to
be on the same request. The `credentials` parameter is supplied by the
framework and removed from the advertised schema; a client never sends it as
an argument.

## Outbound budget

`budget.py`

Nothing in MCP bounds what one tool returns, so a single search over a large
page could spend a model's whole context. The budget bounds the **text** a
tool result carries to `MCP_MAX_RESULT_CHARS` (default 100,000 characters,
about 25k tokens; values below 1,000 are raised to 1,000).

It is measured on the exact text the client receives, after the result is
shaped. When a result is cut:

- the text is replaced by an envelope with `_truncated: true`, the original
  length, a notice telling the model to narrow the request or paginate, and
  the longest prefix of the content that fits, so the envelope itself stays
  within the budget;
- `isError` and every non-text content block (an image, an attachment) are
  kept;
- `structuredContent` is dropped, since it would carry the full payload past
  the cut. A tool with a declared output schema cannot be cut and still
  conform, so that cut is reported with `isError` set.

A silently shortened list is worse than an error, because the model treats it
as complete. That is why truncation is always visible.

## Result shaping

`results.py`

- A handler that returns a dict whose `error` is a **non-empty string** has
  refused the call, and the result carries `isError: true` with the payload
  intact, so the model reads what to do instead.
- `ValueError`, `TypeError` and `KeyError` raised by a handler are reported to
  the caller with their message, scrubbed of anything credential-shaped and
  capped at 500 characters. Any other exception is masked as a generic error,
  so an internal detail never reaches the caller by accident.
- A dict may carry one extra content block under `_mcp_content` (an image or
  resource alongside the JSON); it is moved into the result's content.
- A tool with `@declare_output_schema` returns structured content as well as
  text.

## Redaction and logging

`redact.py`, `logging_setup.py`

Redaction is installed once per process, before anything logs, on both paths
a connector process logs through:

- **structlog** (connectors and the framework): a processor redacts any key
  whose name looks secret (`token`, `password`, `api_key`, `authorization`,
  `cookie` and similar) and scrubs credential-shaped values (bearer and basic
  authorization values, JWTs, well-known key prefixes, passwords in URLs)
  before rendering.
- **stdlib logging** (the SDK, uvicorn, httpx): a record factory scrubs every
  record's message and traceback at creation, which is where tokens actually
  leaked.

On top of that, tool arguments are logged by key only, never by value, and
`upstream_error()` scrubs and truncates a vendor's error body before it goes
anywhere, because a vendor's 401 often echoes the token that failed.
`MCP_LOG_RAW_UPSTREAM_ERRORS=1` logs unscrubbed bodies for a debugging
session, and is honoured only in `local` mode.

## Read-only mode

`readonly.py`

`MCP_READ_ONLY=1` makes a deployment incapable of writing:

- `tools/list` omits every tool whose `readOnlyHint` is not `true`;
- `tools/call` refuses such a tool with a clear message, **whatever the client
  believes**. The call check is the control; the listing filter is a
  convenience. A client that cached the list earlier, or guessed a name, is
  still refused.

A tool with no annotations is treated as a write and fails closed. The flag
is read on every call, so changing it does not need a restart.

This is the one control here with real blast-radius value against prompt
injection: a deployment that only needs reads cannot be talked into a write.

## Per-connection tool selection

`selection.py`

A connection rarely wants a connector's whole surface. Two request headers
narrow what one connection is served and may call:

- `X-MCP-Enabled-Tools`: tool names, comma-separated;
- `X-MCP-Enabled-Groups`: permission groups, comma-separated.

Names win over groups when both are sent. A missing or blank header means no
selection was made and nothing is filtered. As with read-only mode, the call
check is the control and the listing filter is the convenience.

Each tool's group comes from `permission_group=` on its decorator (default
`default`), with optional `required_scopes=`. Both are published on the
listed tool as `_meta` (`io.magone/permission_group`,
`io.magone/required_scopes`), so a client can build a selection UI.

## Annotations

Every tool declares MCP tool annotations (see "Tools" in the
[MCP specification](https://modelcontextprotocol.io/specification)):

- `readOnlyHint` decides whether read-only mode offers the tool at all;
- `destructiveHint` warns a client that the tool overwrites or removes rather
  than only adding. The specification defaults it to `true` for a tool that is
  not read-only, so `false` is a claim.

`scripts/tool_contract_lint.py` fails a tool without both, or with both set to
`true`. Annotations are hints to a client; the framework's own decisions use
only `readOnlyHint`.

## Schemas

`schema.py`, `text_args.py`

A new tool's input schema is derived from its type hints by the SDK; describe
each parameter with `Annotated[..., Field(description=...)]`.

A tool whose schema agents already rely on uses `@declare_schema({...})`
instead, which publishes the schema byte for byte. Derivation would invent
titles and drop descriptions, a silent behaviour change for an agent reading
the description to decide what to pass. `tests/golden/tool-schemas.json`
records every tool's schema and annotations, and
`scripts/golden_schema_gate.py` fails on any diff.

An argument whose published schema admits a string reaches the handler as the
string that was sent. The SDK would otherwise `json.loads` it whenever the
Python annotation is not exactly `str`, turning `"null"` into `None`.

## SSRF validation

`net.py`

`validate_url()` is the single check every tool uses before fetching a URL a
caller supplied. It allows only `http` and `https`, resolves the host, and
refuses private, loopback, link-local (including cloud instance metadata),
carrier-grade NAT, multicast, reserved and documentation ranges. IPv6
literals that embed an IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64) are
decoded and the embedded address judged too. The linter fails a tool that
takes a `url`-like parameter and never calls it.

## Provenance

`provenance.py`

A tool can declare `untrusted_fields=` and the framework wraps those fields
of its result in a labelled envelope marking third-party text. This is a
convention, not a guarantee: it gives a client something to key on if it
wants to treat that text as data, and it does not stop a model reading
instructions inside it. See "On prompt injection" in the
[README](../README.md#on-prompt-injection).

## Content handles

`content.py`

An interface for moving bytes without giving a connector storage credentials:
a service mints a time-bounded presigned URL and the caller uploads or
downloads directly. **No connector uses it today**; it is documented so that
nobody mistakes it for a control already in place.

## Module map

| Module | Responsibility |
|---|---|
| `server.py` | `ConnectorServer`: tool registration, the guarded handler, modes, bind, health route, shutdown hooks |
| `transport.py` | Host and Origin allowlist, request body cap |
| `auth.py` | `AuthContext`, token verifiers, audience validation, service-token middleware, no-passthrough |
| `credentials.py` | building the per-call `credentials` dict from declared headers and the optional file |
| `budget.py` | the outbound result budget |
| `results.py` | refusal handling, pass-through exceptions, extra content blocks |
| `readonly.py` | read-only classification and enforcement |
| `selection.py` | per-connection tool selection |
| `redact.py`, `logging_setup.py` | secret redaction on every log path |
| `net.py` | SSRF validation |
| `schema.py`, `text_args.py` | declared schemas and text-argument preservation |
| `provenance.py` | marking untrusted text |
| `content.py` | content-handle interface (not yet adopted) |
