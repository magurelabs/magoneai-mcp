"""The test report: HTML escaping and media, PDF and CSV content, the size budget,
and publish_test_report's three embedded resources over a real MCP client."""

import base64
import csv
import io
import json
import re

import pytest
from mcp import Client
from mcp.types import BlobResourceContents, EmbeddedResource

from connectors.playwright import config
from connectors.playwright.paths import ArtifactError
from connectors.playwright.report_build import build_pdf
from connectors.playwright.report_csv import render_csv, safe_cell
from connectors.playwright.report_media import collect, recording_names
from connectors.playwright.report_model import TestReport, report_json_schema
from connectors.playwright.report_render import case_video_paths, render_report, report_paths

from .conftest import module
from .fakes import case, issue, png, report, write

LIMIT = 10 * 1024 * 1024
PdfReader = pytest.importorskip("pypdf").PdfReader


def _media(root, payload: dict, video: str | None = None):
    parsed = TestReport.model_validate(payload)
    return parsed, collect(report_paths(parsed), video, root, case_video_paths(parsed))


def _html(root, payload: dict, video: str | None = None, limit: int = LIMIT):
    parsed, media = _media(root, payload, video)
    return render_report(parsed, media, limit).decode("utf-8"), media


def _pdf(root, payload: dict, video: str | None = None, limit: int = LIMIT) -> bytes:
    parsed, media = _media(root, payload, video)
    return build_pdf(parsed, media, limit)


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


# -- HTML ------------------------------------------------------------------


def test_untrusted_text_is_escaped(root):
    evil = "<script>alert(1)</script>"
    payload = report(
        title=evil,
        url="javascript:alert(1)",
        test_cases=[case("TC-1", actual=evil, title='"><img src=x onerror=alert(1)>')],
        issues=[issue("BUG-1", "Major", title=evil, console_errors=[evil])],
        console_errors=[evil],
    )
    html, _ = _html(root, payload)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert 'href="javascript:' not in html
    assert "javascript:alert(1)" in html  # shown as text only


def test_http_url_is_linked(root):
    html, _ = _html(root, report())
    assert 'href="https://shop.example.com/checkout"' in html


def test_issues_grouped_blocker_major_minor(root):
    html, _ = _html(root, report())
    order = [html.index(f"Issue {i}") for i in ("BUG-2", "BUG-3", "BUG-1")]
    assert order == sorted(order)


def test_summary_counts(root):
    html, _ = _html(root, report())
    pairs = re.findall(r'<div class="n">(\d+)</div><div class="l">(\w+)</div>', html)
    assert {label: int(n) for n, label in pairs} == {
        "Passed": 1,
        "Failed": 1,
        "Blocked": 1,
        "Skipped": 0,
        "Blocker": 1,
        "Major": 1,
        "Minor": 1,
    }


def test_screenshot_embedded_and_missing_one_shown(root):
    write(root / "page-1.png", 64)
    payload = report(issues=[issue("BUG-1", "Major", screenshot_paths=["page-1.png", "gone.png"])])
    html, _ = _html(root, payload)
    expected = base64.b64encode((root / "page-1.png").read_bytes()).decode()
    assert f'src="data:image/png;base64,{expected}"' in html
    assert "Screenshot unavailable: gone.png" in html


def test_screenshot_outside_root_is_unavailable(root, tmp_path):
    write(tmp_path / "secret.png", 64)
    shots = [str(tmp_path / "secret.png")]
    html, _ = _html(root, report(issues=[issue("BUG-1", "Major", screenshot_paths=shots)]))
    assert "Screenshot unavailable: secret.png" in html and "base64" not in html


def test_video_uses_publish_artifact_path_rules(root):
    write(root / "video-2026-10-15T13-46-38-154Z.webm", 64)
    link = "- [Video](../../artifacts/video-2026-10-15T13-46-38-154Z.webm)"
    html, media = _html(root, report(), video=link)
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in html
    assert media.embedded_paths() == [root / "video-2026-10-15T13-46-38-154Z.webm"]


def test_no_video_and_missing_video_notes(root):
    assert "No recording was attached" in _html(root, report())[0]
    assert "could not be found" in _html(root, report(), video="nope.webm")[0]


def test_excluded_videos_are_named_not_embedded_and_not_dropped(root):
    write(root / "video.webm", 64)
    parsed, media = _media(root, report(), "video.webm")
    media.exclude_videos()
    html = render_report(parsed, media, LIMIT).decode()
    assert "<video" not in html
    assert "The recording (video.webm) is not embedded in this report." in html
    assert media.dropped == [] and media.embedded_paths() == []


