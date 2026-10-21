"""The web-search connector.

Ported from the v1 mixin form by a codemod.

5 tools, 5 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import (
    ConnectorServer,
    InvalidURL,
    SSRFBlocked,
    declare_output_schema,
    declare_schema,
    validate_url,
)

SERVER_NAME = "web-search"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

import asyncio
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import trafilatura

from . import _research
from ._scrape import (
    BLOCKED_URL,
    EMPTY,
    FETCH_FAILED,
    PDF_NO_TEXT,
    PDF_PARSE_ERROR,
    TOO_LARGE,
    UNSUPPORTED,
    ScrapeError,
    extract_async,
    fetch_url,
    precheck_url,
)

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")

# Empty means Crawl4AI is not deployed: the JavaScript-rendering fallback is
# skipped rather than aimed at a localhost port nothing listens on. Every
# deployment that runs Crawl4AI sets this explicitly.
CRAWL4AI_URL = os.getenv("CRAWL4AI_URL", "")

# Search-flow scrape budget. The model picks max_chars per call, and a model
# that asks for a whole report puts tens of thousands of tokens of page text
# into its context, most of it unrelated to the question. WEB_RESULT_MAX_CHARS
# caps what one scrape_url call returns (return_full is not affected; KB
# ingestion has its own limits). With `focus`, up to WEB_FOCUS_READ_CHARS of
# the page is read and only the passages matching the focus come back.
WEB_RESULT_MAX_CHARS = int(os.getenv("WEB_RESULT_MAX_CHARS", "16000"))
WEB_FOCUS_READ_CHARS = int(os.getenv("WEB_FOCUS_READ_CHARS", "120000"))


def _crawl4ai_configured() -> bool:
    return bool(CRAWL4AI_URL.strip())

_MARKDOWN_GENERATOR_CONFIG = {
    "type": "DefaultMarkdownGenerator",
    "params": {
        "content_filter": {"type": "PruningContentFilter", "params": {}},
    },
}

_DOT_LINE = re.compile(r"^\s*[-*]\s*\d+\s*$")

def _strip_slider_dots(md: str) -> str:
    """Drop slider/carousel pagination-dot lines that survive extraction."""
    return "\n".join(line for line in md.splitlines() if not _DOT_LINE.match(line))

_EMPTY_BULLET = re.compile(r"^\s*[-*+]\s*$")

def _strip_empty_bullets(md: str) -> str:
    """Drop bullet-marker lines with no content after them."""
    return "\n".join(line for line in md.splitlines() if not _EMPTY_BULLET.match(line))

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

SERPER_BASE_URL = "https://google.serper.dev"

_SERPER_PAGE_SIZE = 10

_SERPER_MAX_PAGES = 3

def _serper_key(credentials: dict | None = None) -> str:
    if credentials:
        from_caller = credentials.get("serper_api_key")
        if from_caller:
            return str(from_caller)
    return os.getenv("SERPER_API_KEY", "") or SERPER_API_KEY

def _web_search_provider(credentials: dict | None = None) -> str:
    explicit = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    return "serper" if _serper_key(credentials) else "searxng"

PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")

PERPLEXITY_BASE_URL = "https://api.perplexity.ai"

DEFAULT_LANGUAGE = os.getenv("SEARXNG_DEFAULT_LANGUAGE", "en")

DEFAULT_REGION = os.getenv("SEARXNG_DEFAULT_REGION", "")

_ALLOWED_TIME_RANGES = {"day", "week", "month", "year"}

_ALLOWED_SAFE_SEARCH = {0, 1, 2}

_ALLOWED_PPLX_RECENCY = {"hour", "day", "week", "month", "year"}

_PPLX_DATE_RE = re.compile(r"^(0[1-9]|1[0-2])/(0[1-9]|[12][0-9]|3[01])/\d{4}$")

_ENGINE_FAILS: dict[str, dict[str, float]] = {}

_ENGINE_FAIL_THRESHOLD = int(os.getenv("SEARXNG_ENGINE_FAIL_THRESHOLD", "2"))

_ENGINE_FAIL_COOLDOWN_SEC = int(os.getenv("SEARXNG_ENGINE_COOLDOWN_SEC", "600"))

def _enrich_result(item: dict) -> dict:
    """Add a usable ``image_url`` and ``source_domain`` to a result dict.

    ``image_url`` is set in priority order:
      1. SearXNG's per-result ``img_src`` if non-empty (real image)
      2. SearXNG's ``thumbnail`` if non-empty
      3. Google's public favicon service for the result's domain (always
         available, free, no API key) — used as a visual floor so every
         preview card has *something* to render.

    ``source_domain`` is the host part of the URL, used by the LLM/UI
    to label the card (e.g. "khaleejtimes.com").
    """
    url = item.get("url", "")
    if not url:
        return item
    try:
        domain = urlparse(url).netloc
    except Exception:
        domain = ""
    if domain:
        item["source_domain"] = domain
        img = (item.get("img_src") or "").strip() or (item.get("thumbnail") or "").strip()
        if not img:
            img = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        item["image_url"] = img
    return item

def _finalize(
    results: list[dict], query: str, provider: str, reported_total: int | None = None
) -> dict[str, Any]:
    """Build the search_web response, marking an empty set explicitly.

    An empty list on its own reads to the agent as "no such information exists"
    and it over-abstains on findable public queries. ``note="no_results"`` lets
    the caller and the guardrail tell "the provider returned nothing" apart from
    an authoritative answer. An unreachable MCP already returns {"error": ...},
    so the three states stay distinguishable.
    """
    out: dict[str, Any] = {
        "results": results,
        "query": query,
        "total_results": (reported_total if reported_total is not None else len(results))
        if results
        else 0,
        "provider": provider,
    }
    if not results:
        out["note"] = "no_results"
    return out

def _serper_error(exc: BaseException) -> str:
    """Map a Serper failure to a message the agent can act on.

    Serper answers a bad key with 403 rather than 401, and signals an exhausted
    credit balance with 402. Both must read as configuration problems, not as
    "no results", so the agent reports a broken tool instead of concluding the
    web has nothing on the topic.
    """
    import httpx

    if isinstance(exc, httpx.ConnectError):
        return "Cannot reach Serper API."
    if isinstance(exc, httpx.TimeoutException):
        return "Serper API timed out."
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return "Invalid SERPER_API_KEY. Check the key at https://serper.dev"
    if status == 402:
        return "Serper credit balance exhausted. Top up at https://serper.dev"
    if status == 429:
        return "Serper rate limit exceeded."
    if status == 400:
        detail = ""
        try:
            detail = (exc.response.json() or {}).get("message", "")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - detail is best-effort
            detail = ""
        return f"Serper rejected the query{': ' + detail if detail else ''}."
    return f"Serper search failed: {type(exc).__name__}"

async def _serper_web_search(
    api_key: str,
    query: str,
    num_results: int,
    region: str | None,
    language: str,
    time_range: str,
    safe_search: int = 1,
) -> dict[str, Any]:
    """search_web backed by Serper, returning the SAME shape as the SearXNG path.

    Callers (the agent loop, citation chips, the UI) must not be able to tell
    which provider answered, so organic hits are mapped onto the SearXNG result
    keys and put through the same dedupe/enrich pipeline.
    """
    import httpx

    # Fall back through the configured defaults the SearXNG path already honours
    # via _prepare_language, so a deployment that sets SEARXNG_DEFAULT_REGION=ae
    # keeps getting local results after the provider switch.
    base: dict[str, Any] = {
        "q": query,
        "gl": ((region or "").strip() or DEFAULT_REGION or "us").lower(),
        "hl": ((language or "").strip() or DEFAULT_LANGUAGE or "en").lower(),
        # SearXNG grades SafeSearch 0/1/2; Google has only on or off, so both
        # moderate and strict map to active.
        "safe": "off" if safe_search == 0 else "active",
    }
    # Serper expresses recency as a qdr: code, not a named range.
    qdr = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_range)
    if qdr:
        base["tbs"] = f"qdr:{qdr}"

    # Serper echoes `num` back in searchParameters but still returns one page of
    # ten, so asking for twenty in a single call silently yields ten. Depth comes
    # from pagination instead: each page is a fresh set and costs one credit, so
    # the page count is bounded rather than driven straight off num_results.
    pages = min(max(-(-num_results // _SERPER_PAGE_SIZE), 1), _SERPER_MAX_PAGES)

    async def fetch(client: "httpx.AsyncClient", page: int) -> dict[str, Any]:
        resp = await client.post(
            f"{SERPER_BASE_URL}/search",
            json={**base, "page": page},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            got = await asyncio.gather(
                *(fetch(client, p) for p in range(1, pages + 1)), return_exceptions=True
            )
    except Exception as e:  # noqa: BLE001 - surfaced to the agent as an error result
        return {"error": _serper_error(e)}

    # Page 1 decides success. Later pages are depth, not correctness, so a
    # failure there degrades the result count instead of failing the search.
    first = got[0]
    if isinstance(first, BaseException):
        return {"error": _serper_error(first)}

    organic: list[dict] = []
    for data in got:
        if isinstance(data, BaseException) or not data:
            continue
        organic.extend(data.get("organic") or [])

    raw = [
        {
            "url": item.get("link", ""),
            "title": item.get("title", ""),
            "content": item.get("snippet", ""),
            "engine": "serper",
            "engines": ["serper"],
            "publishedDate": item.get("date", ""),
            "img_src": item.get("imageUrl", ""),
        }
        for item in organic
        if item.get("link")
    ]

    return _finalize(_build_results(raw, num_results), query, "serper", len(raw))

def _build_results(raw: list[dict], num_results: int) -> list[dict]:
    """Return deduped + enriched SearXNG results, sliced to the requested count."""
    deduped = _dedupe_results(raw)
    return [_enrich_result(r) for r in deduped[:num_results]]

def _normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        params = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if k.lower().startswith("utm_"):
                continue
            if k.lower() in {"gclid", "fbclid", "ref", "ref_src"}:
                continue
            params.append((k, v))
        new_query = urlencode(params, doseq=True)
        cleaned = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
            query=new_query,
        )
        return urlunparse(cleaned)
    except Exception:
        return ""

def _dedupe_results(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    output: list[dict] = []
    for item in results:
        key = _normalize_url(item.get("url", ""))
        if key:
            if key in seen:
                continue
            seen.add(key)
        output.append(item)
    return output

def _record_engine_fail(engine: str) -> None:
    if not engine:
        return
    now = time.time()
    entry = _ENGINE_FAILS.get(engine, {"count": 0.0, "until": 0.0})
    entry["count"] = entry.get("count", 0.0) + 1.0
    if entry["count"] >= _ENGINE_FAIL_THRESHOLD:
        entry["until"] = now + _ENGINE_FAIL_COOLDOWN_SEC
    _ENGINE_FAILS[engine] = entry

def _engine_cooldown_list() -> list[str]:
    now = time.time()
    return [name for name, v in _ENGINE_FAILS.items() if v.get("until", 0) > now]

async def _searxng_request(params: dict[str, Any]) -> dict[str, Any]:
    import httpx

    retries = 2
    backoffs = [0.3, 0.8]

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{SEARXNG_URL}/search", params=params)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                    await asyncio.sleep(backoffs[attempt])
                    continue
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            if attempt < retries:
                await asyncio.sleep(backoffs[attempt])
                continue
            raise

def _prepare_language(language: str | None, region: str | None) -> str:
    lang = (language or "").strip() or DEFAULT_LANGUAGE
    reg = (region or "").strip() or DEFAULT_REGION
    if reg and len(lang) == 2 and len(reg) == 2:
        return f"{lang.lower()}-{reg.upper()}"
    return lang

def _prepare_categories(categories: list[str] | None, default_category: str) -> str:
    if not categories:
        return default_category
    return ",".join([c.strip() for c in categories if c and c.strip()])

def _prepare_engines(engines: list[str] | None) -> str | None:
    if not engines:
        return None
    joined = ",".join([e.strip() for e in engines if e and e.strip()])
    return joined or None

def _truncate_content(content: str, max_chars: int) -> str:
    """Truncate content at the last full stop within max_chars."""
    if len(content) <= max_chars:
        return content
    cut = content.rfind(".", 0, max_chars)
    return content[: cut + 1] if cut != -1 else content[:max_chars]

_MIN_CONTENT_LENGTH = 50

class RedirectBlocked(Exception):
    """A fetch was redirected to an address the SSRF check refuses."""


def _refuse_blocked_final_url(result: dict, requested: str) -> None:
    """Refuse content from a crawl whose FINAL URL is a blocked address.

    Crawl4AI fetches and follows redirects itself, so the check on the URL we
    hand it cannot see where it ended up. The request has already happened by
    now; what this stops is the body of a metadata endpoint or an internal
    service being returned to the model as page text. The egress policy on
    the Crawl4AI pod is what stops the request itself.
    """
    for key in ("redirected_url", "url"):
        final = result.get(key)
        if isinstance(final, str) and final and final != requested:
            # A relative value is resolved against what was requested, as a
            # browser would; refusing it outright would reject a clean crawl.
            final = urljoin(requested, final)
            try:
                validate_url(final)
            except (SSRFBlocked, InvalidURL) as exc:
                logger.warning("scrape_url.final_url_blocked", target=final)
                raise RedirectBlocked(str(exc)) from exc


_RESOLVER_FLAG = "--host-resolver-rules="


def _pinning_args(url: str, extra_args: list[str] | None) -> list[str]:
    """The only Chromium flags a caller may pass: DNS pinning for THIS host.

    `extra_args` exists so KB ingestion can pin the page's host to the address
    it already resolved, closing the DNS-rebinding window. Forwarded verbatim,
    it let the model choose Chromium's resolver and proxy:
    `--host-resolver-rules=MAP example.com 169.254.169.254` passes the URL
    check and then fetches cloud metadata. So each rule must MAP the URL's own
    host to a public IP literal, and no other flag is accepted.
    """
    if not extra_args:
        return []
    host = (urlparse(url).hostname or "").lower()
    for arg in extra_args:
        if not isinstance(arg, str) or not arg.startswith(_RESOLVER_FLAG):
            raise ValueError(
                "extra_args accepts only --host-resolver-rules pinning the URL's host; "
                f"refused: {str(arg)[:80]!r}"
            )
        for rule in arg[len(_RESOLVER_FLAG):].split(","):
            parts = rule.strip().split()
            if len(parts) != 3 or parts[0].upper() != "MAP" or parts[1].lower() != host:
                raise ValueError(
                    f"extra_args may only MAP the URL's own host ({host}); refused: {rule.strip()!r}"
                )
            ip = parts[2]
            literal = f"[{ip}]" if ":" in ip else ip
            try:
                validate_url(f"http://{literal}/")
            except (SSRFBlocked, InvalidURL) as exc:
                raise ValueError(f"extra_args may not pin {host} to a blocked address: {exc}") from exc
    return list(extra_args)


async def _fetch_via_crawl4ai(url: str, max_chars: int) -> str:
    """Fallback: fetch with Crawl4AI (renders JS via headless browser).

    Skipped (returns "") when CRAWL4AI_URL is unset.
    """
    import httpx

    if not _crawl4ai_configured():
        return ""
    try:
        payload = {
            "urls": [url],
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {"cache_mode": "bypass"},
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{CRAWL4AI_URL}/crawl",
                json=payload,
            )
            if resp.status_code != 200:
                return ""
            data = resp.json()

        # Crawl4AI returns a list of results, one per URL
        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            return ""

        _refuse_blocked_final_url(results[0], url)
        content = results[0].get("markdown") or results[0].get("fit_markdown") or ""
        return _truncate_content(content, max_chars) if content else ""
    except RedirectBlocked:
        raise
    except Exception:
        return ""

async def _fetch_page_content(url: str, max_chars: int = 8000) -> tuple[str, str]:
    """Fetch a URL and extract meaningful content. Returns (content, kind).

    Direct fetch first (`_scrape.fetch_url`): every redirect hop is resolved,
    checked against the framework's blocklist and the connection pinned to the
    vetted address; servers that omit their intermediate certificate are
    recovered through a validated AIA chain; PDFs are read as text. If that
    yields too little (likely a JavaScript-rendered page), the Crawl4AI
    fallback runs. Raises ScrapeError with an actionable code otherwise.

    A blocked address never reaches the fallback: Crawl4AI's own browser would
    follow the same redirect and undo the refusal.
    """
    err: ScrapeError | None = None
    try:
        res = await fetch_url(url)
        content, kind = await extract_async(res, max_chars)
        if len(content) >= _MIN_CONTENT_LENGTH or kind != "html":
            return content, kind
        err = ScrapeError(EMPTY, f"no readable text content found at {url}")
    except ScrapeError as e:
        if e.code in (BLOCKED_URL, UNSUPPORTED, PDF_NO_TEXT, PDF_PARSE_ERROR, TOO_LARGE):
            raise
        err = e

    fallback = await _fetch_via_crawl4ai(url, max_chars)
    if len(fallback) >= _MIN_CONTENT_LENGTH:
        return fallback, "html"
    raise err

async def _fetch_via_crawl4ai_rich(
    url: str,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Crawl4AI fetch that preserves the full payload for downstream pipelines.

    The standard ``_fetch_via_crawl4ai`` returns a truncated plain-text body
    suitable for search-result excerpts. KB URL ingestion needs the full
    markdown, the parsed link graph (for BFS), the title, the status code,
    and the post-redirect final URL. This helper returns all of them
    without truncating.

    ``extra_args`` is forwarded to Chromium via ``BrowserConfig.extra_args``
    so callers can pin DNS via ``--host-resolver-rules`` for SSRF
    defense-in-depth.
    """
    import httpx

    if not _crawl4ai_configured():
        return {"success": False, "error": "crawl4ai is not configured (CRAWL4AI_URL unset)"}
    try:
        browser_params: dict[str, Any] = {"headless": True}
        if extra_args:
            browser_params["extra_args"] = extra_args
        payload = {
            "urls": [url],
            "browser_config": {"type": "BrowserConfig", "params": browser_params},
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "cache_mode": "bypass",
                    "markdown_generator": _MARKDOWN_GENERATOR_CONFIG,
                },
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{CRAWL4AI_URL}/crawl", json=payload)
            if resp.status_code != 200:
                return {"success": False, "error": f"crawl4ai returned HTTP {resp.status_code}"}
            data = resp.json()
        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            return {"success": False, "error": "crawl4ai returned no results"}
        r = results[0]

        # KB ingestion only: extract main-content markdown ourselves via
        # trafilatura on Crawl4AI's rendered HTML, instead of trusting
        # Crawl4AI's own markdown conversion. Strips nav bars, repeated
        # logos, and boilerplate far more precisely than Crawl4AI's
        # built-in filter (same technique used by the separate kb_scraper
        # tool that produced the clean reference output). Scoped to this
        # function only — the general-purpose `_fetch_via_crawl4ai` (used
        # by other agents' scrape_url calls) is untouched.
        html = r.get("cleaned_html") or r.get("html") or ""
        md = (
            trafilatura.extract(
                html,
                output_format="markdown",
                include_links=False,
                include_tables=True,
                favor_recall=True,
            )
            if html
            else None
        )
        if md:
            md = _strip_slider_dots(md)
            md = _strip_empty_bullets(md)

        if not md:
            # Fallback: Crawl4AI's own filtered/raw markdown, in case
            # trafilatura finds nothing extractable on an unusual page.
            raw_md = r.get("markdown") or ""
            md = (
                raw_md.get("fit_markdown") or raw_md.get("raw_markdown") or ""
                if isinstance(raw_md, dict)
                else raw_md
            )

        # Flatten the parsed link graph to a simple list of hrefs so the
        # KB BFS does not need to know Crawl4AI's internal shape.
        link_field = r.get("links") or {}
        links: list[str] = []
        if isinstance(link_field, dict):
            for group in ("internal", "external"):
                for item in link_field.get(group) or []:
                    href = item.get("href") if isinstance(item, dict) else item
                    if isinstance(href, str) and href.strip():
                        links.append(href.strip())

        _refuse_blocked_final_url(r, url)
        metadata = r.get("metadata") or {}
        final_url = r.get("url") or url
        return {
            "success": bool(r.get("success", True)),
            "url": final_url,
            "final_url": final_url,
            "markdown": md,
            "content": md,
            "char_count": len(md),
            "title": metadata.get("title") or "",
            "status_code": r.get("status_code"),
            "links": links,
        }
    except RedirectBlocked:
        raise
    except Exception as e:
        return {"success": False, "error": f"crawl4ai call failed: {e}"}






