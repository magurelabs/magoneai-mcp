"""The Playwright connector.

Browser automation through Microsoft's official Playwright MCP server, which
the operator runs next to this connector, plus three tools of its own that
hand the browser's files back to the caller.

3 tools, 1 of them proposed read-only. Those are the local tools below; the
reviewed browser tools the upstream lists (``browser_navigate``,
``browser_click``, ``browser_take_screenshot``, ``browser_start_tracing``, ...)
are relayed as well, in permission group ``browser``, with their own schema and
annotations, from the allowlist in ``allowlist.py``. See ``gateway.py``.
``browser_evaluate``, ``browser_run_code`` and
``browser_run_code_unsafe`` run arbitrary code and are hidden and refused
unless ``PLAYWRIGHT_EXPOSE_UNSAFE_TOOLS`` is on.

## Where files go

Nowhere this connector owns. Playwright writes screenshots, recordings and
traces to a volume shared with this connector (``ARTIFACTS_ROOT``); the tools
here read them and return them IN THE TOOL RESULT as MCP embedded resources
(``EmbeddedResource`` with ``BlobResourceContents``: an ``artifact://<name>``
URI, a MIME type and base64 bytes). The caller keeps them wherever it keeps
files: a platform such as MagOne stores them itself, a laptop client can save
them to disk. Screenshots a browser tool returns stay as the upstream returns
them, image content.

## Run identity

Each caller run gets its own browser session, keyed by the verified caller
(when a token verifier authenticates requests) and the ``X-Execution-Id``
header. Without that header a caller's calls share one ``default`` session.
"""

from typing import Any

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError

from magone_mcp_core import ConnectorServer, declare_schema

from . import config, gateway
from .files import read_artifact
from .listing import list_recent
from .paths import ArtifactError, resolve_artifact
from .report_build import build_report
from .report_model import TestReport, format_validation_error

SERVER_NAME = "playwright"
SERVER_VERSION = "0.1.0"

MAX_SINCE_MINUTES = 7 * 24 * 60
MAX_LIST_LIMIT = 500
ARTIFACTS_GROUP = "artifacts"

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description=(
        "Browser automation with Microsoft Playwright, returning screenshots, "
        "recordings and HTML, PDF and CSV test reports in the tool result."
    ),
)


def _json_text(payload: dict) -> TextContent:
    import json

    return TextContent(type="text", text=json.dumps(payload, indent=2))


def _require_own_artifact(resolved: Any, ctx: Any) -> None:
    """Refuse a file this caller's own run did not produce (outside ``local`` mode)."""
    allowed = browser.allowed_paths(ctx)
    if allowed is not None and str(resolved) not in allowed:
        raise ArtifactError(
            "That file was not produced by this run. Only files the browser saved in "
            "your own run can be returned; call list_artifacts to see them."
        )


@server.tool(
    name="publish_artifact",
    description=(
        "Return a screenshot, recording (.webm), trace (.zip), PDF or snapshot that the "
        "Playwright browser saved, as an embedded resource in this tool result. Call it "
        "after browser_take_screenshot, browser_pdf_save, browser_stop_tracing or "
        "browser_stop_video, passing the path Playwright reported; if none was reported, "
        "use list_artifacts to find the file. The local copy is deleted once read "
        "unless the server keeps it."
    ),
    # Not read-only: by default the file is removed from the shared volume
    # once it has been read, so a second call finds nothing.
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
    permission_group=ARTIFACTS_GROUP,
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path of the file exactly as Playwright printed it, e.g. "
                    "'../../artifacts/video-1.webm', '/artifacts/page-1.png' or the whole "
                    "'[Video](...)' link."
                ),
            },
            "filename": {
                "type": ["string", "null"],
                "description": "Optional name to return it under; the extension is kept.",
            },
        },
        "required": ["path"],
    }
)
async def publish_artifact(path: str, filename: str | None = None, ctx: Any = None) -> Any:
    options = {
        "filename": filename,
        "max_bytes": config.max_bytes(),
        "delete": config.delete_after_publish(),
    }
    try:
        resolved = resolve_artifact(path, config.artifacts_root())
        _require_own_artifact(resolved, ctx)
        summary, resource = read_artifact(resolved, options)
    except ArtifactError as exc:
        server.logger.warning("artifact_refused", reason=str(exc)[:200])
        return {"error": str(exc)}
    return CallToolResult(content=[_json_text(summary), resource])


