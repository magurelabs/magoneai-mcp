"""Tests for web_research: fusion, page choice, passage ranking, deadlines, cache.

Search and fetch are injected fakes, so no network or key is needed.
"""

import asyncio
import importlib
import time
from urllib.parse import urlparse

import pytest

from connectors.web_search import _research
from connectors.web_search._research import (
    BM25,
    _PageCache,
    budget,
    fuse_results,
    pick_pages,
    rank_passages,
    research,
    split_passages,
)
from connectors.web_search._scrape import TIMEOUT, ScrapeError

_normalize_url = importlib.import_module("connectors.web_search.server")._normalize_url


def _run(coro):
    return asyncio.run(coro)


def _hit(url, title="t", content="", date=""):
    return {"url": url, "title": title, "content": content, "publishedDate": date}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_fuse_rewards_pages_found_by_several_queries():
    a = [_hit("https://a.org/x"), _hit("https://b.org/y")]
    b = [_hit("https://b.org/y?utm_source=z"), _hit("https://c.org/z")]
    fused = fuse_results([a, b], _normalize_url)
    assert fused[0]["url"].startswith("https://b.org/y")
    assert len(fused) == 3  # utm variant merged


def test_fuse_keeps_first_nonempty_snippet_and_date():
    a = [_hit("https://a.org/x")]
    b = [_hit("https://a.org/x", content="snip", date="2026-01-01")]
    fused = fuse_results([a, b], _normalize_url)
    assert fused[0]["snippet"] == "snip"
    assert fused[0]["published_date"] == "2026-01-01"


def test_pick_pages_skips_login_walls_and_caps_per_domain():
    fused = fuse_results(
        [
            [
                _hit("https://www.facebook.com/post/1"),
                _hit("https://a.org/1"),
                _hit("https://a.org/2"),
                _hit("https://a.org/3"),
                _hit("https://m.youtube.com/watch?v=1"),
                _hit("https://b.org/1"),
            ]
        ],
        _normalize_url,
    )
    picked = pick_pages(fused, 5)
    urls = [p["url"] for p in picked]
    assert "https://www.facebook.com/post/1" not in urls
    assert not any("youtube" in u for u in urls)
    assert sum(urlparse(u).hostname == "a.org" for u in urls) == 2
    assert "https://b.org/1" in urls


def test_split_passages_respects_size_and_keeps_all_text():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
    parts = split_passages(text, size=500)
    assert all(len(p) <= 1000 for p in parts)
    joined = " ".join(parts)
    for i in range(10):
        assert f"Paragraph {i}." in joined


def test_split_passages_breaks_one_huge_paragraph():
    text = ("Sentence number one is here. " * 200).strip()
    parts = split_passages(text, size=300)
    assert len(parts) > 5
    assert all(len(p) <= 600 for p in parts)


def test_bm25_prefers_matching_passage():
    docs = [["school", "rating", "good"], ["weather", "today", "sunny"]]
    bm = BM25(docs)
    assert bm.score(["rating"], 0) > bm.score(["rating"], 1)


def test_rank_passages_picks_relevant_text_in_document_order():
    pad = " Lorem ipsum dolor sit amet consectetur." * 18  # ~700 chars: one passage each
    page = {
        "_text": "\n\n".join(
            [
                "Navigation Home About Contact." + pad,
                "The school received an overall rating of Very Good in its 2025 inspection." + pad,
                "Our cafeteria serves lunch daily." + pad,
                "Accreditation: the school is accredited by an international body." + pad,
            ]
        ),
        "_score": 1.0,
    }
    ranked = rank_passages([page], ["school inspection rating", "school accreditation"], per_page=2)
    passages = ranked[0]["passages"]
    joined = " ".join(passages)
    assert "Very Good" in joined
    assert "accredited" in joined
    assert "cafeteria" not in joined


def test_rank_passages_handles_arabic():
    page = {"_text": "تقييم المدرسة جيد جدا\n\nالطقس مشمس اليوم", "_score": 1.0}
    ranked = rank_passages([page], ["تقييم المدرسة"], per_page=1)
    assert "تقييم" in ranked[0]["passages"][0]