# credential_headers: ['X-Perplexity-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to True - POST to a search API; a search POST changes nothing.
@server.tool(
    name='perplexity_search',
    description="Search the web via Perplexity's Search API. Returns ranked results with rich pre-extracted content snippets — often eliminates the need for a follow-up scrape_url call. Supports batch queries (up to 5), domain allow/deny lists, and date-range filters.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Perplexity-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'oneOf': [{'type': 'string'},
                                        {'type': 'array',
                                         'items': {'type': 'string'},
                                         'maxItems': 5}],
                              'description': 'Search query — a single string or up to 5 '
                                             'strings for batch'},
                    'max_results': {'type': 'integer',
                                    'description': 'Max results to return (1-20, default '
                                                   '10)',
                                    'default': 10},
                    'max_tokens_per_page': {'type': 'integer',
                                            'description': 'Max tokens of content '
                                                           'extracted per page (default '
                                                           '4096)',
                                            'default': 4096},
                    'country': {'type': 'string',
                                'description': 'ISO 3166-1 alpha-2 country code, e.g. '
                                               "'US', 'GB'"},
                    'search_language_filter': {'type': 'array',
                                               'items': {'type': 'string'},
                                               'description': 'ISO 639-1 language codes to '
                                                              'restrict results to'},
                    'search_domain_filter': {'type': 'array',
                                             'items': {'type': 'string'},
                                             'description': 'Up to 20 domains. Use plain '
                                                            'domain for allowlist or '
                                                            "'-domain' prefix for denylist "
                                                            '— do not mix'},
                    'search_recency_filter': {'type': 'string',
                                              'enum': ['hour',
                                                       'day',
                                                       'week',
                                                       'month',
                                                       'year'],
                                              'description': 'Recency filter'},
                    'search_after_date_filter': {'type': 'string',
                                                 'description': 'Results published after '
                                                                'this date (MM/DD/YYYY)'},
                    'search_before_date_filter': {'type': 'string',
                                                  'description': 'Results published before '
                                                                 'this date (MM/DD/YYYY)'}},
     'required': ['query']})
