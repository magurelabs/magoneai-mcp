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
`mcr.microsoft.com/playwright/mcp` image, next to it. The browser fetches
whatever URL the model asks for, so it must not be able to reach your network:
in this example it sits on an `internal: true` network with no route out at
all, and reaches the internet only through an egress proxy that refuses every
private, loopback, link-local (cloud metadata) and other non-public address.
The proxy checks the address a hostname RESOLVES to, so a wildcard-DNS name
pointing at `169.254.169.254` is refused like the literal IP.

```yaml
services:
  playwright:
    # Pin an exact released tag, never `latest`: a new upstream release can add,
    # rename or change the relayed browser tools under a running deployment. The
    # allowlist in `allowlist.py` is a snapshot captured from THIS tag (via
    # `capture_snapshot.py`) and only relays tools whose schema still matches it;
    # bump the tag and regenerate the snapshot together (`UPSTREAM_IMAGE` there).
    image: mcr.microsoft.com/playwright/mcp:v0.0.85
    working_dir: /artifacts          # files the agent names land on the volume too
    command: >
      --isolated --host 0.0.0.0 --port 8931 --caps devtools,pdf
      --allowed-hosts playwright:8931
      --output-dir /artifacts
      --proxy-server http://egress-proxy:3128
      --blocked-origins "http://169.254.169.254;http://metadata.google.internal;http://localhost;http://127.0.0.1;http://mcp-playwright:8000"
    volumes: [playwright-output:/artifacts]
    networks: [playwright-network]   # internal: no route out, no published port
    # 0.0.85's capabilities are `vision`, `pdf` and `devtools`. The allowlist's
    # snapshot was captured with `--caps devtools,pdf`: `pdf` adds
    # browser_pdf_save, `devtools` adds tracing (browser_start_tracing /
    # browser_stop_tracing) and screen recording (browser_start_video /
    # browser_stop_video / browser_video_chapter; the .webm lands in the output
    # dir and browser_stop_video reports its path). A tool a cap adds is relayed
    # only if it is in that snapshot, so adding a cap without regenerating the
    # snapshot exposes nothing.
    #
    # Pinned at 0.0.85, not 0.0.42: 0.0.42 starts with a relative `cli.js`, so
    # the `working_dir` above stops it starting; its image ships no
    # chromium_headless_shell, so a headless launch fails and every navigation
    # comes back as an empty reply; and once launched it closed the page right
    # after navigating on some sites. 0.0.85 runs this compose file as written.

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

  egress-proxy:
    # The browser's only way out. Any forward proxy that can refuse by
    # destination address works; pin the exact tag (or digest) you have vetted.
    image: ubuntu/squid:5.2-22.04_beta
    configs: [{ source: egress-proxy, target: /etc/squid/squid.conf }]
    networks: [playwright-network, playwright-egress]

configs:
  egress-proxy:
    content: |
      http_port 3128
      cache deny all
      # Every destination that is not the public internet, IPv4 and IPv6:
      # this-host, RFC 1918, CGNAT, loopback, link-local (169.254.169.254 and
      # fd00:ec2::254, the cloud metadata services), benchmarking, multicast,
      # reserved, unique-local and IPv4-mapped. `dst` is matched on the
      # address the name resolves to, not on the name.
      acl blocked_dst dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8
      acl blocked_dst dst 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16
      acl blocked_dst dst 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
      acl blocked_dst dst ::/128 ::1/128 ::ffff:0:0/96 fc00::/7 fe80::/10 ff00::/8
      acl web_ports port 80 443
      acl CONNECT method CONNECT
      http_access deny !web_ports
      http_access deny CONNECT !web_ports
      http_access deny blocked_dst
      # Only the browser can reach this port: playwright-network is internal.
      http_access allow all

volumes: { playwright-output: {} }
networks:
  playwright-network: { internal: true }   # no gateway: nothing leaves except via the proxy
  playwright-egress: {}                    # the proxy's own way out
```