def test_budget_caps_total_characters():
    pages = [{"passages": ["x" * 400, "y" * 400]}, {"passages": ["z" * 400]}]
    out = budget(pages, 900)
    total = sum(len(p) for page in out for p in page["passages"])
    assert total <= 900


def test_cache_ttl_and_lru():
    cache = _PageCache(ttl_s=60, max_items=2)
    cache.put("a", "A", "html")
    cache.put("b", "B", "html")
    assert cache.get("a") == ("A", "html")
    cache.put("c", "C", "html")  # evicts b (a was touched)
    assert cache.get("b") is None
    assert cache.get("a") is not None
    expired = _PageCache(ttl_s=0.0, max_items=2)
    expired.put("k", "v", "html")
    time.sleep(0.01)
    assert expired.get("k") is None


# --------------------------------------------------------------------------- #
# research(): orchestration
# --------------------------------------------------------------------------- #


def _search_fake(table, delay=0.0, calls=None):
    async def search(q):
        if calls is not None:
            calls.append(q)
        await asyncio.sleep(delay)
        value = table.get(q)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, dict) else {"results": value or []}

    return search


def _fetch_fake(pages, delays=None, calls=None):
    async def fetch(url, max_chars):
        if calls is not None:
            calls.append(url)
        await asyncio.sleep((delays or {}).get(url, 0.0))
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value, "html"

    return fetch


def test_research_end_to_end_returns_ranked_passages():
    search = _search_fake(
        {
            "alpha rating": [
                _hit("https://a.org/1", "A1", "a snippet"),
                _hit("https://b.org/1", "B1"),
            ],
            "alpha accreditation": [_hit("https://b.org/1", "B1"), _hit("https://c.org/1", "C1")],
        }
    )
    fetch = _fetch_fake(
        {
            "https://a.org/1": "Menu\n\nAlpha was rated Good in 2024.\n\nFooter",
            "https://b.org/1": "Alpha holds accreditation from a recognised body.\n\nContact us",
            "https://c.org/1": "Unrelated text about weather.",
        }
    )
    out = _run(
        research(
            ["alpha rating", "alpha accreditation"],
            search=search,
            fetch=fetch,
            normalize=_normalize_url,
            cache=None,
        )
    )
    urls = [s["url"] for s in out["sources"]]
    assert "https://b.org/1" in urls and "https://a.org/1" in urls
    joined = " ".join(p for s in out["sources"] for p in s["passages"])
    assert "rated Good" in joined and "accreditation" in joined
    assert "timing_ms" in out and out["timing_ms"]["total"] >= 0


def test_research_runs_searches_and_fetches_in_parallel():
    queries = [f"q{i}" for i in range(4)]
    table = {q: [_hit(f"https://d{i}.org/p", "T")] for i, q in enumerate(queries)}
    search = _search_fake(table, delay=0.3)
    fetch = _fetch_fake(
        {f"https://d{i}.org/p": "text about q" for i in range(4)},
        {f"https://d{i}.org/p": 0.3 for i in range(4)},
    )
    t0 = time.monotonic()
    out = _run(research(queries, search=search, fetch=fetch, normalize=_normalize_url, cache=None))
    elapsed = time.monotonic() - t0
    assert elapsed < 1.2  # sequential would be 2.4s
    assert len(out["sources"]) == 4


def test_research_deadline_returns_what_is_ready():
    search = _search_fake({"q": [_hit("https://fast.org/a"), _hit("https://slow.org/b")]})
    fetch = _fetch_fake(
        {"https://fast.org/a": "fast page about q", "https://slow.org/b": "slow page about q"},
        {"https://slow.org/b": 5.0},
    )
    t0 = time.monotonic()
    out = _run(
        research(
            ["q"], search=search, fetch=fetch, normalize=_normalize_url, deadline_s=1.0, cache=None
        )
    )
    assert time.monotonic() - t0 < 1.8
    assert [s["url"] for s in out["sources"]] == ["https://fast.org/a"]
    reasons = {u["url"]: u["reason"] for u in out["unread_pages"]}
    assert reasons["https://slow.org/b"] in ("deadline", "timeout")