@declare_output_schema({'properties': {'query': {},
                                       'results': {'items': {'type': 'object'}, 'type': 'array'},
                                       'total_results': {'type': 'integer'}},
                        'type': 'object'})
async def perplexity_search(credentials: dict[str, str],
    query: str | list[str],
    max_results: int = 10,
    max_tokens_per_page: int = 4096,
    country: str | None = None,
    search_language_filter: list[str] | None = None,
    search_domain_filter: list[str] | None = None,
    search_recency_filter: str | None = None,
    search_after_date_filter: str | None = None,
    search_before_date_filter: str | None = None,
) -> dict[str, Any]:
    import httpx

    api_key = credentials.get("perplexity_api_key") or PERPLEXITY_API_KEY
    if not api_key:
        return {
            "error": "PERPLEXITY_API_KEY not configured. "
            "Set it as an env var or provide via credentials. "
            "Get a key at https://www.perplexity.ai/settings/api"
        }

    if isinstance(query, list):
        queries = [q for q in query if isinstance(q, str) and q.strip()]
        if not queries:
            return {"error": "query array is empty"}
        if len(queries) > 5:
            return {"error": "query array supports at most 5 items"}
        payload_query: str | list[str] = queries
    else:
        if not query or not query.strip():
            return {"error": "query is required"}
        payload_query = query

    max_results = max(1, min(max_results, 20))

    payload: dict[str, Any] = {
        "query": payload_query,
        "max_results": max_results,
        "max_tokens_per_page": max_tokens_per_page,
    }
    if country:
        payload["country"] = country
    if search_language_filter:
        payload["search_language_filter"] = search_language_filter
    if search_domain_filter:
        payload["search_domain_filter"] = search_domain_filter[:20]
    if search_recency_filter:
        if search_recency_filter not in _ALLOWED_PPLX_RECENCY:
            return {
                "error": (
                    f"search_recency_filter must be one of: {sorted(_ALLOWED_PPLX_RECENCY)}"
                )
            }
        payload["search_recency_filter"] = search_recency_filter
    for name, value in (
        ("search_after_date_filter", search_after_date_filter),
        ("search_before_date_filter", search_before_date_filter),
    ):
        if value:
            if not _PPLX_DATE_RE.match(value):
                return {"error": f"{name} must be in MM/DD/YYYY format"}
            payload[name] = value

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PERPLEXITY_BASE_URL}/search",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                return {
                    "error": "Invalid PERPLEXITY_API_KEY. "
                    "Check your key at https://www.perplexity.ai/settings/api"
                }
            if resp.status_code == 429:
                return {"error": "Perplexity rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return {"error": "Cannot reach Perplexity API. Check your internet connection."}
    except Exception as e:
        return {"error": f"Perplexity search failed: {type(e).__name__}"}

    results = data.get("results", []) or []
    return {
        "query": query,
        "results": results,
        "total_results": len(results),
    }



# credential_headers: []
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to True - fetches a URL; reading a page changes no state.
@server.tool(
    name='scrape_url',
    description="Fetch a URL and extract its text content. Use this after search_web or search_news to read a specific result. Pass `focus` (what you need from the page, in a few words) to get only the matching sections of a long page or PDF instead of its first pages. Pass return_full=true for KB-style ingestion (full markdown, link graph, title, status).",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'url': {'type': 'string', 'description': 'The URL to scrape'},
                    'focus': {'type': 'string',
                              'description': 'What you need from the page, in a few '
                                             'words (for example the entity and the '
                                             'fact). The whole page is read and only '
                                             'the sections that match are returned, '
                                             'in page order. Ignored when '
                                             'return_full=true.'},
                    'max_chars': {'type': 'integer',
                                  'description': 'Max characters to return, stopping at '
                                                 'the last full stop (default 8000, '
                                                 'capped by the deployment). Ignored when '
                                                 'return_full=true.',
                                  'default': 8000},
                    'return_full': {'type': 'boolean',
                                    'description': 'When true, return the full markdown '
                                                   'plus parsed links, title, status code, '
                                                   'and final post-redirect URL. Used by '
                                                   'KB URL ingestion to enable multi-page '
                                                   'BFS crawl. Defaults to false for '
                                                   'backwards compatibility with search '
                                                   'flows.',
                                    'default': False},
                    'extra_args': {'type': 'array',
                                   'items': {'type': 'string'},
                                   'description': 'Optional Chromium flags forwarded to '
                                                  "Crawl4AI's BrowserConfig.extra_args. "
                                                  'Used by KB ingestion to pin DNS via '
                                                  '--host-resolver-rules as SSRF '
                                                  'defense-in-depth. Ignored when '
                                                  'return_full=false.'}},
     'required': ['url']})