**What you must adapt before copying this:**

- **Your own public-address services.** The proxy refuses private and
  link-local ranges. An internal service on a public IP (an admin UI, a
  partner API on an allowlisted IP) is reachable unless you add its address to
  `blocked_dst`. Add them.
- **Ports.** Only 80 and 443 are allowed out. Add others to `web_ports` if your
  tests need them.
- **The proxy image.** Pin the tag or digest you have reviewed; `ubuntu/squid`
  is one choice, not a requirement. Anything that resolves the name and refuses
  by destination address does the job.
- **Kubernetes.** `internal: true` has no direct equivalent: give the browser
  pod an egress `NetworkPolicy` that allows only DNS and the proxy, and run the
  same proxy with the same ACL.
- **`--blocked-origins`** stays as a second layer; replace `mcp-playwright:8000`
  with your connector's service name. It matches origins, not addresses, so it
  is not the boundary on its own.

- **Same mount path in both** (`/artifacts`), so an absolute path Playwright
  prints resolves here unchanged.
- **The private network is the boundary, not `--blocked-origins`.** Origin
  blocking alone does not cover every IP literal or DNS trick (a hostname that
  resolves to the metadata address does not match its origin). With
  `internal: true` and the proxy above, a page can reach only what the proxy
  lets through.
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
`browser_click`, `browser_take_screenshot`, `browser_start_video`, ...: 31 of
the 39 the pinned image lists), each with its own schema and annotations, in
permission group `browser`, so `MCP_READ_ONLY`,
`X-MCP-Enabled-Tools`/`-Groups` and the outbound text budget apply to them as to
any tool. The allowlist is built from [`upstream_snapshot.json`](upstream_snapshot.json),
a capture of the pinned image's own `tools/list` (regenerate with
[`capture_snapshot.py`](capture_snapshot.py)): a tool whose upstream schema or
annotations have drifted from that snapshot is withheld and logged rather than
relayed unreviewed, so bumping the pinned image and regenerating the snapshot go
together. A relayed result comes back as the upstream sent it, with two
changes. Every text item (page snapshots, titles, console output: the page's
words) is wrapped in the framework's `<<<UNTRUSTED_CONTENT source=playwright>>>`
envelope, as other connectors mark scraped or third-party text; screenshots
stay image content, untouched. And the images (and any other binary items) of
one result may total at most `ARTIFACTS_MAX_BYTES`: a full-page screenshot of a
very long page that would go over is replaced by a one-line note giving its
size and suggesting a smaller capture. `browser_take_screenshot`'s description
gains a note that no arguments captures the viewport while `fullPage: true`
captures the whole page.