def test_research_per_page_timeout_and_scrape_errors():
    search = _search_fake(
        {"q": [_hit("https://ok.org/a"), _hit("https://bad.org/b"), _hit("https://hang.org/c")]}
    )
    fetch = _fetch_fake(
        {
            "https://ok.org/a": "good text about q",
            "https://bad.org/b": ScrapeError(TIMEOUT, "boom"),
            "https://hang.org/c": "never",
        },
        {"https://hang.org/c": 3.0},
    )
    out = _run(
        research(
            ["q"],
            search=search,
            fetch=fetch,
            normalize=_normalize_url,
            page_timeout_s=0.5,
            deadline_s=5,
            cache=None,
        )
    )
    assert [s["url"] for s in out["sources"]] == ["https://ok.org/a"]
    reasons = {u["url"]: u["reason"] for u in out["unread_pages"]}
    assert reasons["https://bad.org/b"] == TIMEOUT
    assert reasons["https://hang.org/c"] == "timeout"


def test_research_one_failed_query_does_not_sink_others():
    search = _search_fake(
        {
            "good": [_hit("https://g.org/a")],
            "bad": RuntimeError("x"),
            "err": {"error": "Serper rate limit exceeded."},
        }
    )
    fetch = _fetch_fake({"https://g.org/a": "good text"})
    out = _run(
        research(
            ["good", "bad", "err"], search=search, fetch=fetch, normalize=_normalize_url, cache=None
        )
    )
    assert len(out["sources"]) == 1
    assert len(out["errors"]) == 2
    assert "error" not in out


def test_research_all_failed_reports_error():
    search = _search_fake({"a": {"error": "Invalid SERPER_API_KEY."}})
    out = _run(
        research(["a"], search=search, fetch=_fetch_fake({}), normalize=_normalize_url, cache=None)
    )
    assert "error" in out and "Invalid" in out["error"]


def test_research_no_results_note():
    search = _search_fake({"a": []})
    out = _run(
        research(["a"], search=search, fetch=_fetch_fake({}), normalize=_normalize_url, cache=None)
    )
    assert out["note"] == "no_results" and out["sources"] == []


def test_research_rejects_empty_queries_and_caps_count():
    out = _run(
        research(
            ["", "  "],
            search=_search_fake({}),
            fetch=_fetch_fake({}),
            normalize=_normalize_url,
            cache=None,
        )
    )
    assert "error" in out
    calls = []
    search = _search_fake({}, calls=calls)
    _run(
        research(
            [f"q{i}" for i in range(9)],
            search=search,
            fetch=_fetch_fake({}),
            normalize=_normalize_url,
            cache=None,
        )
    )
    assert len(calls) == _research.MAX_QUERIES


def test_research_uses_cache_on_second_call():
    cache = _PageCache()
    search = _search_fake({"q": [_hit("https://a.org/x")]})
    calls = []
    fetch = _fetch_fake({"https://a.org/x": "cached text about q"}, calls=calls)
    _run(research(["q"], search=search, fetch=fetch, normalize=_normalize_url, cache=cache))
    out = _run(research(["q"], search=search, fetch=fetch, normalize=_normalize_url, cache=cache))
    assert calls == ["https://a.org/x"]
    assert out["sources"][0]["passages"]


def test_research_output_budget_respected():
    search = _search_fake({"q": [_hit(f"https://s{i}.org/p") for i in range(5)]})
    big = "\n\n".join("q relevant text " * 50 for _ in range(10))
    fetch = _fetch_fake({f"https://s{i}.org/p": big for i in range(5)})
    out = _run(
        research(
            ["q"],
            search=search,
            fetch=fetch,
            normalize=_normalize_url,
            output_chars=3000,
            cache=None,
        )
    )
    total = sum(len(p) for s in out["sources"] for p in s["passages"])
    assert total <= 3000