@server.tool(
    name="list_artifacts",
    description=(
        "List files the Playwright browser saved recently (newest first) with their "
        "size and modified time. Use it to find a video or trace whose path the "
        "Playwright reply did not name, then pass that path to publish_artifact."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    permission_group=ARTIFACTS_GROUP,
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "since_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10080,
                "default": 60,
                "description": "Only files modified within this many minutes.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 50,
                "description": "Maximum number of files to return.",
            },
        },
    }
)
async def list_artifacts(since_minutes: int = 60, limit: int = 50, ctx: Any = None) -> dict:
    since = max(1, min(int(since_minutes), MAX_SINCE_MINUTES))
    capped = max(1, min(int(limit), MAX_LIST_LIMIT))
    result = list_recent(config.artifacts_root(), since, capped, browser.allowed_paths(ctx))
    server.logger.info("artifacts_listed", count=result["count"], since_minutes=since)
    return result


@server.tool(
    name="publish_test_report",
    description=(
        "Build a QA test report and return it as three embedded resources: one "
        "self-contained HTML file (summary, issues by severity, test cases, console "
        "errors, with screenshots embedded), a PDF of the same report and a CSV of the "
        "test cases. Call it once at the end of a test session with the recording path "
        "(the .webm in the output dir; list_artifacts finds it) and the screenshot paths "
        "Playwright reported. Recordings are not embedded unless include_videos is true; "
        "the result lists "
        "them, and publish_artifact returns each one on its own."
    ),
    # Not read-only: the screenshots and recordings it embeds are removed from
    # the shared volume afterwards, unless the server keeps them.
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
    permission_group=ARTIFACTS_GROUP,
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "report": {
                "description": (
                    "The test report: what was tested, the test cases, the issues found."
                ),
                "properties": {
                    "title": {"maxLength": 500, "minLength": 1, "title": "Title", "type": "string"},
                    "url": {"maxLength": 4000, "title": "Url", "type": "string"},
                    "tested_at": {"format": "date-time", "title": "Tested At", "type": "string"},
                    "environment": {
                        "anyOf": [{"maxLength": 500, "type": "string"}, {"type": "null"}],
                        "default": None,
                        "title": "Environment",
                    },
                    "summary": {"maxLength": 20000, "title": "Summary", "type": "string"},
                    "test_cases": {
                        "items": {
                            "properties": {
                                "id": {
                                    "maxLength": 200,
                                    "minLength": 1,
                                    "title": "Id",
                                    "type": "string",
                                },
                                "title": {
                                    "maxLength": 20000,
                                    "minLength": 1,
                                    "title": "Title",
                                    "type": "string",
                                },
                                "category": {
                                    "enum": ["positive", "negative", "edge"],
                                    "title": "Category",
                                    "type": "string",
                                },
                                "priority": {
                                    "anyOf": [
                                        {"enum": ["P0", "P1", "P2"], "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                    "title": "Priority",
                                },
                                "steps": {
                                    "items": {"maxLength": 4000, "type": "string"},
                                    "maxItems": 500,
                                    "title": "Steps",
                                    "type": "array",
                                },
                                "expected": {
                                    "maxLength": 20000,
                                    "title": "Expected",
                                    "type": "string",
                                },
                                "actual": {"maxLength": 20000, "title": "Actual", "type": "string"},
                                "status": {
                                    "enum": ["pass", "fail", "blocked", "skipped"],
                                    "title": "Status",
                                    "type": "string",
                                },
                                "video_chapter": {
                                    "anyOf": [
                                        {"maxLength": 200, "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                    "title": "Video Chapter",
                                },
                                "video_path": {
                                    "anyOf": [
                                        {"maxLength": 2000, "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                    "title": "Video Path",
                                },
                                "screenshot_paths": {
                                    "items": {"maxLength": 2000, "type": "string"},
                                    "maxItems": 20,
                                    "title": "Screenshot Paths",
                                    "type": "array",
                                },
                            },
                            "required": ["id", "title", "category", "expected", "actual", "status"],
                            "title": "TestCase",
                            "type": "object",
                        },
                        "maxItems": 500,
                        "title": "Test Cases",
                        "type": "array",
                    },
                    "issues": {
                        "items": {
                            "properties": {
                                "id": {
                                    "maxLength": 200,
                                    "minLength": 1,
                                    "title": "Id",
                                    "type": "string",
                                },
                                "severity": {
                                    "enum": ["Blocker", "Major", "Minor"],
                                    "title": "Severity",
                                    "type": "string",
                                },
                                "title": {
                                    "maxLength": 20000,
                                    "minLength": 1,
                                    "title": "Title",
                                    "type": "string",
                                },
                                "test_case_id": {
                                    "default": "",
                                    "maxLength": 200,
                                    "title": "Test Case Id",
                                    "type": "string",
                                },
                                "steps": {
                                    "items": {"maxLength": 4000, "type": "string"},
                                    "maxItems": 500,
                                    "title": "Steps",
                                    "type": "array",
                                },
                                "expected": {
                                    "maxLength": 20000,
                                    "title": "Expected",
                                    "type": "string",
                                },
                                "actual": {"maxLength": 20000, "title": "Actual", "type": "string"},
                                "screenshot_paths": {
                                    "items": {"maxLength": 2000, "type": "string"},
                                    "maxItems": 20,
                                    "title": "Screenshot Paths",
                                    "type": "array",
                                },
                                "console_errors": {
                                    "items": {"maxLength": 4000, "type": "string"},
                                    "maxItems": 500,
                                    "title": "Console Errors",
                                    "type": "array",
                                },
                            },
                            "required": ["id", "severity", "title", "expected", "actual"],
                            "title": "Issue",
                            "type": "object",
                        },
                        "maxItems": 500,
                        "title": "Issues",
                        "type": "array",
                    },
                    "console_errors": {
                        "items": {"maxLength": 4000, "type": "string"},
                        "maxItems": 500,
                        "title": "Console Errors",
                        "type": "array",
                    },
                },
                "required": ["title", "url", "summary"],
                "title": "TestReport",
                "type": "object",
            },
            "video_path": {
                "type": ["string", "null"],
                "description": "The recording's .webm path in the output dir (list_artifacts finds it).",
            },
            "include_videos": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Embed the recordings in the HTML report. Off by default: one "
                    "recording usually exceeds the result size budget."
                ),
            },
        },
        "required": ["report"],
    }
)
async def publish_test_report(
    report: dict,
    video_path: str | None = None,
    include_videos: bool = False,
    ctx: Any = None,
) -> Any:
    try:
        parsed = TestReport.model_validate(report)
    except ValidationError as exc:
        return {"error": format_validation_error(exc)}
    options = {
        "video_path": video_path,
        "include_videos": include_videos,
        # Fits the whole result into the server's outbound budget by default.
        "max_bytes": config.report_max_bytes(server.budget.max_result_chars),
        # Only this run's own screenshots and recordings may be embedded.
        "allowed_paths": browser.allowed_paths(ctx),
    }
    try:
        summary, resources = build_report(parsed, options)
    except ArtifactError as exc:
        server.logger.warning("test_report_refused", reason=str(exc)[:200])
        return {"error": str(exc)}
    return CallToolResult(content=[_json_text(summary), *resources])


# Last, so the gateway knows which names are local and never relays them.
browser = gateway.install(server)
