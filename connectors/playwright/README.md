# `playwright`

Browser automation through Microsoft's official Playwright MCP server, plus
three tools that hand the browser's files back to the caller. **Not
published** (see `connectors/publication.json`): it has not yet run against
the browser container in a cluster.

## Architecture

```
caller --(MCP, X-Execution-Id)--> connector :8000 --(browser_* relayed)--> playwright :8931
                                      |              no caller headers      (official image,
                                      |                                      private network)
                                      +------ both mount one volume at /artifacts ------+
```

The connector is a relay. You run the browser yourself, as the official
`mcr.microsoft.com/playwright/mcp` image, next to it:

```yaml
services:
  playwright:
    # Pin an exact released tag, never `latest`: a new upstream release can add,
    # rename or change the relayed browser tools under a running deployment. The
    # allowlist in `allowlist.py` is a snapshot captured from THIS tag (via
    # `capture_snapshot.py`) and only relays tools whose schema still matches it;
    # bump the tag and regenerate the snapshot together (`UPSTREAM_IMAGE` there).
    image: mcr.microsoft.com/playwright/mcp:v0.0.42
    working_dir: /artifacts          # files the agent names land on the volume too
    command: >
      --isolated --host 0.0.0.0 --port 8931 --caps pdf,tracing
      --save-video=800x600
      --allowed-hosts playwright:8931
      --output-dir /artifacts
      --blocked-origins "http://169.254.169.254;http://metadata.google.internal;http://localhost;http://127.0.0.1;http://mcp-playwright:8000"
    volumes: [playwright-output:/artifacts]
    networks: [playwright-network]   # no published port
    # 0.0.42's capabilities are `vision`, `pdf`, `testing` and `tracing`. The
    # allowlist's snapshot was captured with `--caps pdf,tracing` (for
    # browser_pdf_save and browser_start_tracing/browser_stop_tracing). A tool a
    # cap adds is relayed only if it is in that snapshot, so adding a cap without
    # regenerating the snapshot exposes nothing. Recording is the `--save-video`
    # launch option, not a tool; the .webm lands in the output dir.

  mcp-playwright:
    build:
      context: .
      args: { CONNECTOR: playwright }
    user: "1000"                     # the Playwright image's `node` user, see below
    environment:
      MCP_MODE: platform             # or hosted / local, as for any connector
      MCP_ALLOWED_HOSTS: mcp-playwright
      PLAYWRIGHT_UPSTREAM_URL: http://playwright:8931/mcp
      ARTIFACTS_ROOT: /artifacts
    volumes: [playwright-output:/artifacts]
    networks: [default, playwright-network]

volumes: { playwright-output: {} }
networks: { playwright-network: {} }
```

- **Same mount path in both** (`/artifacts`), so an absolute path Playwright
  prints resolves here unchanged.
- **`--blocked-origins` and the private network.** The browser fetches
  whatever URL the model asks for, from inside your network. Block your own
  internal services and the cloud metadata endpoint by origin, and keep the
  container on a network that cannot route to them: origin blocking alone does
  not cover every IP literal or DNS trick. Adjust the list to your deployment.
- **File ownership.** The image runs as uid 10001; the Playwright image as
  uid 1000. Removing a file after returning it needs write access to the
  directory Playwright created, so run the connector as `user: "1000"` (or give
  the volume a shared group). Otherwise the file is still returned and the
  delete is logged as `artifact_delete_failed`.
- Fonts (`fonts/`, DejaVu, licence in `fonts/LICENSE_DEJAVU.txt`), the HTML
  template and its stylesheet live in this package directory, which the
  repository's Dockerfile copies whole, so they ship with the image.

## Tools

