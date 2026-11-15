"""web_research: several searches, parallel page reads, ranked passages, one call.

The agent used to run search_web, pick results, then call scrape_url page by
page and read whole pages back into the model. Each round trip is a model step,
and whole pages are mostly navigation and boilerplate. This module does the
mechanical part in one tool call with a hard deadline:

1. run every query in parallel through the same provider dispatch search_web
   uses (so region, SafeSearch and credentials behave identically);
2. fuse the result lists (reciprocal rank fusion) and pick the best few pages,
   at most two per domain, skipping hosts that need a login to read;
3. fetch those pages in parallel through the hardened scrape path (SSRF guard,
   IP pinning, PDF text, AIA chain recovery), each under its own time cap;
4. split the readable text into passages and rank them against the queries
   with BM25, so the model receives the few paragraphs that answer the
   question instead of whole pages.

Whatever is ready when the deadline hits is returned; slow pages are dropped,
never waited for. Extracted text of public pages is cached in process for a
short TTL, keyed by normalised URL. Nothing caller specific is cached: fetches
carry no credentials and private addresses are refused by the scrape path.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections import Counter, OrderedDict
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from ._scrape import ScrapeError

SearchFn = Callable[..., Awaitable[dict[str, Any]]]
FetchFn = Callable[[str, int], Awaitable[tuple[str, str]]]

DEFAULT_MAX_PAGES = 5
MAX_PAGES_CAP = 8
MAX_QUERIES = 5
RESULTS_PER_QUERY = 8
PER_DOMAIN_PAGES = 2
PAGE_TIMEOUT_S = float(os.getenv("WEB_RESEARCH_PAGE_TIMEOUT_S", "5"))
DEADLINE_S = float(os.getenv("WEB_RESEARCH_DEADLINE_S", "8"))
PAGE_TEXT_CHARS = 60_000
PASSAGE_CHARS = 900
PASSAGES_PER_PAGE = 3
OUTPUT_CHARS = int(os.getenv("WEB_RESEARCH_OUTPUT_CHARS", "24000"))
OTHER_RESULTS = 8
CACHE_TTL_S = float(os.getenv("WEB_RESEARCH_CACHE_TTL_S", "900"))
CACHE_MAX = int(os.getenv("WEB_RESEARCH_CACHE_MAX", "256"))

# Hosts whose pages need a login or are mostly script shells, so a plain fetch
# returns a sign-in wall. Their search snippets are still returned.
# WEB_RESEARCH_LOGIN_WALLED_HOSTS (comma-separated) replaces this list, so an
# operator can correct it without a release; set it to "-" to read every host.
_DEFAULT_LOGIN_WALLED = (
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "pinterest.com",
    "threads.net",
)


def _login_walled_hosts(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return _DEFAULT_LOGIN_WALLED
    if value.strip() == "-":
        return ()
    hosts = (h.strip().lower().removeprefix("www.") for h in value.split(","))
    return tuple(h for h in hosts if h)


_LOGIN_WALLED = _login_walled_hosts(os.getenv("WEB_RESEARCH_LOGIN_WALLED_HOSTS"))

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "this to was were what which who will with how does do about vs versus".split()
)


# --------------------------------------------------------------------------- #
# Page cache
# --------------------------------------------------------------------------- #


class _PageCache:
    """Small TTL + LRU cache of extracted page text, keyed by normalised URL."""

    def __init__(self, ttl_s: float = CACHE_TTL_S, max_items: int = CACHE_MAX) -> None:
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._items: OrderedDict[str, tuple[float, str, str]] = OrderedDict()

    def get(self, key: str) -> tuple[str, str] | None:
        hit = self._items.get(key)
        if not hit:
            return None
        stored_at, text, kind = hit
        if time.monotonic() - stored_at > self.ttl_s:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return text, kind

    def put(self, key: str, text: str, kind: str) -> None:
        if not key or not text:
            return
        self._items[key] = (time.monotonic(), text, kind)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


PAGE_CACHE = _PageCache()


# --------------------------------------------------------------------------- #
# Ranking helpers
# --------------------------------------------------------------------------- #


# Arabic harakat, Quranic marks and tatweel carry no meaning for matching.
_AR_MARKS = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_AR_FOLD = str.maketrans(
    {
        "\u0623": "\u0627",  # alef with hamza above
        "\u0625": "\u0627",  # alef with hamza below
        "\u0622": "\u0627",  # alef with madda
        "\u0671": "\u0627",  # alef wasla
        "\u0649": "\u064a",  # alef maksura to yeh
        "\u0629": "\u0647",  # teh marbuta to heh
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Persian digits
    }
)
# The definite article, alone or after "and" or "with".
_AR_ARTICLES = ("\u0648\u0627\u0644", "\u0628\u0627\u0644", "\u0627\u0644")


def _norm(word: str) -> str:
    """Fold a lowercased word to a matching key: Arabic spelling variants and
    the definite article, English plurals."""
    if word.isascii():
        if len(word) > 3 and not word.isdigit():
            if word.endswith("ies"):
                return word[:-3] + "y"
            if word.endswith(("ches", "shes", "sses", "xes")):
                return word[:-2]
            if word.endswith("s") and not word.endswith(("ss", "us", "is")):
                return word[:-1]
        return word
    word = word.translate(_AR_FOLD)
    if len(word) > 3:
        for art in _AR_ARTICLES:
            if word.startswith(art) and len(word) - len(art) >= 2:
                return word[len(art) :]
    return word


def _terms(text: str) -> list[str]:
    words = _WORD.findall(_AR_MARKS.sub("", text).lower())
    # Single digits stay: "Year 3" or "Grade 9" depend on them.
    return [_norm(w) for w in words if w not in _STOP and (len(w) > 1 or w.isdigit())]


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _login_walled(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in _LOGIN_WALLED)


def fuse_results(result_lists: list[list[dict]], normalize: Callable[[str], str]) -> list[dict]:
    """Reciprocal rank fusion across the per-query result lists.

    A page that ranks well for several queries rises; duplicates (after URL
    normalisation) merge, keeping the first non-empty snippet and date.
    """
    k = 60.0
    merged: dict[str, dict] = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            url = item.get("url") or ""
            key = normalize(url) or url
            if not key:
                continue
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "url": url,
                    "title": item.get("title") or "",
                    "snippet": item.get("content") or "",
                    "published_date": item.get("publishedDate") or "",
                    "source_domain": _host(url),
                    "_score": 0.0,
                    "_key": key,
                }
                merged[key] = entry
            else:
                if not entry["snippet"] and item.get("content"):
                    entry["snippet"] = item["content"]
                if not entry["published_date"] and item.get("publishedDate"):
                    entry["published_date"] = item["publishedDate"]
            entry["_score"] += 1.0 / (k + rank + 1)
    return sorted(merged.values(), key=lambda e: e["_score"], reverse=True)


def pick_pages(fused: list[dict], max_pages: int) -> list[dict]:
    """The best pages to read: skip login walls, at most two per domain."""
    picked: list[dict] = []
    per_domain: Counter[str] = Counter()
    for entry in fused:
        if len(picked) >= max_pages:
            break
        if _login_walled(entry["url"]):
            continue
        dom = entry["source_domain"]
        if per_domain[dom] >= PER_DOMAIN_PAGES:
            continue
        per_domain[dom] += 1
        picked.append(entry)
    return picked


_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_table(para: str) -> tuple[str, str, list[str], str] | None:
    """Find a markdown table in a paragraph: (text before, header and rule
    rows, body rows, text after), or None when there is no table."""
    lines = para.split("\n")
    for j in range(1, len(lines)):
        if "|" in lines[j - 1] and "|" in lines[j] and _TABLE_RULE.match(lines[j]):
            end = j + 1
            while end < len(lines) and "|" in lines[end]:
                end += 1
            return (
                "\n".join(lines[: j - 1]).strip(),
                "\n".join(lines[j - 1 : j + 1]),
                lines[j + 1 : end],
                "\n".join(lines[end:]).strip(),
            )
    return None


def _table_passages(header: str, rows: list[str], size: int) -> list[str]:
    """Cut a table on row boundaries, each piece starting with the header."""
    out: list[str] = []
    buf = header
    for row in rows:
        if buf != header and len(buf) + len(row) + 1 > size:
            out.append(buf)
            buf = header
        buf = f"{buf}\n{row}"
    if buf != header or not out:
        out.append(buf)
    return out


def split_passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Split page text into passages of about `size` chars on paragraph and
    sentence boundaries, so a passage never starts mid-sentence. A markdown
    table too long for one passage is cut between rows, and every piece
    repeats the header row so each one reads on its own."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
    out: list[str] = []
    tables: set[int] = set()
    buf = ""
    i = 0
    while i < len(paras):
        para = paras[i]
        i += 1
        table = _split_table(para) if len(para) > size * 1.5 else None
        if table:
            before, header, rows, after = table
            if before or after:
                # Text around the table is handled as paragraphs of its own.
                paras[i:i] = [p for p in (before, "\n".join([header, *rows]), after) if p]
                continue
            if buf:
                out.append(buf)
                buf = ""
            for piece in _table_passages(header, rows, size):
                tables.add(len(out))
                out.append(piece)
            continue
        if len(para) > size * 1.5:
            # Long paragraph: break on sentence ends.
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                if buf and len(buf) + len(sent) + 1 > size:
                    out.append(buf)
                    buf = ""
                buf = f"{buf} {sent}".strip() if buf else sent
            continue
        if buf and len(buf) + len(para) + 2 > size:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    # A single huge sentence still gets cut, at a word boundary. Table pieces
    # already end on a row.
    final: list[str] = []
    for n, p in enumerate(out):
        while n not in tables and len(p) > size * 2:
            cut = p.rfind(" ", 0, size * 2)
            cut = cut if cut > 0 else size * 2
            final.append(p[:cut])
            p = p[cut:].strip()
        if p:
            final.append(p)
    return final


class BM25:
    """Okapi BM25 over a small passage set. No external dependency."""

    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        self.n = len(docs)
        self.avgdl = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tfs = [Counter(d) for d in docs]

    def score(self, query: list[str], i: int) -> float:
        tf = self.tfs[i]
        dl = len(self.docs[i]) or 1
        s = 0.0
        for t in set(query):
            f = tf.get(t)
            if not f:
                continue
            idf = self.idf.get(t, 0.0)
            s += (
                idf
                * f
                * (self.k1 + 1)
                / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            )
        return s


def rank_passages(
    pages: list[dict], queries: list[str], per_page: int = PASSAGES_PER_PAGE
) -> list[dict]:
    """Attach the best passages to each page; pages keep their search order."""
    query_terms = [_terms(q) for q in queries]
    all_terms = [t for q in query_terms for t in q]
    flat: list[tuple[int, str]] = []
    for pi, page in enumerate(pages):
        for passage in split_passages(page.get("_text") or ""):
            flat.append((pi, passage))
    if not flat:
        return pages
    bm = BM25([_terms(p) for _, p in flat])
    scored: list[tuple[float, int, int, str]] = []
    for idx, (pi, passage) in enumerate(flat):
        # Best match against any single query, plus a small bonus for covering
        # terms from several queries, so a passage answering two sub-questions
        # beats one that repeats a single keyword.
        best = max((bm.score(q, idx) for q in query_terms if q), default=0.0)
        coverage = bm.score(all_terms, idx) * 0.1
        scored.append((best + coverage, pi, idx, passage))
    by_page: dict[int, list[tuple[float, int, str]]] = {}
    for s, pi, idx, passage in scored:
        by_page.setdefault(pi, []).append((s, idx, passage))
    for pi, page in enumerate(pages):
        cands = sorted(by_page.get(pi, []), key=lambda x: x[0], reverse=True)[:per_page]
        cands = [c for c in cands if c[0] > 0] or cands[:1]
        # Keep chosen passages in document order so they read naturally.
        page["passages"] = [p for _, _, p in sorted(cands, key=lambda x: x[1])]
        page["_best"] = cands[0][0] if cands else 0.0
    return pages


def focus_text(text: str, focus: str, max_chars: int) -> tuple[str, bool, list[str]]:
    """Keep the passages of `text` that match `focus`, within `max_chars`.

    Passages are ranked with BM25 against the focus terms, the best ones are
    kept until the budget is spent, and they are returned in document order
    joined by a blank line, so the result still reads top to bottom. Returns
    (text, focused, matched_terms): matched_terms are the focus words found in
    the kept passages, and focused is True only when they are at least half of
    the focus words. When no passage shares a term with the focus, the head of
    the page is returned instead.
    """
    words = _WORD.findall(_AR_MARKS.sub("", focus or "").lower())
    keys = {w: _terms(w) for w in dict.fromkeys(words)}
    keys = {w: k[0] for w, k in keys.items() if k}
    terms = list(dict.fromkeys(keys.values()))
    # Passages smaller than the budget, so several matches can fit.
    passages = split_passages(text, size=max(200, min(PASSAGE_CHARS, max_chars // 3)))
    if not terms or not passages:
        return _head(text, max_chars), False, []
    bm = BM25([_terms(p) for p in passages])
    ranked = sorted(
        ((bm.score(terms, i), i) for i in range(len(passages))), key=lambda x: x[0], reverse=True
    )
    chosen: list[int] = []
    used = 0
    out = ""
    for score, i in ranked:
        if score <= 0:
            break
        size = len(passages[i]) + (2 if chosen else 0)
        if used + size > max_chars:
            if not chosen:
                # The best match alone is over budget: keep its head.
                out = _head(passages[i], max_chars)
                break
            continue
        chosen.append(i)
        used += size
    if chosen:
        out = "\n\n".join(passages[i] for i in sorted(chosen))
    if not out:
        return _head(text, max_chars), False, []
    found = set(_terms(out))
    matched = [w for w, k in keys.items() if k in found]
    hit = {keys[w] for w in matched}
    return out, len(hit) * 2 >= len(terms), matched


def _head(text: str, max_chars: int) -> str:
    """The first max_chars of text, cut at the last full stop when one is near."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind(".", 0, max_chars)
    return text[: cut + 1] if cut > max_chars // 2 else text[:max_chars]


