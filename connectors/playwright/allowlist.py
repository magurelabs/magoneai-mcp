"""The upstream browser tools this connector relays, and their reviewed schemas.

Relaying is an allowlist, not a blocklist. The official Playwright MCP server
lists dozens of tools and a new image can add more (and change the ones it has),
so "relay everything the upstream lists except a few" would put tools nobody has
looked at — ``browser_file_upload`` reads the browser container's disk into a
page, and whatever a future release adds — in front of callers the moment the
image is pulled. Instead only the tools in ``ALLOWED`` below are ever relayed.

``ALLOWED`` is derived from ``upstream_snapshot.json``, a **capture of the pinned
image's own ``tools/list``** (regenerate it with ``capture_snapshot.py``), minus
the excluded tools. So each tool's reviewed input schema and annotations are the
real ones the image sends, reviewed in a pull request the way the local tools'
schemas are. ``gateway.apply_manifest`` relays a tool only when the upstream's
live schema and annotations still match this snapshot; a tool whose schema has
drifted (an upstream upgrade changed it) is withheld and logged rather than
relayed unreviewed. Bumping the pinned image (``UPSTREAM_IMAGE``) and
regenerating the snapshot go together.

Deliberately excluded, even though the pinned image lists them:

- ``browser_evaluate`` — runs arbitrary page code (kept in
  ``gateway.UNSAFE_TOOLS``, exposed only behind ``PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS``).
- ``browser_file_upload`` — puts a file from the browser container's filesystem
  into a page.
- ``browser_install`` — installs browser binaries into the container.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The upstream image the snapshot was captured from, and the capabilities it was
# started with. The allowlist (browser_pdf_save, the tracing tools) assumes these
# caps; the connector README's compose file enables them.
UPSTREAM_IMAGE = "mcr.microsoft.com/playwright/mcp:v0.0.42"
UPSTREAM_CAPS = ("pdf", "tracing")

# Listed by the pinned image but never relayed; see the module docstring.
EXCLUDED = frozenset({"browser_evaluate", "browser_file_upload", "browser_install"})

_SNAPSHOT_PATH = Path(__file__).with_name("upstream_snapshot.json")
_SNAPSHOT: dict[str, dict[str, Any]] = {
    tool["name"]: tool for tool in json.loads(_SNAPSHOT_PATH.read_text())
}

# name -> {"inputSchema": <reviewed schema>, "annotations": <reviewed annotations>}
ALLOWED: dict[str, dict[str, Any]] = {
    name: {"inputSchema": tool.get("inputSchema"), "annotations": tool.get("annotations")}
    for name, tool in _SNAPSHOT.items()
    if name not in EXCLUDED
}

ALLOWED_NAMES = frozenset(ALLOWED)


def reviewed(name: str) -> dict[str, Any] | None:
    """The reviewed snapshot for an allowlisted tool, or None if it is not on the list."""
    return ALLOWED.get(name)


def schema_matches_snapshot(name: str, tool: dict[str, Any]) -> bool:
    """True when the upstream tool's schema and annotations still match the snapshot.

    An allowlisted tool whose upstream schema or annotations have drifted from the
    reviewed snapshot (an upstream upgrade changed it) is not relayed until the
    snapshot is regenerated and re-approved.
    """
    snapshot = ALLOWED.get(name)
    if snapshot is None:
        return False
    schema = tool.get("inputSchema")
    schema = schema if isinstance(schema, dict) else {"type": "object"}
    return schema == snapshot["inputSchema"] and tool.get("annotations") == snapshot["annotations"]