Relaying is an **allowlist**, not "everything except a few". Only the browser
tools named in [`allowlist.py`](allowlist.py) are relayed (`browser_navigate`,
`browser_click`, `browser_take_screenshot`, `browser_start_tracing`, ... — 21 of
the 24 the pinned image lists), each with its own schema and annotations, in
permission group `browser`, so `MCP_READ_ONLY`,
`X-MCP-Enabled-Tools`/`-Groups` and the outbound text budget apply to them as to
any tool. The allowlist is built from [`upstream_snapshot.json`](upstream_snapshot.json),
a capture of the pinned image's own `tools/list` (regenerate with
[`capture_snapshot.py`](capture_snapshot.py)): a tool whose upstream schema or
annotations have drifted from that snapshot is withheld and logged rather than
relayed unreviewed, so bumping the pinned image and regenerating the snapshot go
together. A relayed result comes back as the upstream sent it: screenshots stay
image content, and `browser_take_screenshot`'s description gains a note to call
it with no arguments for a full-page shot.

Deliberately **not** on the allowlist: `browser_file_upload` (puts a file from
the browser container's disk into a page), `browser_install` (installs browser
binaries), and `browser_evaluate` / `browser_run_code` / `browser_run_code_unsafe`
(run arbitrary code — these stay hidden and are refused unless
`PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS=true`).

The local tools (group `artifacts`) return files IN THE TOOL RESULT as MCP
embedded resources: `{"type": "resource", "resource": {"uri":
"artifact://<name>", "mimeType": "...", "blob": "<base64>"}}`. Nothing is
stored anywhere by this connector. A platform such as MagOne stores the
returned resources itself; a laptop client can save them to disk.

| Tool | Returns |
|---|---|
| `publish_artifact(path, filename?)` | a JSON text item (`filename`, `uri`, `mime_type`, `size_bytes`, `deleted_local_copy`) and one embedded resource |
| `list_artifacts(since_minutes=60, limit=50)` | JSON text: `{artifacts: [{path, size_bytes, modified_at}], count, truncated}` |
| `publish_test_report(report, video_path?, include_videos=false)` | a JSON text item (`files`, `counts`, `videos_embedded`, `dropped`, `recordings_not_embedded`, `deleted_local_copies`, `pdf_error` if any) and embedded resources `test-report-<stamp>.html` (`text/html`), `test-report-<stamp>.pdf` (`application/pdf`), `test-cases-<stamp>.csv` (`text/csv`) |

`path` takes any form Playwright prints (`/artifacts/page-1.png`,
`page-1.png`, `../../artifacts/video-1.webm`, or the whole `[Video](...)`
link). The REAL path (symlinks followed) must be inside `ARTIFACTS_ROOT`; only
`.png .jpg .jpeg .webp .webm .mp4 .zip .pdf .yml .yaml .txt .md` are returned.
A file is deleted from the volume only after it has been read in full.

The report is self-contained HTML (inline CSS, no scripts, light and dark),
with every value escaped (Jinja2 autoescape; only an http(s) `url` becomes a
link); a PDF of the same content (fpdf2, DejaVu Sans, so Arabic, Cyrillic and
Greek render); and a CSV of the test cases (UTF-8 with BOM; a cell starting
with `= + - @`, tab or CR gets a leading `'` so a spreadsheet keeps it text).

### The size budget, and why recordings are off by default

A report goes back in ONE tool result, and clients measure results in
characters: `MCP_MAX_RESULT_CHARS`, 1,000,000 by default. Base64 turns 3
bytes into 4 characters, so the three files together may carry about 735 KB
(three quarters of the budget, less a margin for the JSON). A single
recording usually exceeds that on its own (a report with per-case videos
measured about 10 MB), so `include_videos` defaults to `false`: the report
names each recording and lists it in `recordings_not_embedded`, the files
stay on the volume, and `publish_artifact` returns each one in its own call,
up to `ARTIFACTS_MAX_BYTES`. Within the budget, the HTML comes first:
screenshots that do not fit are left out (largest first, then per-case
recordings, then the full run when videos are included) and listed in
`dropped`; the PDF then gets its screenshots if room is left, else names them,
else is left out with `pdf_error`. Only a report too big without any media
fails. Raise `MCP_MAX_RESULT_CHARS` (and `ARTIFACTS_MAX_BYTES`) for bigger
reports, if your client accepts them.

