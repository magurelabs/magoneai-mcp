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
- `onedrive` connector: 11 tools over the signed-in user's own OneDrive
  (`/me/drive`) through Microsoft Graph's delegated flow: list, get, upload
  (one file or up to 20 inline in one call, with resumable sessions above
  4 MB), download, delete, create folder, copy, move, share link and search.
  v1's "shared with me" listing is not ported: Microsoft has deprecated both
  `/me/drive/sharedWithMe` and `/me/drive/recent` with no successor. Credentials
  arrive per call as `X-Microsoft-*` headers; its own Graph scopes are
  `Files.ReadWrite`, `User.Read` and `offline_access`, and no other connector's
  scopes change. Blocked from publication until it has run end to end.
- `server.json` registry manifests for every publishable connector, derived
  from the connector's own name and version.
- Each `server.json` names the connector's Docker Hub image,
  `docker.io/magureai/mcp-<name>:<version>`, as an `oci` package, so a
  registry entry can be installed. The publish workflow builds and pushes
  exactly that tag.
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

- `cloudflare_r2`, `outlook_email` and `surveymonkey` send the User-Agent
  `Mozilla/5.0 (compatible; magoneai-<server name>/<version>; +<repository URL>)`
  on their outbound
  requests, in place of three different hand-written strings, so the version
  in it follows the connector's.
- The default outbound budget (`MCP_MAX_RESULT_CHARS`) is 1,000,000
  characters instead of 100,000. Real results reach about 220,000 characters,
  and a tool with a declared output schema returns an oversized result as an
  error.
- `web_search`: pages are fetched with the User-Agent
  `Mozilla/5.0 (compatible; magoneai-web-search/<version>; +<repository URL>)`
  instead of a desktop
  Chrome string, on both the direct fetch and Crawl4AI. `scrape_url` with
  `return_full=true` checks the site's robots.txt first, per RFC 9309, and
  refuses a page it disallows; a single-page fetch does not.
- `outlook_email`: `reply_email` takes an optional `mailbox`, so a reply to a
  message in an allowed shared mailbox is sent from that mailbox, with or
  without attachments. Existing properties are unchanged.
- `twitter_intel` is blocked from publication and carries no `server.json`. It
  calls twitterapi.io, an unofficial third-party provider not affiliated with
  X Corp, rather than the official X API. It still runs from a checkout, and
  whoever runs it is responsible for the terms of X and of twitterapi.io.

### Fixed

- `magone-mcp-core`: when `MCP_CREDENTIALS_PATH` supplies values, the file now
  wins over request headers. A header can no longer override a key the file
  sets, and once the file holds a secret it can no longer supply a host, port,
  URL, endpoint, connection string or allowlist, so a caller who sent only a
  host header could no longer make the server present the file's password
  there. Header-only deployments are unchanged. This reverses the previous
  rule that a header overrides the file.
- `atlassian_jira`: `addJiraIssueAttachment`'s description points callers at
  `attachments` again instead of inviting model-written base64.
- `atlassian_jira`: an attachment that arrives as an unresolved file reference
  is refused with a message that tells a direct caller to send the file's
  bytes inline, instead of naming only the platform that resolves references.
- `outlook_email`: an empty `mailbox` on `get_email`, `list_attachments`,
  `read_attachment` and `download_attachment` means the signed-in user's own
  mailbox, as an omitted one does, instead of being refused as invalid.
- `otp_verification`: codes come from the operating system's CSPRNG instead of
  SQL `RANDOM()`, are stored only as a salted hash, and are compared in
  constant time. Asking again while a code is live now issues a replacement
  (the old one stops working) instead of returning the stored one, and the raw
  identifier is no longer written to the log on lockout. Rows written before
  this change hold plaintext codes, never match, and expire on their own.
- `outlook_email`: `attachment_urls` on `send_email`, `create_draft` and
  `reply_email` now use the framework's SSRF check instead of a private copy
  that missed carrier-grade NAT, IPv6 site-local and IPv4-in-IPv6 forms, and
  the address actually connected to is checked as well as the name. Redirects
  are validated again at each hop. Added `magone_mcp_core.net.reject_blocked_peer`.

[Unreleased]: https://github.com/magurelabs/magoneai-mcp/commits/main