@declare_output_schema({'properties': {'char_count': {'type': 'integer'},
                                       'content': {'type': 'string'},
                                       'content_type': {'type': 'string'},
                                       'error_code': {'type': 'string'},
                                       'final_url': {'type': 'string'},
                                       'focused': {'type': 'boolean'},
                                       'source_char_count': {'type': 'integer'},
                                       'links': {'items': {'type': 'string'}, 'type': 'array'},
                                       'markdown': {'type': 'string'},
                                       'status_code': {'type': 'integer'},
                                       'title': {'type': 'string'},
                                       'url': {'type': 'string'}},
                        'type': 'object'})
async def scrape_url(credentials: dict[str, str],
    url: str,
    max_chars: int = 8000,
    return_full: bool = False,
    extra_args: list[str] | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    # The URL comes from the model, so it is the connector's job to refuse an
    # address inside the trust boundary. Without this, a prompt injection in
    # any page the agent had already read could aim it at cloud instance
    # metadata or at the platform's own API, and the body came back as page
    # text. Both branches below are covered because only checking the one
    # with a test is how this kind of gap survives.
    # Scheme, localhost and single-label in-cluster names (api, vault) are
    # refused before any DNS lookup: a resolver inside a cluster answers them.
    try:
        precheck_url(url)
        validate_url(url)
    except ScrapeError as exc:
        logger.warning("scrape_url.blocked", url=url)
        return {
            "error": f"Refused to scrape URL: address is blocked ({exc.message})",
            "error_code": exc.code,
        }
    except SSRFBlocked as exc:
        logger.warning("scrape_url.blocked", url=url)
        return {"error": f"Refused to scrape URL: address is blocked ({exc})", "error_code": BLOCKED_URL}
    except InvalidURL as exc:
        return {"error": f"Refused to scrape URL: {exc}", "error_code": FETCH_FAILED}

    try:
        pinned = _pinning_args(url, extra_args)
    except ValueError as exc:
        return {"error": f"Refused to scrape URL: {exc}"}

    if return_full:
        # KB-style rich fetch: full markdown + links + title + status.
        # No truncation here; callers (kb-worker) enforce their own
        # max-content-MB ceiling downstream.
        try:
            result = await _fetch_via_crawl4ai_rich(url, extra_args=pinned)
        except RedirectBlocked as exc:
            return {"error": f"Refused to scrape URL: redirected to a blocked address ({exc})"}
        if not result.get("success"):
            return {"error": f"Failed to scrape URL: {result.get('error', 'unknown error')}"}
        return result

    # Search-result path: same input and success shape as before; the extra
    # "content_type" key is additive. A failure carries an "error_code" the
    # agent can act on (tls_error, http_403, timeout, pdf_no_text, ...).
    try:
        limit = max(1, min(int(max_chars or 8000), WEB_RESULT_MAX_CHARS))
    except (TypeError, ValueError):
        limit = min(8000, WEB_RESULT_MAX_CHARS)
    focus = (focus or "").strip()
    read_chars = max(limit, WEB_FOCUS_READ_CHARS) if focus else limit
    try:
        content, kind = await _fetch_page_content(url, read_chars)
    except RedirectBlocked as exc:
        return {
            "error": f"Refused to scrape URL: redirected to a blocked address ({exc})",
            "error_code": BLOCKED_URL,
        }
    except ScrapeError as e:
        if e.code == BLOCKED_URL:
            return {
                "error": f"Refused to scrape URL: address is blocked ({e.message})",
                "error_code": e.code,
            }
        return {"error": f"Failed to scrape URL ({e.code}): {e.message}", "error_code": e.code}

    if not focus:
        return {
            "url": url,
            "content": content,
            "char_count": len(content),
            "content_type": kind,
        }
    selected, matched = _research.focus_text(content, focus, limit)
    return {
        "url": url,
        "content": selected,
        "char_count": len(selected),
        "content_type": kind,
        "focused": matched,
        "source_char_count": len(content),
    }



async def run_web_search(
    credentials: dict[str, str],
    query: str,
    num_results: int = 10,
    language: str = "en",
    region: str | None = None,
    time_range: str = "",
    safe_search: int = 1,
    engines: list[str] | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """search_web's provider dispatch, shared with web_research."""
    # Empty / invalid time_range means ALL-TIME (no recency filter). Only an
    # explicit day/week/month/year narrows results. Defaulting to a recency
    # filter silently hides evergreen pages (profiles, company info, docs).:
    if time_range not in _ALLOWED_TIME_RANGES:
        time_range = ""
    if safe_search not in _ALLOWED_SAFE_SEARCH:
        safe_search = 1

    # Serper is primary whenever a key is configured; SearXNG stays as the
    # fallback for deployments without one. Same return shape either way.
    #
    # safe_search and region are forwarded rather than dropped: region is
    # what makes a deployment outside the US return local sources instead of
    # US ones, and silently ignoring a caller's SafeSearch on the provider
    # that replaced SearXNG would be a quiet regression.
    serper_key = _serper_key(credentials)
    if _web_search_provider(credentials) == "serper" and serper_key:
        return await _serper_web_search(
            serper_key,
            query,
            num_results,
            region,
            language,
            time_range,
            safe_search,
        )

    lang = _prepare_language(language, region)
    category_param = _prepare_categories(categories, "general")
    engine_param = _prepare_engines(engines)

    params = {
        "q": query,
        "format": "json",
        "categories": category_param,
        "language": lang,
        "safesearch": safe_search,
    }
    if time_range:
        params["time_range"] = time_range
    if engine_param:
        params["engines"] = engine_param
    cooldown_engines = _engine_cooldown_list()
    if cooldown_engines:
        params["disabled_engines"] = ",".join(cooldown_engines)
    try:
        data = await _searxng_request(params)
    except Exception as e:
        if "ConnectError" in type(e).__name__:
            return {
                "error": (
                    f"SearXNG not reachable at {SEARXNG_URL}. Point SEARXNG_URL at a "
                    "SearXNG instance with the JSON output format enabled, or set "
                    "SERPER_API_KEY to search through Serper instead."
                )
            }
        return {"error": f"Search failed: {str(e)}"}

    unresponsive = data.get("unresponsive_engines") or data.get("errors") or []
    if isinstance(unresponsive, list):
        for engine_name in unresponsive:
            if isinstance(engine_name, str):
                _record_engine_fail(engine_name)

    results = _build_results(data.get("results", []), num_results)
    return _finalize(results, query, "searxng", data.get("number_of_results"))



# credential_headers: ['X-Serper-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: _serper_web_search() POSTs a query, or _searxng_request() GETs one.
# A search that uses POST is still a read: the only state it touches
# is an in-process _ENGINE_FAILS dict that is never persisted.
@server.tool(
    name='search_web',
    description="Search the web using a self-hosted SearXNG metasearch engine. Aggregates results from Google, Bing, DuckDuckGo and more — no API key needed. Use scrape_url to fetch full content from any result URL.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Serper-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string', 'description': 'Search query'},
                    'num_results': {'type': 'integer',
                                    'description': 'Maximum number of results to return '
                                                   '(default 10, max 50)',
                                    'default': 10},
                    'language': {'type': 'string',
                                 'description': "Language code for results, e.g. 'en', "
                                                "'de', 'fr' (default 'en')",
                                 'default': 'en'},
                    'region': {'type': 'string',
                               'description': 'Region code to pair with language, e.g. '
                                              "'US' (optional)"},
                    'time_range': {'type': 'string',
                                   'description': 'Recency filter: day, week, month, or '
                                                  'year. Omit (default) for ALL-TIME '
                                                  'results - use a range ONLY for '
                                                  'time-sensitive queries like latest '
                                                  'news. For '
                                                  'person/company/definition/evergreen '
                                                  'lookups do NOT set this, or good older '
                                                  'pages get filtered out.',
                                   'default': ''},
                    'safe_search': {'type': 'integer',
                                    'description': 'Safe search level: 0 (off), 1 '
                                                   '(moderate), 2 (strict) (default 1)',
                                    'default': 1},
                    'engines': {'type': 'array',
                                'items': {'type': 'string'},
                                'description': 'Optional list of engines to use (reduces '
                                               'requests when set)'},
                    'categories': {'type': 'array',
                                   'items': {'type': 'string'},
                                   'description': 'Optional list of categories to use '
                                                  '(default: general)'}},
     'required': ['query']})