def test_web_research_tool_wires_search_and_fetch(monkeypatch):
    search_tools = importlib.import_module("connectors.web_search.server")

    captured = {}

    async def fake_run(
        credentials, query, num_results=10, language="en", region=None, time_range="", **kw
    ):
        captured.setdefault("q", []).append((query, region, time_range))
        return {"results": [_hit("https://x.org/1")]}

    async def fake_fetch(url, max_chars):
        return "x text about topic", "html"

    monkeypatch.setattr(search_tools, "run_web_search", fake_run)
    monkeypatch.setattr(search_tools, "_fetch_page_content", fake_fetch)
    _research.PAGE_CACHE.clear()

    out = _run(
        search_tools.web_research({}, ["topic one", "topic two"], region="ae", time_range="bogus")
    )
    assert [c[0] for c in captured["q"]] == ["topic one", "topic two"]
    assert all(c[1] == "ae" and c[2] == "" for c in captured["q"])
    assert out["sources"][0]["url"] == "https://x.org/1"


@pytest.mark.parametrize("q", ["single string query"])
def test_web_research_accepts_a_bare_string(monkeypatch, q):
    search_tools = importlib.import_module("connectors.web_search.server")

    async def fake_run(credentials, query, **kw):
        return {"results": []}

    monkeypatch.setattr(search_tools, "run_web_search", fake_run)

    out = _run(search_tools.web_research({}, q))
    assert out["queries"] == [q]


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, _research._DEFAULT_LOGIN_WALLED),
        ("  ", _research._DEFAULT_LOGIN_WALLED),
        ("-", ()),
        ("Example.com, www.walled.test ,,", ("example.com", "walled.test")),
    ],
)
def test_login_walled_hosts_can_be_replaced(value, expected):
    assert _research._login_walled_hosts(value) == expected


def test_web_research_logs_why_pages_went_unread(monkeypatch):
    from structlog.testing import capture_logs

    search_tools = importlib.import_module("connectors.web_search.server")

    async def fake_run(credentials, query, **kw):
        return {"results": [_hit("https://a.org/1"), _hit("https://b.org/2")]}

    async def fake_fetch(url, max_chars):
        if urlparse(url).hostname == "b.org":
            raise ScrapeError(TIMEOUT, "slow")
        return "a text about topic", "html"

    monkeypatch.setattr(search_tools, "run_web_search", fake_run)
    monkeypatch.setattr(search_tools, "_fetch_page_content", fake_fetch)
    _research.PAGE_CACHE.clear()

    with capture_logs() as events:
        _run(search_tools.web_research({}, ["topic"]))

    logged = [e for e in events if e["event"] == "web_research.pages"]
    assert len(logged) == 1
    assert logged[0]["sources"] == 1
    assert logged[0]["unread"] == 1
    assert logged[0]["unread_reasons"] == {TIMEOUT: 1}


def _three_pages():
    search = _search_fake({"q": [_hit(f"https://s{i}.org/p", f"S{i}") for i in range(3)]})
    text = "\n\n".join("q relevant text about the question " * 20 for _ in range(4))
    fetch = _fetch_fake({f"https://s{i}.org/p": text for i in range(3)})
    return search, fetch


def test_a_page_trimmed_by_the_budget_is_listed_as_unread():
    search, fetch = _three_pages()
    out = _run(
        research(
            ["q"], search=search, fetch=fetch, normalize=_normalize_url, output_chars=1500,
            cache=None,
        )
    )
    shown = {s["url"] for s in out["sources"]}
    unread = {u["url"]: u["reason"] for u in out["unread_pages"]}
    assert shown and len(shown) < 3
    # Every page read is in sources or in unread_pages, never in neither.
    assert shown | set(unread) == {f"https://s{i}.org/p" for i in range(3)}
    assert set(unread.values()) == {"budget"}
    # Its search snippet stays available too.
    assert set(unread) <= {o["url"] for o in out["other_results"]}


def test_pages_read_but_all_trimmed_are_not_called_unreadable():
    search, fetch = _three_pages()
    out = _run(
        research(
            ["q"], search=search, fetch=fetch, normalize=_normalize_url, output_chars=10,
            cache=None,
        )
    )
    assert out["sources"] == []
    assert out["note"] == "output_budget_too_small"
    assert [u["reason"] for u in out["unread_pages"]] == ["budget"] * 3
