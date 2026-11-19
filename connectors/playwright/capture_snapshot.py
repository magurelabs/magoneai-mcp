"""Regenerate ``upstream_snapshot.json`` from a running Playwright MCP server.

The allowlist's reviewed schemas come from the pinned upstream image's own
``tools/list`` (see ``allowlist.py``). Run this against that exact image to
refresh the snapshot when bumping ``UPSTREAM_IMAGE``:

    # start the pinned image (the caps the allowlist assumes), then:
    docker run --rm -p 8931:8931 mcr.microsoft.com/playwright/mcp:v0.0.85 \\
        --port 8931 --host 0.0.0.0 --headless --caps devtools,pdf
    python -m connectors.playwright.capture_snapshot http://127.0.0.1:8931/mcp

It writes the tools exactly as the server sends them (so the gateway's drift
check compares like for like), sorted by name, next to this file. Review the
diff before committing; ``allowlist.py`` derives ``ALLOWED`` from it and the
tests exercise these shapes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .upstream_rpc import UpstreamRpc
from .upstream_sessions import SessionPool

DEFAULT_URL = "http://127.0.0.1:8931/mcp"
SNAPSHOT_PATH = Path(__file__).with_name("upstream_snapshot.json")


async def _capture(url: str) -> list[dict]:
    pool = SessionPool(UpstreamRpc(url, 30.0), idle_seconds=60, max_sessions=4)
    tools = await pool.list_tools()
    return sorted((t for t in tools if isinstance(t, dict)), key=lambda t: t["name"])


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    tools = asyncio.run(_capture(url))
    SNAPSHOT_PATH.write_text(json.dumps(tools, indent=2) + "\n")
    print(f"wrote {len(tools)} tools to {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