@declare_output_schema({'description': 'Web search results',
                        'properties': {'note': {'type': 'string'},
                                       'provider': {'type': 'string'},
                                       'query': {'type': 'string'},
                                       'results': {'type': 'array'},
                                       'total_results': {'type': 'integer'}},
                        'type': 'object'})
async def search_web(credentials: dict[str, str],
    query: str,
    num_results: int = 10,
    language: str = "en",
    region: str | None = None,
    time_range: str = "",
    safe_search: int = 1,
    engines: list[str] | None = None,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    return await run_web_search(
        credentials,
        query,
        num_results,
        language,
        region,
        time_range,
        safe_search,
        engines,
        categories,
    )



# credential_headers: ['X-Serper-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False. Every query goes through
# run_web_search (a search read) and every page through _fetch_page_content
# (GET only, SSRF-guarded); the only state touched is an in-process page cache.
@server.tool(
    name='web_research',
    description=(
        "Research a question on the web in ONE call: runs up to 5 search queries in "
        "parallel, reads the best few pages in parallel (PDFs and official sites "
        "included), and returns the most relevant passages from each page with its URL, "
        "title and date, plus snippets of other results. Prefer this over search_web + "
        "scrape_url. Put every entity and angle you need into the queries list (one query "
        "per distinct sub-question, e.g. one per organisation), then answer from the "
        "returned passages. Fast: returns within about 8 seconds with whatever is ready."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Serper-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'queries': {'type': 'array',
                                'items': {'type': 'string'},
                                'minItems': 1,
                                'maxItems': 5,
                                'description': '1 to 5 specific search queries covering '
                                               'everything needed.'},
                    'max_pages': {'type': 'integer',
                                  'description': 'How many pages to read (default 5, max 8).',
                                  'default': 5},
                    'region': {'type': 'string',
                               'description': "Optional two-letter region for local "
                                              "results, e.g. 'ae'."},
                    'language': {'type': 'string',
                                 'description': "Language code for results (default 'en').",
                                 'default': 'en'},
                    'time_range': {'type': 'string',
                                   'description': 'Recency filter: day, week, month or '
                                                  'year. Omit for all-time results; set it '
                                                  'only for news-type questions.',
                                   'default': ''}},
     'required': ['queries']})