Deliberately **not** on the allowlist: `browser_file_upload` and
`browser_drop` (put files from the browser container's disk into a page),
`browser_annotate`, `browser_start_recording`, `browser_stop_recording` and
`browser_resume` (interactive: they wait for or record a human at the browser,
which never happens in a headless relay), and `browser_evaluate` / `browser_run_code` / `browser_run_code_unsafe`
(run arbitrary code: these stay hidden and are refused unless
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

**Artifacts are scoped to the run that produced them.** A file joins a run's
scope only when all three hold: Playwright's own report lines in that run's
reply name it (the `- [Screenshot of viewport](path)` links under `### Snapshot`,
or under `### Result` for a tool that writes files: screenshot, PDF, snapshot,
video and trace stop; never the page's URL, title or text, and nothing from a
tool that echoes the page, such as `browser_console_messages`), the file
exists, and it was written during that call. `publish_artifact`, `list_artifacts` and
`publish_test_report` only read, list, embed or delete files in the caller's
scope, and a file rewritten since it was recorded drops out of it. So a page
that shows another run's file name, or a list of guessed names, gets a run
nothing: `list_artifacts` stays empty and `publish_artifact` gives the same
refusal whether or not the named file exists. When two runs ask Playwright for
the same `filename`, the later screenshot overwrites the earlier one on disk,
and the file belongs to the later run. (In `local` mode, the single caller sees
every file.)

A caller may hold at most `PLAYWRIGHT_MAX_SESSIONS_PER_CALLER` sessions, so one
caller opening runs cannot crowd others out; a session with a call in flight is
never closed to make room.

**Single replica.** The session pool and the per-run artifact record live in this
process, so run them on **one replica**, or route so an `X-Execution-Id` always
lands on the same replica. What a run sees when that does not hold:

- **A restart mid-run** (a deploy, an OOM kill): the run's next
  `publish_artifact` or `list_artifacts` is refused with "This server has no
  record of this run ..." naming the likely causes, not an empty list. Its files
  are still on the volume but can no longer be returned, and its next browser
  call starts a fresh browser (logins and open pages are gone).
- **Two replicas, or a rolling update, without sticky routing:** the same
  refusal and a fresh browser whenever a call lands on the replica that did not
  serve the run before.
- **A run idle longer than `PLAYWRIGHT_SESSION_IDLE_MINUTES`:** its browser is
  closed and its record dropped; the files it produced but never fetched are
  deleted from the volume (kept when `ARTIFACTS_DELETE_AFTER_PUBLISH` is off).
- **Files orphaned by a restart** belong to no record and are never deleted by
  the connector. Clean the volume on a schedule, for example a daily job
  running `find /artifacts -type f -mmin +1440 -delete`.

This is one reason the connector is `blocked` until its deployment shape is
settled.

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
| `ARTIFACTS_MAX_BYTES` | `57671680` (55 MB) | Largest file `publish_artifact` returns, and the most image bytes one relayed browser result may carry; can only lower the report budget. 55 MB fits a platform that uploads it onward (e.g. OneDrive's 75 MiB body cap). Raise or lower it as needed. |
| `ARTIFACTS_DELETE_AFTER_PUBLISH` | `true` | Remove a returned file from the volume, and the unfetched files of a run that went idle. `false`, `0`, `no` or `off` keeps both. |
| `MCP_MAX_RESULT_CHARS` | `1000000` | The framework's result budget; sets the report budget. |

The volume is shared by every caller of one browser container, but the local
tools are scoped to the run that produced a file (see **Run identity**), so
`list_artifacts` shows only the caller's own files and `publish_artifact`
refuses another run's. For a stronger boundary still, run one browser and volume
per tenant.

## Before unblocking

Every test here fakes the upstream, so none of them catches a mismatch with
the real image. Before `connectors/publication.json` moves this connector to
`ready`, run these against the pinned `v0.0.85` image with the compose file
above, and record the results in the pull request that unblocks it:

1. **Manifest matches the snapshot.** `tools/list` on the connector shows all
   31 allowlisted tools, and the log has no `upstream_tool_schema_drift` lines.
   (The first snapshot did not match the real image, so this has already gone
   wrong once.)
2. **Screenshot round trip.** `browser_navigate`, `browser_take_screenshot`,
   then `publish_artifact` with the path Playwright printed: the file comes
   back and is deleted from the volume (the uid 1000 versus 10001 ownership
   question in **File ownership** above).
3. **Video.** `browser_start_video`, a few actions, `browser_stop_video`, then
   `publish_artifact` on the `.webm`: not empty.
4. **Two runs at once** (different `X-Execution-Id`): separate cookies, and
   neither can list or fetch the other's files, including when one run's page
   shows the other's file name.
5. **Session lifecycle.** Idle eviction closes the browser upstream, and
   `browser_close` ends the session.
6. **Network.** `browser_navigate` to `http://169.254.169.254/`, to a hostname
   that resolves to it, and to an internal service is refused, while a public
   site still loads through the proxy.
7. **Health.** `/healthz` answers at once with the browser container stopped
   or hung.
