"""scrape_url `focus` and the deployment cap on search-flow scrapes."""

import asyncio
import importlib
from unittest.mock import AsyncMock, patch

import pytest

from connectors.web_search import _research

search_tools = importlib.import_module("connectors.web_search.server")

URL = "https://example.test/report"


def _page(paragraphs: list[str]) -> str:
    return "\n\n".join(paragraphs)


FILLER = [
    f"Section {i}. The annual report covers general operations, staffing and facilities."
    for i in range(60)
]
TARGET = (
    "Inspection outcome. The 2024 to 2025 inspection rated the school Very Good overall, "
    "with Outstanding for health and safety."
)
PAGE = _page(FILLER[:40] + [TARGET] + FILLER[40:])


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(search_tools, "CRAWL4AI_URL", "")


def _run_scrape(content: str, **kw):
    fetch = AsyncMock(return_value=(content, "html"))
    with (
        patch.object(search_tools, "_fetch_page_content", fetch),
        patch.object(search_tools, "precheck_url", lambda u: None),
        patch.object(search_tools, "validate_url", lambda u: None),
    ):
        out = asyncio.run(search_tools.scrape_url({}, URL, **kw))
    return out, fetch


def test_focus_returns_matching_passage_from_deep_in_the_page():
    out, fetch = _run_scrape(PAGE, focus="inspection rating overall", max_chars=600)
    assert out["focused"] is True
    assert "Very Good overall" in out["content"]
    assert len(out["content"]) <= 600
    assert out["source_char_count"] == len(PAGE)
    # The whole page is read when a focus is given, not just max_chars.
    assert fetch.await_args.args[1] >= search_tools.WEB_FOCUS_READ_CHARS


def test_focus_with_no_match_falls_back_to_the_head():
    out, _ = _run_scrape(PAGE, focus="zebra quantum", max_chars=300)
    assert out["focused"] is False
    assert out["content"].startswith("Section 0.")
    assert len(out["content"]) <= 300


def test_no_focus_keeps_the_old_shape():
    out, fetch = _run_scrape("Short page text that is long enough to count as content.")
    assert set(out) == {"url", "content", "char_count", "content_type"}
    assert fetch.await_args.args[1] == 8000


def test_max_chars_is_capped_by_the_deployment(monkeypatch):
    monkeypatch.setattr(search_tools, "WEB_RESULT_MAX_CHARS", 5000)
    _, fetch = _run_scrape("x" * 100, max_chars=100000)
    assert fetch.await_args.args[1] == 5000


def test_bad_max_chars_falls_back_to_default():
    _, fetch = _run_scrape("x" * 100, max_chars="lots")
    assert fetch.await_args.args[1] == min(8000, search_tools.WEB_RESULT_MAX_CHARS)


def test_focus_text_keeps_document_order_within_budget():
    pad = " filler words here" * 30
    text = _page(
        ["alpha beta." + pad, "gamma delta." + pad, "alpha gamma." + pad, "epsilon." + pad]
    )
    out, matched = _research.focus_text(text, "alpha gamma", 3000)
    assert matched
    assert out.index("alpha beta") < out.index("gamma delta") < out.index("alpha gamma")
    assert "epsilon" not in out


def test_focus_text_respects_budget():
    out, matched = _research.focus_text(PAGE, "section operations staffing", 500)
    assert matched and len(out) <= 500
