"""Rendering the HTML report: escaping, order, counts, media and the size budget."""

import base64
import re

import pytest

from connectors.playwright.paths import ArtifactError
from connectors.playwright.report_media import collect
from connectors.playwright.report_model import TestReport
from connectors.playwright.report_render import case_video_paths, render_report, report_paths

from .fakes import case, issue, report, write

LIMIT = 10 * 1024 * 1024


def _render(root, payload: dict, video: str | None = None, limit: int = LIMIT, exclude=False):
    parsed = TestReport.model_validate(payload)
    media = collect(report_paths(parsed), video, root, case_video_paths(parsed))
    if exclude:
        media.exclude_videos()
    return render_report(parsed, media, limit).decode("utf-8"), media


def test_untrusted_text_is_escaped(root):
    evil = "<script>alert(1)</script>"
    payload = report(
        title=evil,
        url="javascript:alert(1)",
        test_cases=[case("TC-1", actual=evil, title='"><img src=x onerror=alert(1)>')],
        issues=[issue("BUG-1", "Major", title=evil, console_errors=[evil])],
        console_errors=[evil],
    )
    html, _ = _render(root, payload)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert 'href="javascript:' not in html
    assert "javascript:alert(1)" in html  # shown as text only


def test_http_url_is_linked(root):
    html, _ = _render(root, report())
    assert 'href="https://shop.example.com/checkout"' in html


def test_the_report_names_no_platform(root):
    html, _ = _render(root, report())
    assert "MagOne" not in html
    assert "Generated from a Playwright browser session." in html


def test_issues_grouped_blocker_major_minor(root):
    html, _ = _render(root, report())
    order = [html.index(f"Issue {i}") for i in ("BUG-2", "BUG-3", "BUG-1")]
    assert order == sorted(order)


def test_summary_counts(root):
    html, _ = _render(root, report())
    pairs = re.findall(r'<div class="n">(\d+)</div><div class="l">(\w+)</div>', html)
    counts = {label: int(n) for n, label in pairs}
    assert counts == {
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
    html, _ = _render(root, payload)
    expected = base64.b64encode((root / "page-1.png").read_bytes()).decode()
    assert f'src="data:image/png;base64,{expected}"' in html
    assert "Screenshot unavailable: gone.png" in html


def test_screenshot_outside_root_is_unavailable(root, tmp_path):
    write(tmp_path / "secret.png", 64)
    payload = report(
        issues=[issue("BUG-1", "Major", screenshot_paths=[str(tmp_path / "secret.png")])]
    )
    html, _ = _render(root, payload)
    assert "Screenshot unavailable: secret.png" in html and "base64" not in html


def test_video_uses_publish_artifact_path_rules(root):
    write(root / "video-2026-10-15T13-46-38-154Z.webm", 64)
    link = "- [Video](../../artifacts/video-2026-10-15T13-46-38-154Z.webm)"
    html, media = _render(root, report(), video=link)
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in html
    assert media.embedded_paths() == [root / "video-2026-10-15T13-46-38-154Z.webm"]


def test_no_video_and_missing_video_notes(root):
    assert "No recording was attached" in _render(root, report())[0]
    assert "could not be found" in _render(root, report(), video="nope.webm")[0]


def test_excluded_videos_are_named_not_embedded_and_not_dropped(root):
    write(root / "run.webm", 64)
    write(root / "tc1.webm", 64)
    write(root / "page-1.png", 64)
    payload = report(
        test_cases=[case("TC-1", video_path="tc1.webm", screenshot_paths=["page-1.png"])]
    )
    html, media = _render(root, payload, video="run.webm", exclude=True)
    assert "<video" not in html and "data:video" not in html
    assert "The recording (run.webm) is not embedded in this report." in html
    assert "The recording (tc1.webm) is not embedded in this report." in html
    assert "data:image/png" in html  # screenshots are unaffected
    assert media.dropped == []
    assert media.embedded_paths() == [root / "page-1.png"]


def test_budget_drops_largest_screenshot_first_then_video(root):
    payload = report(issues=[issue("BUG-1", "Major", screenshot_paths=["small.png", "large.png"])])
    skeleton, _ = _render(root, payload)  # before the files exist: no media at all
    write(root / "small.png", 2_000)
    write(root / "large.png", 40_000)
    write(root / "video.webm", 30_000)
    html, media = _render(root, payload, video="video.webm", limit=len(skeleton) + 50_000)
    assert media.dropped == ["large.png"]
    assert "Left out to stay within the size limit: large.png" in html
    assert "data:video/webm" in html and len(html.encode()) <= len(skeleton) + 50_000

    # Screenshots go before the video, whatever their size.
    html, media = _render(root, payload, video="video.webm", limit=len(skeleton) + 41_000)
    assert media.dropped == ["large.png", "small.png"]
    assert "data:video/webm" in html

    html, media = _render(root, payload, video="video.webm", limit=len(skeleton) + 1_000)
    assert media.dropped == ["large.png", "small.png", "video.webm"]
    assert "was left out to stay within the size limit" in html
    assert media.embedded_paths() == []


def test_report_too_large_even_without_media_errors(root):
    with pytest.raises(ArtifactError, match="even without media"):
        _render(root, report(), limit=1_000)


def _row(html: str, index: int) -> str:
    start = html.index(f'id="case-{index}"')
    end = html.find('<details class="row"', start + 1)
    return html[start : end if end != -1 else len(html)]


def _case_payload(**extra):
    cases = [
        case("TC-1", video_path="../../artifacts/tc1.webm"),
        case("TC-2", "fail", video_path="[Video](tc2.webm)"),
        case("TC-3", "blocked"),
    ]
    return report(test_cases=cases, issues=[issue("BUG-1", "Major", test_case_id="TC-2")], **extra)


def test_case_videos_embed_in_their_rows_and_issue_links(root):
    write(root / "tc1.webm", 50)
    write(root / "tc2.webm", 60)
    html, _ = _render(root, _case_payload())
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in _row(html, 0)
    assert '<video controls preload="metadata" src="data:video/webm;base64,' in _row(html, 1)
    assert "<video" not in _row(html, 2)
    assert '<a class="rec-link" href="#case-1">Recording</a>' in html
    assert "Full run recording" not in html and "Screen recording" not in html


def test_run_video_section_kept_alongside_case_videos(root):
    write(root / "tc1.webm", 50)
    write(root / "run.webm", 50)
    html, _ = _render(root, _case_payload(), video="run.webm")
    assert "Full run recording" in html


def test_budget_drops_shots_then_largest_case_video_then_run_video(root):
    payload = _case_payload()
    payload["test_cases"][2]["screenshot_paths"] = ["shot.png"]
    skeleton, _ = _render(root, payload)
    write(root / "shot.png", 1_000)
    write(root / "tc1.webm", 10_000)
    write(root / "tc2.webm", 20_000)
    write(root / "run.webm", 30_000)
    base = len(skeleton.encode())

    _, media = _render(root, payload, "run.webm", base + 4 * 30_000 // 3 + 4 * 10_000 // 3 + 500)
    assert media.dropped == ["shot.png", "tc2.webm"]
    _, media = _render(root, payload, "run.webm", base + 4 * 30_000 // 3 + 500)
    assert media.dropped == ["shot.png", "tc2.webm", "tc1.webm"]
    html, media = _render(root, payload, "run.webm", base + 1_000)
    assert media.dropped == ["shot.png", "tc2.webm", "tc1.webm", "run.webm"]
    assert re.search(r"Left out to stay within the size limit: shot.png, tc2.webm", html)