## Run identity

Each caller run gets its own browser session (cookies, logins, open pages),
keyed by the verified caller (the access token's principal, when a token
verifier authenticates requests) and the `X-Execution-Id` header. `X-Project-Id`
is not used: this repository does not take tenancy from a header. In `platform`
mode the platform is the only caller and sets a distinct run id per run.

**A browser call with neither a verified identity nor an `X-Execution-Id` is
refused** in `platform` and `hosted` mode, rather than falling back to a shared
`default` browser that several callers would land in together. `local` mode is a
single user on loopback, where one default session is fine.

**Artifacts are scoped to the run that produced them.** The gateway records the
files each run's own relayed results name, and `publish_artifact`,
`list_artifacts` and `publish_test_report` only read, list, embed or delete
files that run produced. One run cannot fetch — or, since delete-after-publish is
on, remove — another run's screenshots. (In `local` mode, the single caller sees
every file.)

A caller may hold at most `PLAYWRIGHT_MAX_SESSIONS_PER_CALLER` sessions, so one
caller opening runs cannot crowd others out; a session with a call in flight is
never closed to make room.

**Single replica.** The session pool and the per-run artifact record live in this
process, so run them on **one replica**, or route so an `X-Execution-Id` always
lands on the same replica. With two replicas sharing the artifacts volume, a
run's next call can reach the other replica and get a fresh browser, and the
per-run artifact scoping would not see the other replica's records. This is why
the connector is `blocked` until its deployment shape is settled.

The upstream is stateful: the connector sends `initialize`, carries
`Mcp-Session-Id` on every call, parses JSON or SSE replies and re-initializes
once if the upstream forgot the session. Sessions idle for
`PLAYWRIGHT_SESSION_IDLE_MINUTES` are ended with `DELETE` (the upstream then
closes the browser), as is a session right after `browser_close` and every
session at shutdown. Only `Accept`, `Content-Type`, `Mcp-Session-Id` and
`MCP-Protocol-Version` reach the upstream; no caller header does.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PLAYWRIGHT_UPSTREAM_URL` | `http://playwright:8931/mcp` | The Playwright MCP server. On a laptop: `npx @playwright/mcp --port 8931 --output-dir <dir>` and `http://localhost:8931/mcp`. |
| `PLAYWRIGHT_SESSION_IDLE_MINUTES` | `15` | End a browser session idle this long. |
| `PLAYWRIGHT_MAX_SESSIONS` | `64` | Sessions kept at once; the least recently used idle session is ended first. |
| `PLAYWRIGHT_MAX_SESSIONS_PER_CALLER` | a quarter of `PLAYWRIGHT_MAX_SESSIONS` | Most sessions one caller may hold, so one caller cannot evict every other. |
| `PLAYWRIGHT_CALL_TIMEOUT_SECONDS` | `100` | Per relayed call. |
| `PLAYWRIGHT_MANIFEST_REFRESH_MINUTES` | `10` | How often the upstream tool list is re-read. A failed read backs off from 5 seconds to a minute. |
| `PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS` | `false` | Expose the code-running tools. |
| `ARTIFACTS_ROOT` | `/artifacts` | Mount point of the shared volume. |
| `ARTIFACTS_MAX_BYTES` | `26214400` (25 MB) | Largest file `publish_artifact` returns; can only lower the report budget. Raise it to return a larger recording. |
| `ARTIFACTS_DELETE_AFTER_PUBLISH` | `true` | Remove a returned file from the volume. `false`, `0`, `no` or `off` keeps it. |
| `MCP_MAX_RESULT_CHARS` | `1000000` | The framework's result budget; sets the report budget. |

The volume is shared by every caller of one browser container, but the local
tools are scoped to the run that produced a file (see **Run identity**), so
`list_artifacts` shows only the caller's own files and `publish_artifact`
refuses another run's. For a stronger boundary still, run one browser and volume
per tenant.
