"""The upstream browser tools this connector will relay, and their reviewed schemas.

Relaying is an allowlist, not a blocklist. The official Playwright MCP server
lists dozens of tools and a new image can add more (and change the ones it has),
so "relay everything the upstream lists except a few" would put tools nobody has
looked at — ``browser_file_upload`` reads the browser container's disk into a
page, and whatever a future release adds — in front of callers the moment the
image is pulled. Instead only the names in ``ALLOWED`` below are ever relayed.

``ALLOWED`` is also a **snapshot of each tool's reviewed input schema and
annotations**, checked in so the relayed contract is reviewed in a pull request
the way the local tools' schemas are. ``gateway.apply_manifest`` relays a tool
only when the upstream's advertised schema and annotations still match this
snapshot; a tool whose schema has drifted (an upstream upgrade changed it) is
withheld and logged rather than relayed unreviewed. Bumping the pinned upstream
image and re-approving this file go together.

Deliberately excluded, even though the upstream lists them:

- ``browser_evaluate``, ``browser_run_code``, ``browser_run_code_unsafe`` — run
  arbitrary page or Playwright code (kept in ``gateway.UNSAFE_TOOLS``, exposed
  only behind ``PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS``).
- ``browser_file_upload`` — puts a file from the browser container's filesystem
  into a page.
- ``browser_install`` — installs browser binaries into the container.

This connector is ``blocked`` in ``publication.json`` and has not run against the
pinned image yet. Before it is unblocked, regenerate the schemas here from that
exact image (the gateway withholds any tool whose live schema differs, so a
stale snapshot fails safe — it relays nothing — rather than relaying something
unreviewed).
"""

from __future__ import annotations

from typing import Any

# name -> {"inputSchema": <reviewed schema>, "annotations": <reviewed annotations or None>}
ALLOWED: dict[str, dict[str, Any]] = {
    "browser_navigate": {
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_navigate_back": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_click": {
        "inputSchema": {
            "type": "object",
            "properties": {"element": {"type": "string"}, "ref": {"type": "string"}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_type": {
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {"type": "string"},
                "ref": {"type": "string"},
                "text": {"type": "string"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_press_key": {
        "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_hover": {
        "inputSchema": {
            "type": "object",
            "properties": {"element": {"type": "string"}, "ref": {"type": "string"}},
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_select_option": {
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {"type": "string"},
                "ref": {"type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_snapshot": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_take_screenshot": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_pdf_save": {
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_wait_for": {
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "textGone": {"type": "string"},
                "time": {"type": "number"},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_handle_dialog": {
        "inputSchema": {
            "type": "object",
            "properties": {
                "accept": {"type": "boolean"},
                "promptText": {"type": "string"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_resize": {
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "number"}, "height": {"type": "number"}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    "browser_console_messages": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_network_requests": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_tabs": {
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "index": {"type": "number"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    "browser_start_video": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_stop_video": {
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_video_chapter": {
        "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "browser_close": {
        "inputSchema": {"type": "object"},
        "annotations": None,
    },
}

ALLOWED_NAMES = frozenset(ALLOWED)


def reviewed(name: str) -> dict[str, Any] | None:
    """The reviewed snapshot for an allowlisted tool, or None if it is not on the list."""
    return ALLOWED.get(name)


def schema_matches_snapshot(name: str, tool: dict[str, Any]) -> bool:
    """True when the upstream tool's schema and annotations still match the snapshot.

    An allowlisted tool whose upstream schema or annotations have drifted from the
    reviewed snapshot (an upstream upgrade changed it) is not relayed until the
    snapshot is re-approved.
    """
    snapshot = ALLOWED.get(name)
    if snapshot is None:
        return False
    schema = tool.get("inputSchema")
    schema = schema if isinstance(schema, dict) else {"type": "object"}
    return schema == snapshot["inputSchema"] and tool.get("annotations") == snapshot["annotations"]
