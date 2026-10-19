"""Build publish_test_report's result: HTML, PDF and CSV as embedded resources.

Everything comes back in ONE tool result, so the three files share one size
budget, counted before base64: what fits the server's result budget
(``config.report_max_bytes``: about 735 KB under the default
``MCP_MAX_RESULT_CHARS`` of 1,000,000), never more than ``ARTIFACTS_MAX_BYTES``.
Recordings are left out unless the caller asks (``include_videos``), because
one recording alone usually exceeds that budget: a report with per-case
videos measured about 10 MB. A recording is returned on its own with
publish_artifact, one file per call, under ``ARTIFACTS_MAX_BYTES``.

The files are fitted in this order, because the HTML is the report and the
other two are copies of it:

1. the CSV, which is small and holds no media;
2. the HTML, given everything except the CSV and the smallest PDF (the PDF
   with screenshots named rather than drawn). Media that does not fit is left
   out (screenshots first, then per-case recordings, then the full run) and
   listed in the report and in the result's ``dropped``;
3. the PDF, with screenshots if what is left allows, else names only, else
   none: a PDF that cannot be rendered or does not fit is reported in the
   result (``pdf_error``) and never fails the call.

Only a report too large to fit even without media fails the call.
"""

from __future__ import annotations

from collections import Counter
from datetime import timezone

import structlog

from . import config
from .files import describe, embedded, remove_local
from .paths import ArtifactError, relative_to_root
from .report_csv import render_csv
from .report_media import MediaSet, collect, recording_names
from .report_model import TestReport
from .report_pdf import render_pdf
from .report_render import (
    SEVERITY_ORDER,
    STATUS_ORDER,
    case_video_paths,
    render_report,
    report_paths,
)

HTML = "text/html"
PDF = "application/pdf"
CSV = "text/csv"
logger = structlog.get_logger("mcp.playwright.report")


def report_stamp(report: TestReport) -> str:
    stamp = report.tested_at
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return f"{stamp.astimezone(timezone.utc):%Y%m%d-%H%M%S}"


def report_filename(report: TestReport, extension: str) -> str:
    return f"test-report-{report_stamp(report)}.{extension}"


def cases_filename(report: TestReport) -> str:
    return f"test-cases-{report_stamp(report)}.csv"


def build_pdf(report: TestReport, media: MediaSet, limit: int) -> bytes:
    """The PDF within ``limit``: with screenshots if they fit, else names only."""
    pdf = render_pdf(report, media, with_shots=True)
    if len(pdf) > limit:
        pdf = render_pdf(report, media, with_shots=False)
    if len(pdf) > limit:
        raise ValueError(f"The PDF is {len(pdf):,} bytes, over the {limit:,} byte limit.")
    return pdf


def _small_pdf(report: TestReport, media: MediaSet) -> tuple[bytes | None, str]:
    """The PDF with screenshots named only: the least the PDF can cost."""
    try:
        return render_pdf(report, media, with_shots=False), ""
    except Exception as exc:  # noqa: BLE001 - the PDF is an extra, never fatal
        logger.warning("test_report_pdf_failed", error=type(exc).__name__)
        return None, f"The PDF could not be rendered ({type(exc).__name__})."


def _final_pdf(report: TestReport, media: MediaSet, room: int, small: bytes) -> bytes | None:
    """The best PDF that fits ``room``: with screenshots, else ``small``, else None."""
    try:
        full = render_pdf(report, media, with_shots=True)
    except Exception as exc:  # noqa: BLE001 - fall back to the PDF already rendered
        logger.warning("test_report_pdf_shots_failed", error=type(exc).__name__)
        full = small
    for candidate in (full, small):
        if len(candidate) <= room:
            return candidate
    return None


def counts(report: TestReport) -> dict:
    statuses = Counter(case.status for case in report.test_cases)
    severities = Counter(issue.severity for issue in report.issues)
    return {
        "test_cases": len(report.test_cases),
        "by_status": {s: statuses.get(s, 0) for s in STATUS_ORDER},
        "issues": len(report.issues),
        "by_severity": {s: severities.get(s, 0) for s in SEVERITY_ORDER},
    }


def build_report(report: TestReport, options: dict) -> tuple[dict, list]:
    """``(summary, resources)``.

    ``options``: ``video_path``, ``include_videos`` (default False) and
    ``max_bytes``, the file bytes the result may carry. Raises ArtifactError
    when the HTML cannot fit even without media.
    """
    limit = int(options["max_bytes"])
    root = config.artifacts_root()
    media = collect(report_paths(report), options.get("video_path"), root, case_video_paths(report))
    if not options.get("include_videos", False):
        media.exclude_videos()

    csv = render_csv(report, recording_names(media))
    small_pdf, pdf_error = _small_pdf(report, media)
    try:
        html = render_report(report, media, limit - len(csv) - len(small_pdf or b""))
    except ArtifactError:
        # Not even the bare HTML fits beside a PDF: give the PDF's room to the
        # HTML. A failure here is the only one that fails the call.
        html = render_report(report, media, limit - len(csv))
    pdf = None
    if small_pdf is not None:
        pdf = _final_pdf(report, media, limit - len(csv) - len(html), small_pdf)
        if pdf is None:
            pdf_error = "The PDF did not fit in what was left of the result budget."

    files = [(report_filename(report, "html"), html, HTML)]
    if pdf is not None:
        files.append((report_filename(report, "pdf"), pdf, PDF))
    files.append((cases_filename(report), csv, CSV))

    embedded_paths = media.embedded_paths()
    deleted = sum(remove_local(p) for p in embedded_paths) if config.delete_after_publish() else 0
    summary = {
        "files": [describe(name, content, mime) for name, content, mime in files],
        "counts": counts(report),
        "videos_embedded": not media.videos_excluded,
        "dropped": list(media.dropped),
        "deleted_local_copies": deleted,
        # For publish_artifact, one file per call: every recording left out.
        "recordings_not_embedded": [
            relative_to_root(m.path, root) for m in media.unique_videos() if not m.kept
        ],
    }
    if pdf_error:
        summary["pdf_error"] = pdf_error
    logger.info(
        "test_report_built",
        files=len(files),
        total_bytes=sum(len(content) for _, content, _ in files),
        dropped=len(media.dropped),
        deleted=deleted,
    )
    return summary, [embedded(name, content, mime) for name, content, mime in files]