@declare_output_schema({'properties': {'queries': {'type': 'array'},
                                       'sources': {'type': 'array',
                                                   'description': 'Pages read, most relevant '
                                                                  'first: url, title, '
                                                                  'source_domain, '
                                                                  'published_date, snippet, '
                                                                  'content_type, passages[].'},
                                       'other_results': {'type': 'array',
                                                         'description': 'Further results not '
                                                                        'read: url, title, '
                                                                        'snippet, '
                                                                        'published_date.'},
                                       'unread_pages': {'type': 'array'},
                                       'timing_ms': {'type': 'object'},
                                       'note': {'type': 'string'},
                                       'errors': {'type': 'array'}},
                        'type': 'object'})
async def web_research(credentials: dict[str, str],
    queries: list[str],
    max_pages: int = _research.DEFAULT_MAX_PAGES,
    region: str | None = None,
    language: str = "en",
    time_range: str = "",
) -> dict[str, Any]:
    if isinstance(queries, str):
        queries = [queries]
    if time_range not in _ALLOWED_TIME_RANGES:
        time_range = ""

    async def search(query: str) -> dict[str, Any]:
        return await run_web_search(
            credentials,
            query,
            num_results=_research.RESULTS_PER_QUERY,
            language=language,
            region=region,
            time_range=time_range,
        )

    return await _research.research(
        list(queries or []),
        search=search,
        fetch=_fetch_page_content,
        normalize=_normalize_url,
        max_pages=max_pages,
    )



