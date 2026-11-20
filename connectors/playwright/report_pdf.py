"""Render a TestReport as a PDF, for readers and document stores that take no HTML.

Same content as the HTML report except the recording, which a PDF cannot hold:
it is named instead. Text is arbitrary page content (any script, emoji), so it
is drawn with the bundled DejaVu Sans; fpdf2 skips a glyph the font lacks with
a warning rather than raising. Text is never interpreted as markup.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from fpdf import FPDF, XPos, YPos

from .report_media import MediaSet
from .report_model import TestReport
from .report_render import SEVERITY_ORDER, STATUS_ORDER, format_when

# fontTools logs every font subset at INFO; one line per table is noise in production.
logging.getLogger("fontTools").setLevel(logging.WARNING)

FONT_DIR = Path(__file__).parent / "fonts"
FONT = "DejaVu"
SHOT_WIDTH_MM = 120
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_STATUS = {"pass": "Passed", "fail": "Failed", "blocked": "Blocked", "skipped": "Skipped"}
_TONE = {
    "Passed": (26, 127, 69),
    "Failed": (180, 35, 24),
    "Blocked": (161, 92, 0),
    "Skipped": (93, 102, 115),
    "Blocker": (180, 35, 24),
    "Major": (181, 71, 8),
    "Minor": (105, 65, 198),
}
_MUTED = (93, 102, 115)
_TEXT = (27, 31, 36)


def clean(value: object) -> str:
    """Control characters dropped; tabs become spaces."""
    return _CONTROL.sub("", str(value).replace("\t", " "))


class _Doc(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4")
        self.add_font(FONT, "", str(FONT_DIR / "DejaVuSans.ttf"))
        self.add_font(FONT, "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(15, 15, 15)
        self.add_page()

    def text_block(self, text: str, size: float = 10, style: str = "", rgb=_TEXT) -> None:
        self.set_font(FONT, style, size)
        self.set_text_color(*rgb)
        self.multi_cell(0, size * 0.5, clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def heading(self, text: str) -> None:
        self.ln(4)
        self.text_block(text, 14, "B")
        self.ln(1)

    def field(self, label: str, value: str) -> None:
        self.text_block(label, 8, "B", _MUTED)
        self.text_block(value or "-", 10)
        self.ln(1)

    def steps(self, steps: list[str]) -> None:
        lines = [f"{i}. {step}" for i, step in enumerate(steps, 1)] or ["None given"]
        self.field("Steps", "\n".join(lines))

    def shot(self, label: str, data: bytes | None) -> None:
        if data is not None:
            try:
                self.image(io.BytesIO(data), w=SHOT_WIDTH_MM)
                self.ln(2)
                return
            except Exception:  # noqa: BLE001, S110 - a bad image must not fail the report
                pass
        self.text_block(f"Screenshot unavailable: {label}", 9, "", _MUTED)


def _counts(doc: _Doc, report: TestReport) -> None:
    doc.heading("Summary")
    statuses = [(_STATUS[s], sum(c.status == s for c in report.test_cases)) for s in STATUS_ORDER]
    sevs = [(s, sum(i.severity == s for i in report.issues)) for s in SEVERITY_ORDER]
    for title, items in (("Test cases", statuses), ("Issues", sevs)):
        doc.text_block(f"{title} ({sum(n for _, n in items)})", 9, "B", _MUTED)
        doc.set_font(FONT, "B", 11)
        for label, n in items:
            doc.set_text_color(*_TONE[label])
            doc.cell(36, 7, f"{label}: {n}")
        doc.ln(9)


def _shots(doc: _Doc, paths: list[str], loader) -> None:
    if paths:
        doc.text_block("Screenshots", 8, "B", _MUTED)
        for raw in paths:
            doc.shot(*loader(raw))


def _issues(doc: _Doc, report: TestReport, loader) -> None:
    doc.heading("Issues")
    if not report.issues:
        doc.text_block("No issues were found.", 10, "", _MUTED)
    for sev in SEVERITY_ORDER:
        for issue in (i for i in report.issues if i.severity == sev):
            doc.text_block(f"[{sev}] {issue.id}  {issue.title}", 11, "B", _TONE[sev])
            if issue.test_case_id:
                doc.field("Test case", issue.test_case_id)
            doc.steps(issue.steps)
            doc.field("Expected", issue.expected)
            doc.field("Actual", issue.actual)
            if issue.console_errors:
                doc.field("Console errors", "\n".join(issue.console_errors))
            _shots(doc, issue.screenshot_paths, loader)
            doc.ln(3)


def _case_recording(media: MediaSet, index: int) -> str:
    videos = media.case_videos
    video = videos[index] if index < len(videos) else None
    return video.path.name if video else "not recorded"


def _cases(doc: _Doc, report: TestReport, loader, media: MediaSet) -> None:
    doc.heading("Test cases")
    if not report.test_cases:
        doc.text_block("No test cases were recorded.", 10, "", _MUTED)
        return
    doc.set_font(FONT, "", 9)
    doc.set_text_color(*_TEXT)
    with doc.table(col_widths=(22, 22, 86, 22, 28), text_align="LEFT") as table:
        table.row(["ID", "Category", "Title", "Status", "Video"])
        for c in report.test_cases:
            ident = f"{c.id} ({c.priority})" if c.priority else c.id
            row = [ident, c.category, c.title, _STATUS[c.status], c.video_chapter or ""]
            table.row([clean(cell) for cell in row])
    for index, c in enumerate(report.test_cases):
        doc.ln(2)
        doc.text_block(f"{c.id}  {c.title} ({_STATUS[c.status]})", 10, "B")
        doc.steps(c.steps)
        doc.field("Expected", c.expected)
        doc.field("Actual", c.actual)
        doc.field("Recording", _case_recording(media, index))
        _shots(doc, c.screenshot_paths, loader)


def render_pdf(report: TestReport, media: MediaSet, with_shots: bool = True) -> bytes:
    """The PDF bytes. ``with_shots=False`` lists screenshots by name only."""

    def loader(raw: str) -> tuple[str, bytes | None]:
        item = media.shots.get(raw)
        label = item.path.name if item else Path(raw).name or "screenshot"
        if item is None or not with_shots:
            return label, None
        try:
            return label, item.path.read_bytes()
        except OSError:
            return label, None

    doc = _Doc()
    doc.text_block(report.title, 18, "B")
    doc.text_block(f"URL: {report.url}", 9, "", _MUTED)
    meta = f"Tested: {format_when(report.tested_at)}"
    doc.text_block(
        meta + (f"   Environment: {report.environment}" if report.environment else ""),
        9,
        "",
        _MUTED,
    )
    video = media.video.path.name if media.video else None
    doc.text_block(
        f"Screen recording: {video} (not embedded in the PDF)"
        if video
        else "Screen recording: not recorded",
        9,
        "",
        _MUTED,
    )
    if not with_shots:
        doc.text_block("Screenshots were left out to stay within the size limit.", 9, "", _MUTED)
    if report.summary:
        doc.ln(2)
        doc.text_block(report.summary)
    _counts(doc, report)
    _issues(doc, report, loader)
    _cases(doc, report, loader, media)
    if report.console_errors:
        doc.heading("Console errors")
        doc.text_block("\n".join(report.console_errors), 9)
    return bytes(doc.output())
