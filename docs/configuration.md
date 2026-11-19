# Configuration reference

Every environment variable the framework and the connectors read, with its
default. Collected by searching the code for `os.environ` / `os.getenv` and
for each `MCP_*` name the framework looks up; if you find one missing, that
is a documentation bug.

Boolean flags accept `1`, `true` or `yes` (case-insensitive). Anything else,
including unset, is off.

For how these fit together, see [architecture.md](architecture.md).

## Framework (`magone-mcp-core`)

Read by every connector.

| Variable | Default | Read | Meaning |
|---|---|---|---|
| `MCP_MODE` | `platform` | start-up | `platform`, `hosted` or `local`. An unrecognised value falls back to `platform` and logs a warning, so a typo cannot disable authentication. |
| `MCP_SERVICE_TOKEN` | unset | start-up | `platform` mode: the shared secret every caller must send as `X-Service-Token` on `/mcp`. Compared in constant time; a wrong or missing token is answered `403`. Ignored in other modes. An interim control: it proves the caller holds a secret, not which tenant it is. |
| `MCP_ALLOW_UNAUTHENTICATED` | off | start-up | Lets `platform` or `hosted` mode start with no caller authentication. Without it such a server refuses to start. Only for a deployment where something in front authenticates every request. |
| `MCP_RESOURCE_URL` | unset | start-up | With a `token_verifier`: this server's canonical URL, which tokens must name as their audience. Required when a verifier is passed. |
| `MCP_AUTH_ISSUER_URL` | unset | start-up | With a `token_verifier`: the authorization server's issuer URL. Required when a verifier is passed. |
| `MCP_ALLOW_INSECURE_BIND` | off | start-up | Lets `local` mode bind beyond loopback. Without it, `local` mode binds `127.0.0.1` whatever host it was given. Needed in a container; only on a network you control. |
| `MCP_ALLOWED_HOSTS` | loopback only | start-up | Comma-separated `Host` names callers use besides loopback, for example `mcp-linear,mcp-linear.tools.svc.cluster.local`. A bare name is accepted with or without a port. Any other Host is answered `421`. `*` disables the check (and logs a warning). |
| `MCP_ALLOWED_ORIGINS` | loopback only | start-up | Comma-separated browser origins allowed to call the server, for example `https://app.example.com`. A server-to-server call sends no `Origin` and is unaffected. |
| `MCP_MAX_BODY_BYTES` | `10485760` (10 MiB), or the connector's own default | start-up | Largest request body accepted. A larger one is answered `413` before it is parsed. A connector whose tools carry attachments may declare a larger default with `ConnectorServer(max_body_bytes=...)`: `atlassian_jira` declares 70 MiB and `outlook_email` 75 MiB. This variable overrides either. An unparseable or non-positive value falls back to the connector's default. Base64 attachments are about a third larger than the file. |
| `MCP_MAX_RESULT_CHARS` | `1000000` | start-up | Outbound budget: the most text one tool result may carry. Larger results are cut, visibly; a tool with a declared output schema cannot be cut and still conform, so its oversized result is returned as an error instead. Values below `1000` are raised to `1000`; an unparseable value falls back to the default. |
| `MCP_READ_ONLY` | off | every call | Refuses every tool not marked `readOnlyHint=True`, and hides them from `tools/list`. Read per call, so changing it needs no restart. |
| `MCP_CREDENTIALS_PATH` | unset | path at start-up, file per call | Path to a JSON object of credentials for a server you run yourself, keyed like the `credentials` dict (`{"linear_api_key": "..."}`). When the file has values it wins: a request header cannot override a key the file sets, and can fill only a key the file leaves unset, never a host, port, URL, endpoint, connection string or allowlist once the file holds a secret. A missing or unreadable file is treated as empty. |
| `MCP_LOG_RAW_UPSTREAM_ERRORS` | off | per log line | Logs unscrubbed upstream error bodies. Honoured only when `MCP_MODE=local`. |

## Process

Read by each connector's `__main__.py` and by the container image.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Interface to bind. In `local` mode the framework clamps a non-loopback host to `127.0.0.1` unless `MCP_ALLOW_INSECURE_BIND` is set. |
| `PORT` | `8000` | Port to listen on. The container healthcheck reads it too. |