def budget(pages: list[dict], max_chars: int) -> list[dict]:
    """Trim passages so the whole payload stays within max_chars."""
    used = 0
    for page in pages:
        kept: list[str] = []
        for passage in page.get("passages") or []:
            if used + len(passage) > max_chars:
                break
            kept.append(passage)
            used += len(passage)
        page["passages"] = kept
    return pages


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def research(
    queries: list[str],
    *,
    search: SearchFn,
    fetch: FetchFn,
    normalize: Callable[[str], str],
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline_s: float = DEADLINE_S,
    page_timeout_s: float = PAGE_TIMEOUT_S,
    output_chars: int = OUTPUT_CHARS,
    cache: _PageCache | None = PAGE_CACHE,
) -> dict[str, Any]:
    """Run the search, read and rank steps under one deadline."""
    t0 = time.monotonic()
    queries = [q.strip() for q in queries if q and q.strip()][:MAX_QUERIES]
    if not queries:
        return {"error": "web_research needs at least one non-empty query."}
    max_pages = max(1, min(int(max_pages or DEFAULT_MAX_PAGES), MAX_PAGES_CAP))
    deadline = t0 + max(1.0, float(deadline_s))

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    # 1. Searches in parallel. A query that errors costs only its own results.
    search_tasks = [asyncio.ensure_future(search(q)) for q in queries]
    done, pending = await asyncio.wait(search_tasks, timeout=remaining())
    for task in pending:
        task.cancel()
    result_lists: list[list[dict]] = []
    search_errors: list[str] = []
    for q, task in zip(queries, search_tasks):
        if task not in done:
            search_errors.append(f"{q}: timed out")
            continue
        try:
            data = task.result()
        except Exception as e:  # noqa: BLE001 - one query must not sink the rest
            search_errors.append(f"{q}: {type(e).__name__}")
            continue
        if data.get("error"):
            search_errors.append(f"{q}: {data['error']}")
            continue
        result_lists.append((data.get("results") or [])[:RESULTS_PER_QUERY])
    t_search = time.monotonic()

    fused = fuse_results(result_lists, normalize)
    if not fused:
        out: dict[str, Any] = {
            "queries": queries,
            "sources": [],
            "other_results": [],
            "note": "no_results",
            "timing_ms": {
                "search": int((t_search - t0) * 1000),
                "total": int((t_search - t0) * 1000),
            },
        }
        if search_errors:
            out["errors"] = search_errors
        if search_errors and len(search_errors) == len(queries):
            out["error"] = "All searches failed: " + "; ".join(search_errors)
        return out

    pages = pick_pages(fused, max_pages)

    # 2. Read the chosen pages in parallel, each capped, all under the deadline.
    async def read(page: dict) -> None:
        key = page["_key"]
        cached = cache.get(key) if cache else None
        if cached:
            page["_text"], page["content_type"] = cached
            page["fetch"] = "cached"
            return
        try:
            text, kind = await asyncio.wait_for(
                fetch(page["url"], PAGE_TEXT_CHARS),
                timeout=max(0.5, min(page_timeout_s, remaining())),
            )
        except TimeoutError:
            page["fetch"] = "timeout"
            return
        except ScrapeError as e:
            page["fetch"] = e.code
            return
        except Exception as e:  # noqa: BLE001 - one bad page must not sink the rest
            page["fetch"] = f"error:{type(e).__name__}"
            return
        page["_text"], page["content_type"] = text, kind
        page["fetch"] = "ok"
        if cache:
            cache.put(key, text, kind)

    read_tasks = [asyncio.ensure_future(read(p)) for p in pages]
    if read_tasks and remaining() > 0:
        _, still = await asyncio.wait(read_tasks, timeout=remaining())
        for task in still:
            task.cancel()
        for page, task in zip(pages, read_tasks):
            if task in still:
                page["fetch"] = "deadline"
    t_fetch = time.monotonic()

    readable = [p for p in pages if p.get("_text")]
    rank_passages(readable, queries)
    # Most relevant sources first; a page that only had boilerplate sinks.
    readable.sort(key=lambda p: (p.get("_best", 0.0), p["_score"]), reverse=True)
    budget(readable, output_chars)

    picked_keys = {p["_key"] for p in readable}
    sources = [
        {
            "url": p["url"],
            "title": p["title"],
            "source_domain": p["source_domain"],
            "published_date": p["published_date"],
            "snippet": p["snippet"],
            "content_type": p.get("content_type", ""),
            "passages": p.get("passages") or [],
        }
        for p in readable
        if p.get("passages")
    ]
    unread = [
        {"url": p["url"], "title": p["title"], "reason": p.get("fetch", "skipped")}
        for p in pages
        if p["_key"] not in picked_keys
    ]
    other = [
        {
            "url": e["url"],
            "title": e["title"],
            "snippet": e["snippet"],
            "published_date": e["published_date"],
            "source_domain": e["source_domain"],
        }
        for e in fused
        if e["_key"] not in picked_keys
    ][:OTHER_RESULTS]

    t_end = time.monotonic()
    out = {
        "queries": queries,
        "sources": sources,
        "other_results": other,
        "unread_pages": unread,
        "timing_ms": {
            "search": int((t_search - t0) * 1000),
            "fetch": int((t_fetch - t_search) * 1000),
            "total": int((t_end - t0) * 1000),
        },
    }
    if not sources:
        out["note"] = "no_readable_pages"
    if search_errors:
        out["errors"] = search_errors
    return out
