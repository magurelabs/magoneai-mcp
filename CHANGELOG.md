# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
How versions are chosen and published is described in [RELEASING.md](RELEASING.md).

Nothing has been released yet. Everything below describes the state of `main`
ahead of the first release.

## [Unreleased]

### Added

- `magone-mcp-core`, a thin layer over the official MCP Python SDK (2.x) for
  building connectors:
  - Streamable HTTP transport at `/mcp`, stateless so a connector scales
    horizontally, with `GET /healthz` for probes.
  - Three deployment modes (`local`, `platform`, `hosted`). A server that
    would serve unauthenticated refuses to start, and `local` mode is held to
    loopback.
  - Optional OAuth resource-server mode through a token verifier, with
    audience and expiry validation and an RFC 9728 challenge; the inbound
    token is never forwarded upstream.
  - Per-call credentials: each tool declares the request headers it may
    receive, and receives only those, as a `credentials` dict.
  - Host and Origin allowlist for DNS-rebinding protection
    (`MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS`) and a request body cap
    (`MCP_MAX_BODY_BYTES`).
  - An outbound budget per tool result (`MCP_MAX_RESULT_CHARS`) that says so
    in the result when it truncates.
  - A hard read-only mode (`MCP_READ_ONLY`) that filters `tools/list` and
    refuses non-read tools on `tools/call`.
  - Per-connection tool selection by name or permission group
    (`X-MCP-Enabled-Tools`, `X-MCP-Enabled-Groups`).
  - Secret redaction on every log path, argument logging by key only, and
    scrubbing of upstream error bodies.
  - A single SSRF check (`validate_url`) that decodes IPv4 addresses embedded
    in IPv6 literals.
  - Declared input and output schemas kept byte for byte, and text arguments
    that stay text.
  - A provenance marker for third-party text, and a content-handle interface
    (not yet used by any connector).
- Connectors for third-party APIs under `connectors/`, each with its own
  tests and an optional dependency extra. [docs/TOOLS.md](docs/TOOLS.md) lists
  every tool, and `connectors/publication.json` records which connectors are
  ready to publish and why the others are not yet.
- `linkedin` connector: 11 tools to post as a member or a company page, read a
  post's reactions and comments, and report on who engages with an
  organization's posts. Blocked from publication until it has run end to end.
- `server.json` registry manifests for every publishable connector, derived
  from the connector's own name and version.
- `outlook_email`: `set_email_categories` adds and removes Outlook categories
  on a message, in the signed-in user's mailbox or an allowed shared mailbox,
  and `get_email` returns a message's `categories`.
- One Dockerfile that builds any connector as a non-root image, labelled with
  its MCP server name.
- CI gates, each runnable locally: golden tool schemas, boundary, tool
  contract, dependencies, compile, code health, credential headers,
  permission groups, publication, server manifests, tool reference, runner
  labels, port status, lint and format, the framework and connector test
  suites, and the official MCP conformance suite.
- A dry-run publishing workflow for PyPI, container images and the MCP
  Registry, whose publish jobs run only on manual dispatch inside a protected
  environment and have every push step disabled.
- Project documentation and community files: README, contributor guide,
  architecture overview, connector-writing guide, configuration reference,
  governance, maintainers, support, security policy, release process, Code of
  Conduct (Contributor Covenant 2.1), issue and pull request templates, and a
  DCO sign-off check.
- OpenSSF Scorecard and CodeQL analysis workflows, and Dependabot updates for
  GitHub Actions, Python dependencies and the container base image.
- A connector can declare its own request body cap with
  `ConnectorServer(max_body_bytes=...)`; `MCP_MAX_BODY_BYTES` still overrides
  it. `atlassian_jira` declares 70 MiB and `outlook_email` 75 MiB, so a call
  carrying several base64 attachments is no longer refused with `413`.

### Changed

- The default outbound budget (`MCP_MAX_RESULT_CHARS`) is 1,000,000
  characters instead of 100,000. Real results reach about 220,000 characters,
  and a tool with a declared output schema returns an oversized result as an
  error.
- `outlook_email`: `reply_email` takes an optional `mailbox`, so a reply to a
  message in an allowed shared mailbox is sent from that mailbox, with or
  without attachments. Existing properties are unchanged.

### Fixed

- `atlassian_jira`: `addJiraIssueAttachment`'s description points callers at
  `attachments` again instead of inviting model-written base64.
- `outlook_email`: an empty `mailbox` on `get_email`, `list_attachments`,
  `read_attachment` and `download_attachment` means the signed-in user's own
  mailbox, as an omitted one does, instead of being refused as invalid.
- `otp_verification`: codes come from the operating system's CSPRNG instead of
  SQL `RANDOM()`, are stored only as a salted hash, and are compared in
  constant time. Asking again while a code is live now issues a replacement
  (the old one stops working) instead of returning the stored one, and the raw
  identifier is no longer written to the log on lockout. Rows written before
  this change hold plaintext codes, never match, and expire on their own.

[Unreleased]: https://github.com/magurelabs/magoneai-mcp/commits/main