The [Dockerfile](../Dockerfile) sets `MCP_MODE=platform` and `PORT=8000` as
image defaults, and takes `CONNECTOR` (required) and `SERVER_NAME` (must
match the connector's `server.json` name, when it has one) as build
arguments.

## Request headers

Not environment variables, but configuration a caller sends per request.

| Header | Meaning |
|---|---|
| `X-Service-Token` | The shared secret, in `platform` mode with `MCP_SERVICE_TOKEN` set. |
| `Authorization: Bearer ...` | An access token, when the server has a token verifier. |
| `X-MCP-Enabled-Tools` | Comma-separated tool names this connection may use. Wins over groups. A caller-supplied hint: it restricts anything only when a trusted host or gateway sets it (see [architecture.md](architecture.md#per-connection-tool-selection)). |
| `X-MCP-Enabled-Groups` | Comma-separated permission groups this connection may use. Same caveat; and most connectors have only the `default` group. |
| `X-Execution-Id` | `playwright` only: the caller's run id. Each run gets its own browser session and its artifacts are scoped to it. In `platform`/`hosted` mode a browser call with neither this header nor a verified identity is refused. |
| a tool's credential headers | For example `X-Linear-Api-Key`. Each tool declares the ones it may receive; [TOOLS.md](TOOLS.md) shows how many each tool takes and `credential_headers=` in `connectors/<name>/server.py` names them. |

## Connectors

Credentials always arrive per call (as headers, or from
`MCP_CREDENTIALS_PATH`). Where a connector also reads a credential from the
environment, that is a **fallback** for a standalone server one person runs;
the per-call value wins whenever it is present. (A credentials file is the
exception: it wins over headers, see `MCP_CREDENTIALS_PATH` above.)

### `web_search`

Pages are fetched with the User-Agent
`Mozilla/5.0 (compatible; magoneai-web-search/<version>; +https://github.com/magurelabs/magoneai-mcp)`,
so a site can see who is asking and address the connector by name in
robots.txt. `scrape_url` with `return_full=true`, the crawl path, checks the
site's robots.txt first and refuses a page it disallows; a single-page fetch
is one request on a user's behalf and does not. Whoever runs the connector is
responsible for complying with each target site's terms and with the terms of
the search provider in use (Serper, Perplexity, or the engines a SearXNG
instance queries).

| Variable | Default | Meaning |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8888` | SearXNG instance used for search when Serper is not selected. JSON output must be enabled on it. |
| `SEARXNG_DEFAULT_LANGUAGE` | `en` | Default search language for SearXNG. |
| `SEARXNG_DEFAULT_REGION` | empty | Default search region for SearXNG. |
| `SEARXNG_ENGINE_FAIL_THRESHOLD` | `2` | Consecutive failures before a SearXNG engine is rested. |
| `SEARXNG_ENGINE_COOLDOWN_SEC` | `600` | How long a failing SearXNG engine is rested, in seconds. |
| `SERPER_API_KEY` | empty | Serper key fallback. When a Serper key is available (per call or here), Serper is used instead of SearXNG. |
| `WEB_SEARCH_PROVIDER` | auto | Overrides the automatic choice. `serper` uses Serper (a key is still required); any other value, such as `searxng`, uses SearXNG even when a Serper key is present. |
| `PERPLEXITY_API_KEY` | empty | Perplexity key fallback, for the tool that uses it. |
| `CRAWL4AI_URL` | `http://localhost:11235` | Crawl4AI service used for page extraction. |

### `cloudflare_r2`

Fallbacks for the per-call credentials of the same name:

| Variable | Default | Meaning |
|---|---|---|
| `R2_ACCOUNT_ID` | empty | Account id. |
| `R2_ACCESS_KEY_ID` | empty | Access key id. |
| `R2_SECRET_ACCESS_KEY` | empty | Secret access key. |
| `R2_BUCKET` | empty | Bucket name. |
| `R2_PUBLIC_BASE_URL` | empty | Public base URL for shareable links. |
| `R2_ENDPOINT_URL` | `https://<account id>.r2.cloudflarestorage.com` | Endpoint override. |

### `docusign`

| Variable | Default | Meaning |
|---|---|---|
| `DOCUSIGN_AUTH_SERVER` | `account-d.docusign.com` | OAuth host. The default is the vendor's developer (demo) environment; set the production host for production accounts. |

### `filesystem`

| Variable | Default | Meaning |
|---|---|---|
| `FILESYSTEM_ROOT` | unset | The one directory this server exposes. Required: with it unset, every tool refuses. Must be an absolute path to an existing directory, and system directories are refused. |
| `FILESYSTEM_MAX_READ_BYTES` | `5242880` (5 MiB) | Largest file a read returns. |
| `FILESYSTEM_MAX_WRITE_BYTES` | `5242880` (5 MiB) | Largest content a write accepts. |

### `image_generation`

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | empty | API key fallback. |
| `IMAGE_GEN_MODEL` | `gemini-2.5-flash-image` | Model used to generate images. |
| `IMAGE_GEN_TIMEOUT` | `120` | Request timeout, in seconds. |

### `kapso_whatsapp`

| Variable | Default | Meaning |
|---|---|---|
| `KAPSO_API_KEY` | empty | API key fallback. |
| `KAPSO_PHONE_NUMBER_ID` | empty | Default sending number, used when neither the call nor the credentials name one. |
| `KAPSO_BASE_URL` | `https://api.kapso.ai/meta/whatsapp` | API base URL; override for testing. |
| `KAPSO_GRAPH_VERSION` | `v24.0` | Graph API version used in request paths. |

### `linear`

| Variable | Default | Meaning |
|---|---|---|
| `LINEAR_API_KEY` | empty | API key fallback. |
| `LINEAR_GRAPHQL_URL` | `https://api.linear.app/graphql` | GraphQL endpoint; override for testing. |

### `linkedin`

| Variable | Default | Meaning |
|---|---|---|
| `LINKEDIN_ORG_ID` | empty | Default post author when a call passes no `author`: the organization with this id is used instead of the authenticated member. It is process-wide, so on a server shared by several callers it changes the author for **every** caller; leave it unset there and have each call pass `author`. |
| `LINKEDIN_VERSION` | `202606` | Versioned-API month (`YYYYMM`) sent with each request. |
| `LINKEDIN_API_BASE` | `https://api.linkedin.com` | API base URL; override for testing or a proxy. |
| `LINKEDIN_TOKEN_URL` | `https://www.linkedin.com/oauth/v2/accessToken` | OAuth token endpoint; override for testing or a proxy. |

### `onedrive`

| Variable | Default | Meaning |
|---|---|---|
| `ONEDRIVE_MAX_UPLOAD_MB` | `250` | Largest single file accepted for upload, in megabytes (floor 1 MB). Checked before any bytes are decoded or sent. |

The connector also declares a 75 MiB request-body cap (the framework default is
10 MiB), so an inline upload above about 55 MB of file bytes needs
`MCP_MAX_BODY_BYTES` (above) raised as well.

### `outlook_email`

| Variable | Default | Meaning |
|---|---|---|
| `MAX_ATTACHMENT_DOWNLOAD_SIZE_MB` | `25` | Largest attachment downloaded, in megabytes. |

### `playwright`

Relays an operator-run Playwright MCP server (see
[`connectors/playwright/README.md`](../connectors/playwright/README.md)) and
returns the browser's files in the tool result. Needs a **single replica** (or
routing that keeps an `X-Execution-Id` on one replica).

| Variable | Default | Meaning |
|---|---|---|
| `PLAYWRIGHT_UPSTREAM_URL` | `http://playwright:8931/mcp` | The upstream Playwright MCP server. |
| `PLAYWRIGHT_SESSION_IDLE_MINUTES` | `15` | End a browser session, and forget its run's artifact record, idle this long. |
| `PLAYWRIGHT_MAX_SESSIONS` | `64` | Sessions kept at once; the least recently used idle one is ended first. |
| `PLAYWRIGHT_MAX_SESSIONS_PER_CALLER` | a quarter of `PLAYWRIGHT_MAX_SESSIONS` | Most sessions one caller may hold. |
| `PLAYWRIGHT_CALL_TIMEOUT_SECONDS` | `100` | Per relayed call. |
| `PLAYWRIGHT_MANIFEST_REFRESH_MINUTES` | `10` | How often the upstream tool list is re-read. |
| `PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS` | `false` | Expose the code-running tools (`browser_evaluate`, `browser_run_code`). |
| `ARTIFACTS_ROOT` | `/artifacts` | Mount point of the volume shared with the browser. |
| `ARTIFACTS_MAX_BYTES` | `26214400` (25 MB) | Largest file `publish_artifact` returns, and the most image bytes one relayed browser result may carry. |
| `ARTIFACTS_DELETE_AFTER_PUBLISH` | `true` | Remove a returned file from the volume, and the unfetched files of a run that went idle. |

### `twitter_intel`

This connector uses twitterapi.io, an unofficial third-party provider that is
not affiliated with X Corp, rather than the official X API. If you run it, you
are responsible for complying with the terms of X and of twitterapi.io.

| Variable | Default | Meaning |
|---|---|---|
| `TWITTERAPIIO_API_KEY` | empty | API key fallback. |

Every other connector reads nothing from the environment beyond the
framework and process variables above: its configuration arrives per call.
