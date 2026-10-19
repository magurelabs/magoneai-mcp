"""The test report an agent hands to publish_test_report, validated with Pydantic.

Every string here came from a web page the agent tested, so it is untrusted:
the template escapes all of it and nothing is rendered as markup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_TEXT = 20_000
MAX_ITEMS = 500
MAX_PATHS = 20


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class TestCase(_Model):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=MAX_TEXT)
    category: Literal["positive", "negative", "edge"]
    priority: Literal["P0", "P1", "P2"] | None = None
    steps: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    expected: str = Field(max_length=MAX_TEXT)
    actual: str = Field(max_length=MAX_TEXT)
    status: Literal["pass", "fail", "blocked", "skipped"]
    video_chapter: str | None = Field(default=None, max_length=200)
    video_path: str | None = Field(default=None, max_length=2000)
    screenshot_paths: list[str] = Field(default_factory=list, max_length=MAX_PATHS)


class Issue(_Model):
    id: str = Field(min_length=1, max_length=200)
    severity: Literal["Blocker", "Major", "Minor"]
    title: str = Field(min_length=1, max_length=MAX_TEXT)
    test_case_id: str = Field(default="", max_length=200)
    steps: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    expected: str = Field(max_length=MAX_TEXT)
    actual: str = Field(max_length=MAX_TEXT)
    screenshot_paths: list[str] = Field(default_factory=list, max_length=MAX_PATHS)
    console_errors: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)


class TestReport(_Model):
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(max_length=4000)
    tested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str | None = Field(default=None, max_length=500)
    summary: str = Field(max_length=MAX_TEXT)
    test_cases: list[TestCase] = Field(default_factory=list, max_length=MAX_ITEMS)
    issues: list[Issue] = Field(default_factory=list, max_length=MAX_ITEMS)
    console_errors: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)


# pytest would otherwise try to collect these as test classes.
TestCase.__test__ = False
TestReport.__test__ = False


def _inline(node: Any, defs: dict) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            return _inline(defs[ref.rsplit("/", 1)[-1]], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    return node


def report_json_schema() -> dict:
    """The report schema with ``$ref``s inlined; some LLM tool APIs reject ``$defs``."""
    schema = TestReport.model_json_schema()
    return _inline(schema, schema.get("$defs", {}))


def format_validation_error(exc: Exception) -> str:
    """One readable line per problem: ``test_cases.0.status: Input should be ...``."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return f"Invalid report: {exc}"
    lines = [
        f"{'.'.join(str(p) for p in err.get('loc', ())) or 'report'}: {err.get('msg')}"
        for err in errors()[:10]
    ]
    return "Invalid report. " + "; ".join(lines)