def test_budget_drops_largest_screenshot_first_then_video(root):
    payload = report(issues=[issue("BUG-1", "Major", screenshot_paths=["small.png", "large.png"])])
    skeleton, _ = _html(root, payload)  # before the files exist: no media at all
    write(root / "small.png", 2_000)
    write(root / "large.png", 40_000)
    write(root / "video.webm", 30_000)
    html, media = _html(root, payload, video="video.webm", limit=len(skeleton) + 50_000)
    assert media.dropped == ["large.png"]
    assert "Left out to stay within the size limit: large.png" in html
    assert "data:video/webm" in html and len(html.encode()) <= len(skeleton) + 50_000

    html, media = _html(root, payload, video="video.webm", limit=len(skeleton) + 41_000)
    assert media.dropped == ["large.png", "small.png"]
    assert "data:video/webm" in html

    html, media = _html(root, payload, video="video.webm", limit=len(skeleton) + 1_000)
    assert media.dropped == ["large.png", "small.png", "video.webm"]
    assert "was left out to stay within the size limit" in html
    assert media.embedded_paths() == []


def test_report_too_large_even_without_media_errors(root):
    with pytest.raises(ArtifactError, match="even without media"):
        _html(root, report(), limit=1_000)


# -- per-case recordings -----------------------------------------------------


def _case_payload(**extra):
    cases = [
        case("TC-1", video_path="../../artifacts/tc1.webm"),
        case("TC-2", "fail", video_path="[Video](tc2.webm)"),
        case("TC-3", "blocked"),
    ]
    return report(test_cases=cases, issues=[issue("BUG-1", "Major", test_case_id="TC-2")], **extra)


def _row(html: str, index: int) -> str:
    start = html.index(f'id="case-{index}"')
    end = html.find('<details class="row"', start + 1)
    return html[start : end if end != -1 else len(html)]


def test_case_videos_embed_in_their_rows_and_issue_links(root):
    write(root / "tc1.webm", 50)
    write(root / "tc2.webm", 60)
    html, _ = _html(root, _case_payload())
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in _row(html, 0)
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in _row(html, 1)
    assert "<video" not in _row(html, 2)
    assert '<a class="rec-link" href="#case-1">Recording</a>' in html
    assert "Full run recording" not in html and "Screen recording" not in html