# credential_headers: ['X-Serper-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint corrected to True - POST to a search API; a search POST changes nothing.
@server.tool(
    name='serper_search',
    description="Search Google via the Serper API (paid, fast, reliable). Returns rich results including organic results, knowledge graph, and people also ask. Requires SERPER_API_KEY env var. Use search_web for free SearXNG-based search.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Serper-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string', 'description': 'Search query'},
                    'search_type': {'type': 'string',
                                    'enum': ['search',
                                             'news',
                                             'images',
                                             'videos',
                                             'scholar'],
                                    'description': 'Type of search (default: search)',
                                    'default': 'search'},
                    'num_results': {'type': 'integer',
                                    'description': 'Number of results to return (default '
                                                   '10, max 100)',
                                    'default': 10},
                    'country': {'type': 'string',
                                'description': "Country code for results, e.g. 'us', 'in', "
                                               "'gb' (default 'us')",
                                'default': 'us'},
                    'language': {'type': 'string',
                                 'description': "Language code, e.g. 'en', 'hi', 'de' "
                                                "(default 'en')",
                                 'default': 'en'},
                    'location': {'type': 'string',
                                 'description': "Specific location for results, e.g. 'New "
                                                "York' (optional)"}},
     'required': ['query']})
@declare_output_schema({'properties': {'credits_used': {'type': 'integer'},
                                       'knowledge_graph': {'type': 'object'},
                                       'organic': {'items': {'type': 'object'}, 'type': 'array'},
                                       'people_also_ask': {'items': {'type': 'object'}, 'type': 'array'},
                                       'query': {'type': 'string'},
                                       'related_searches': {'items': {'type': 'object'}, 'type': 'array'}},
                        'type': 'object'})
