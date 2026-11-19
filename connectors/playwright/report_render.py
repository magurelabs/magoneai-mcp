"""Render a TestReport into one self-contained HTML file.

Jinja2 runs with autoescape forced ON and the template uses no ``|safe``, so
every report value (all of it text from a page under test) is escaped. The
only URL turned into a link is the report's own ``url``, and only when it is
http or https; media appear as base64 data URIs built here, never from input.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .paths import ArtifactError, unwrap_path
from .report_media import MediaSet, fit_budget
from .report_model import TestReport

TEMPLATE = "report_template.html.j2"
SEVERITY_ORDER = ("Blocker", "Major", "Minor")
STATUS_ORDER = ("pass", "fail", "blocked", "skipped")

_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent)),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def report_paths(report: TestReport) -> list[str]:
    """Every screenshot path the report references, in order, repeats included."""
    paths = [p for case in report.test_cases for p in case.screenshot_paths]
    return paths + [p for issue in report.issues for p in issue.screenshot_paths]


def format_when(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _href(url: str) -> str | None:
    return url if urlparse(url).scheme.lower() in ("http", "https") else None


class _Viewer:
    """What the template asks for: a screenshot or a recording, as a view dict."""

    def __init__(self, media: MediaSet, limit: int, placeholders: bool) -> None:
        self.media, self.limit, self.placeholders = media, limit, placeholders
        self.cache: dict[Path, str] = {}

    def _uri(self, item) -> str:
        if self.placeholders:
            return ""
        if item.path not in self.cache:
            self.cache[item.path] = item.data_uri(self.limit)
        return self.cache[item.path]

    def shot(self, raw: str) -> dict:
        label = Path(unwrap_path(raw)).name or "screenshot"
        item = self.media.shots.get(raw)
        if item is None:
            return {"label": label, "state": "unavailable", "uri": ""}
        if not item.kept:
            return {"label": label, "state": "omitted", "uri": ""}
        try:
            return {"label": label, "state": "embedded", "uri": self._uri(item)}
        except ArtifactError:
            return {"label": label, "state": "unavailable", "uri": ""}

    def _video(self, item, requested: bool) -> dict:
        if item is None:
            return {"state": "unavailable" if requested else "none"}
        if not item.kept:
            state = "excluded" if self.media.videos_excluded else "omitted"
            return {"state": state, "name": item.path.name}
        try:
            uri = self._uri(item)
        except ArtifactError:
            return {"state": "unavailable"}
        return {"state": "embedded", "uri": uri, "type": item.content_type}

    def video(self) -> dict:
        return self._video(self.media.video, self.media.video_requested)

    def case_video(self, index: int, requested: object) -> dict:
        videos = self.media.case_videos
        return self._video(videos[index] if index < len(videos) else None, bool(requested))


def case_video_paths(report: TestReport) -> list[str | None]:
    """Each test case's own recording path, by position."""
    return [case.video_path for case in report.test_cases]


def _case_anchors(report: TestReport, media: MediaSet) -> dict[str, int]:
    """Test case id -> row index of the first case with that id that has a recording."""
    anchors: dict[str, int] = {}
    for index, case in enumerate(report.test_cases):
        if index < len(media.case_videos) and media.case_videos[index] is not None:
            anchors.setdefault(case.id, index)
    return anchors


def _context(report: TestReport, viewer: _Viewer) -> dict:
    statuses = Counter(case.status for case in report.test_cases)
    severities = Counter(issue.severity for issue in report.issues)
    groups = [
        (sev, [issue for issue in report.issues if issue.severity == sev]) for sev in SEVERITY_ORDER
    ]
    return {
        "r": report,
        "tested_at": format_when(report.tested_at),
        "url_href": _href(report.url),
        "status_counts": [(s, statuses.get(s, 0)) for s in STATUS_ORDER],
        "severity_counts": [(s, severities.get(s, 0)) for s in SEVERITY_ORDER],
        "issue_groups": [(sev, items) for sev, items in groups if items],
        "shot": viewer.shot,
        "video": viewer.video(),
        "case_video": viewer.case_video,
        "case_anchor": _case_anchors(report, viewer.media),
        "has_case_videos": any(case.video_path for case in report.test_cases),
        "dropped": list(viewer.media.dropped),
    }


def _render(report: TestReport, media: MediaSet, limit: int, placeholders: bool) -> bytes:
    viewer = _Viewer(media, limit, placeholders)
    return _ENV.get_template(TEMPLATE).render(_context(report, viewer)).encode("utf-8")


def render_report(report: TestReport, media: MediaSet, limit: int) -> bytes:
    """The HTML bytes, within ``limit``, leaving media out as the budget requires."""
    skeleton = len(_render(report, media, limit, placeholders=True))
    fit_budget(media, skeleton, limit)
    while True:
        html = _render(report, media, limit, placeholders=False)
        if len(html) <= limit:
            return html
        if not media.drop_one():
            raise ArtifactError(
                f"The report is {len(html):,} bytes even without media, over the "
                f"{limit:,} byte limit. Shorten it."
            )