def test_budget_drops_shots_then_largest_case_video_then_run_video(root):
    payload = _case_payload()
    payload["test_cases"][2]["screenshot_paths"] = ["shot.png"]
    skeleton, _ = _html(root, payload)
    write(root / "shot.png", 1_000)
    write(root / "tc1.webm", 10_000)
    write(root / "tc2.webm", 20_000)
    write(root / "run.webm", 30_000)
    base = len(skeleton.encode())

    _, media = _html(root, payload, "run.webm", base + 4 * 30_000 // 3 + 4 * 10_000 // 3 + 500)
    assert media.dropped == ["shot.png", "tc2.webm"]
    _, media = _html(root, payload, "run.webm", base + 4 * 30_000 // 3 + 500)
    assert media.dropped == ["shot.png", "tc2.webm", "tc1.webm"]
    html, media = _html(root, payload, "run.webm", base + 1_000)
    assert media.dropped == ["shot.png", "tc2.webm", "tc1.webm", "run.webm"]
    assert re.search(r"Left out to stay within the size limit: shot.png, tc2.webm", html)


# -- PDF -----------------------------------------------------------------------


def test_renders_pdf_with_issue_titles_in_severity_order(root):
    (root / "video-1.webm").write_bytes(b"webm")
    pdf = _pdf(root, report(), video="[Video](../../artifacts/video-1.webm)")
    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    for title in ("Checkout QA", "Issue BUG-1", "Issue BUG-2", "Issue BUG-3", "Case TC-2"):
        assert title in text
    assert text.index("Issue BUG-2") < text.index("Issue BUG-3") < text.index("Issue BUG-1")
    assert "Screen recording: video-1.webm (not embedded in the PDF)" in text
    assert "Screen recording: not recorded" in _text(_pdf(root, report()))


def test_unicode_arabic_cjk_emoji_and_controls_do_not_raise(root):
    weird = "مرحبا بالعالم 你好 😀 \x00\x07 tab\tend <script>"
    payload = report(
        title=weird,
        summary=weird,
        test_cases=[case("TC-1", actual=weird, title=weird)],
        issues=[issue("BUG-1", "Blocker", title=weird, console_errors=[weird])],
        console_errors=[weird],
    )
    assert _pdf(root, payload).startswith(b"%PDF")


def test_screenshot_is_drawn_and_a_missing_or_broken_one_tolerated(root):
    (root / "page-1.png").write_bytes(png())
    (root / "broken.png").write_bytes(b"\x89PNG not really")
    shots = ["page-1.png", "gone.png", "broken.png"]
    with_shot = _pdf(root, report(issues=[issue("BUG-1", "Major", screenshot_paths=shots)]))
    without = _pdf(root, report(issues=[issue("BUG-1", "Major", screenshot_paths=["gone.png"])]))
    assert b"/Subtype /Image" in with_shot
    assert b"/Subtype /Image" not in without


def test_over_limit_pdf_names_screenshots_instead(root):
    (root / "page-1.png").write_bytes(png(400, 400))
    payload = report(issues=[issue("BUG-1", "Major", screenshot_paths=["page-1.png"])])
    full = _pdf(root, payload)
    assert b"/Subtype /Image" not in _pdf(root, payload, limit=len(full) - 1)


def test_pdf_and_csv_name_each_case_recording(root):
    for name in ("tc1.webm", "tc2.webm", "run.webm"):
        write(root / name, 50)
    parsed, media = _media(root, _case_payload(), "run.webm")
    text = _text(build_pdf(parsed, media, LIMIT))
    assert "tc1.webm" in text and "tc2.webm" in text and "not recorded" in text
    rows = list(csv.DictReader(io.StringIO(render_csv(parsed, recording_names(media)).decode())))
    assert [r["recording"] for r in rows] == ["tc1.webm", "tc2.webm", "run.webm"]


# -- CSV -----------------------------------------------------------------------


@pytest.mark.parametrize("cell", ["=1+1", "+cmd", "-2", "@SUM(A1)", "\tx", "\rx"])
def test_csv_cells_that_would_run_as_formulas_stay_text(cell):
    assert safe_cell(cell) == f"'{cell}"


def test_csv_has_a_bom_columns_and_guarded_cells():
    payload = report(test_cases=[case("TC-1", title="=HYPERLINK(1)", actual="مرحبا")])
    data = render_csv(TestReport.model_validate(payload))
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0][:4] == ["id", "category", "priority", "title"]
    assert rows[1][3] == "'=HYPERLINK(1)" and rows[1][6] == "مرحبا"


# -- the tool ------------------------------------------------------------------


async def publish(arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool("publish_test_report", arguments)


def resources(result) -> dict[str, BlobResourceContents]:
    out = {}
    for block in result.content[1:]:
        assert isinstance(block, EmbeddedResource) and block.type == "resource"
        assert isinstance(block.resource, BlobResourceContents)
        out[str(block.resource.uri)] = block.resource
    return out


async def test_three_embedded_resources_and_a_json_summary(root):
    shot = write(root / "page-1.png", 100)
    video = write(root / "video-1.webm", 100)
    payload = report(issues=[issue("BUG-1", "Major", screenshot_paths=["page-1.png"])])

    result = await publish({"report": payload, "video_path": "../../artifacts/video-1.webm"})

    assert result.is_error is False
    assert [block.type for block in result.content] == ["text", "resource", "resource", "resource"]
    files = resources(result)
    stamp = "20261015-134638"
    assert {uri: blob.mime_type for uri, blob in files.items()} == {
        f"artifact://test-report-{stamp}.html": "text/html",
        f"artifact://test-report-{stamp}.pdf": "application/pdf",
        f"artifact://test-cases-{stamp}.csv": "text/csv",
    }
    html = base64.b64decode(files[f"artifact://test-report-{stamp}.html"].blob).decode()
    assert "<!doctype html>" in html and "data:image/png;base64," in html
    assert "<video" not in html and "is not embedded in this report" in html
    assert base64.b64decode(files[f"artifact://test-report-{stamp}.pdf"].blob).startswith(b"%PDF")
    rows = base64.b64decode(files[f"artifact://test-cases-{stamp}.csv"].blob).decode("utf-8-sig")
    assert len(list(csv.reader(io.StringIO(rows)))) == 4

    summary = json.loads(result.content[0].text)
    assert [f["filename"] for f in summary["files"]] == [
        f"test-report-{stamp}.html",
        f"test-report-{stamp}.pdf",
        f"test-cases-{stamp}.csv",
    ]
    assert summary["counts"]["test_cases"] == 3 and summary["counts"]["issues"] == 1
    assert summary["counts"]["by_status"] == {"pass": 1, "fail": 1, "blocked": 1, "skipped": 0}
    assert summary["videos_embedded"] is False and summary["dropped"] == []
    assert summary["recordings_not_embedded"] == ["video-1.webm"]
    # The screenshot went into the report; the recording stays for publish_artifact.
    assert not shot.exists() and video.exists()


async def test_include_videos_embeds_and_then_removes_the_recording(root):
    video = write(root / "video-1.webm", 100)
    result = await publish(
        {"report": report(), "video_path": "video-1.webm", "include_videos": True}
    )
    html = base64.b64decode(next(iter(resources(result).values())).blob).decode()
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in html
    summary = json.loads(result.content[0].text)
    assert summary["videos_embedded"] is True and summary["recordings_not_embedded"] == []
    assert not video.exists()


async def test_keeps_files_when_delete_disabled(root, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DELETE_AFTER_PUBLISH", "off")
    video = write(root / "video-1.webm", 100)
    arguments = {"report": report(), "video_path": "video-1.webm", "include_videos": True}
    result = await publish(arguments)
    assert json.loads(result.content[0].text)["deleted_local_copies"] == 0
    assert video.exists()


def test_the_default_budget_fits_the_servers_result_budget():
    chars = module.server.budget.max_result_chars
    limit = config.report_max_bytes(chars)
    assert 4 * limit // 3 + config.REPORT_TEXT_MARGIN <= chars


def test_artifacts_max_bytes_can_only_lower_the_report_budget(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "1000")
    assert config.report_max_bytes(1_000_000) == 1000
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", str(50 * 1024 * 1024))
    assert config.report_max_bytes(1_000_000) == (1_000_000 - config.REPORT_TEXT_MARGIN) * 3 // 4


async def test_the_whole_result_fits_the_budget_and_says_what_it_dropped(root, monkeypatch):
    """Over budget, screenshots go first, then the PDF's drawings; the call still succeeds."""
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "120000")
    write(root / "small.png", 2_000)
    write(root / "large.png", 400_000)
    shots = ["small.png", "large.png"]
    result = await publish(
        {"report": report(issues=[issue("BUG-1", "Major", screenshot_paths=shots)])}
    )
    assert result.is_error is False
    summary = json.loads(result.content[0].text)
    assert summary["dropped"] == ["large.png"]
    assert sum(f["size_bytes"] for f in summary["files"]) <= 120_000
    assert (root / "large.png").exists() and not (root / "small.png").exists()


async def test_a_pdf_that_does_not_fit_is_reported_not_fatal(root, monkeypatch):
    parsed, media = _media(root, report())
    html_alone = len(render_report(parsed, media, LIMIT)) + len(render_csv(parsed))
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", str(html_alone + 100))
    result = await publish({"report": report()})
    summary = json.loads(result.content[0].text)
    assert result.is_error is False and "pdf_error" in summary
    assert [f["mime_type"] for f in summary["files"]] == ["text/html", "text/csv"]


async def test_a_report_too_large_to_fit_is_an_error(root, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "1000")
    result = await publish({"report": report()})
    assert result.is_error is True and "even without media" in result.content[0].text


async def test_malformed_report_is_a_clear_error(root):
    bad = report(test_cases=[{"id": "TC-1", "title": "t", "category": "happy"}])
    result = await publish({"report": bad})
    text = json.loads(result.content[0].text)["error"]
    assert result.is_error is True and text.startswith("Invalid report.")
    assert "test_cases.0.category" in text and "test_cases.0.status" in text


async def test_tested_at_defaults_to_now(root):
    payload = report()
    payload.pop("tested_at")
    result = await publish({"report": payload})
    assert json.loads(result.content[0].text)["files"][0]["filename"].startswith("test-report-20")


def test_the_declared_report_schema_is_the_model_schema():
    """The schema is written out as a literal for the golden gate; it must not drift."""
    declared = module.server.mcp._tool_manager._tools["publish_test_report"].parameters
    report_schema = dict(declared["properties"]["report"])
    assert report_schema.pop("description")
    assert report_schema == report_json_schema()
    assert declared["properties"]["include_videos"]["default"] is False