async def serper_search(credentials: dict[str, str],
    query: str,
    search_type: str = "search",
    num_results: int = 10,
    country: str = "us",
    language: str = "en",
    location: str | None = None,
) -> dict[str, Any]:
    import httpx

    api_key = _serper_key(credentials)
    if not api_key:
        return {
            "error": "SERPER_API_KEY not configured. "
            "Set it as an env var or provide via credentials. "
            "Get a free key at https://serper.dev (2,500 free credits)."
        }

    endpoint = f"{SERPER_BASE_URL}/{search_type}"
    payload: dict[str, Any] = {
        "q": query,
        "gl": country,
        "hl": language,
        "num": min(num_results, 100),
    }
    if location:
        payload["location"] = location

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={
                    "X-API-KEY": api_key,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                return {"error": "Invalid SERPER_API_KEY. Check your key at https://serper.dev"}
            if resp.status_code == 429:
                return {"error": "Serper rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return {"error": "Cannot reach Serper API. Check your internet connection."}
    except Exception as e:
        return {"error": f"Serper search failed: {type(e).__name__}"}

    result: dict[str, Any] = {"query": query}

    if "organic" in data:
        result["organic"] = data["organic"][:num_results]
    if "news" in data:
        result["news"] = data["news"][:num_results]
    if "images" in data:
        result["images"] = data["images"][:num_results]
    if "videos" in data:
        result["videos"] = data["videos"][:num_results]
    if "knowledgeGraph" in data:
        result["knowledge_graph"] = data["knowledgeGraph"]
    if "peopleAlsoAsk" in data:
        result["people_also_ask"] = data["peopleAlsoAsk"]
    if "relatedSearches" in data:
        result["related_searches"] = data["relatedSearches"]
    if "credits" in data:
        result["credits_used"] = data["credits"]

    return result
