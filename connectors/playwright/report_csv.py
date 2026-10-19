"""The test cases as CSV, returned beside the HTML and PDF reports.

Cells are page text, so a cell starting with = + - @, a tab or a carriage
return is prefixed with a single quote: opened in a spreadsheet it stays text
instead of running as a formula (CSV injection). UTF-8 with a BOM so Excel
reads Arabic and other non-Latin text correctly.
"""

from __future__ import annotations

import csv
import io
from datetime import timezone

from .report_model import TestReport

COLUMNS = (
    "id",
    "category",
    "priority",
    "title",
    "steps",
    "expected",
    "actual",
    "status",
    "video_chapter",
    "page_url",
    "tested_at",
    "recording",
)
STEP_SEPARATOR = " | "
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def render_csv(report: TestReport, recordings: list[str] | None = None) -> bytes:
    """One row per test case; ``recordings`` holds each case's recording file name."""
    stamp = report.tested_at
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    tested_at = stamp.astimezone(timezone.utc).isoformat()
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    recordings = recordings or []
    for index, case in enumerate(report.test_cases):
        row = (
            case.id,
            case.category,
            case.priority,
            case.title,
            STEP_SEPARATOR.join(case.steps),
            case.expected,
            case.actual,
            case.status,
            case.video_chapter,
            report.url,
            tested_at,
            recordings[index] if index < len(recordings) else "",
        )
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")
