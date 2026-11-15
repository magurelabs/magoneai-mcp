"""The twitter-intel connector.

**Unofficial provider.** This connector calls twitterapi.io, an unofficial
third-party service, not the official X API. Neither this connector nor that
provider is affiliated with or endorsed by X Corp. If you run it, you are
responsible for complying with the terms of X and of twitterapi.io. It is
blocked from publication in `connectors/publication.json`.

Ported from the v1 mixin form by a codemod.

4 tools, 4 of them proposed read-only. Every input schema
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

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

SERVER_NAME = "twitter-intel"
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
from datetime import datetime, timezone
from typing import Any
import httpx
import re
import sys
from pathlib import Path
from .client import TwitterAPIError

BASE_URL = "https://api.twitterapi.io"

REQUEST_TIMEOUT = 30.0

RATE_LIMIT_RETRY_DELAY = 5.0

PAGE_SIZE = 20

MAX_TWEETS_PER_CALL = 100

_CREATED_AT_FORMAT = "%a %b %d %H:%M:%S %z %Y"

def resolve_api_key(credentials: dict | None) -> str:
    """Credential from the platform/playground wins; env var is the fallback."""
    if credentials:
        from_caller = credentials.get("twitterapiio_api_key")
        if from_caller:
            return str(from_caller)
    return os.getenv("TWITTERAPIIO_API_KEY", "")

def parse_created_at(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _CREATED_AT_FORMAT)
    except (ValueError, TypeError):
        return None

def normalize_tweet(tweet: dict) -> dict:
    """Flatten a raw twitterapi.io tweet into the shape all tools return."""
    author = tweet.get("author") or {}
    return {
        "id": tweet.get("id", ""),
        "url": tweet.get("url", ""),
        "author": author.get("userName", ""),
        "author_name": author.get("name", ""),
        "author_followers": author.get("followers", 0),
        "text": tweet.get("text", ""),
        "created_at": tweet.get("createdAt", ""),
        "likes": tweet.get("likeCount", 0),
        "retweets": tweet.get("retweetCount", 0),
        "replies": tweet.get("replyCount", 0),
        "quotes": tweet.get("quoteCount", 0),
        "views": tweet.get("viewCount", 0),
        "is_retweet": tweet.get("retweeted_tweet") is not None,
        "is_reply": tweet.get("isReply", False),
        "lang": tweet.get("lang", ""),
    }

def sort_newest_first(tweets: list[dict]) -> list[dict]:
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        tweets,
        key=lambda t: parse_created_at(t.get("created_at", "")) or epoch,
        reverse=True,
    )

def time_window_operators(hours_back: float) -> str:
    """since_time:/until_time: operators for a trailing window, or '' for all-time."""
    if hours_back <= 0:
        return ""
    now = datetime.now(timezone.utc).timestamp()
    return f"since_time:{int(now - hours_back * 3600)} until_time:{int(now)}"

def _error_message(status_code: int, body: str) -> str:
    if status_code == 401:
        return "twitterapi.io rejected the API key. Check the key in your credentials."
    if status_code == 402:
        return "twitterapi.io account has insufficient credits. Top up at twitterapi.io."
    if status_code == 429:
        return "twitterapi.io rate limit hit. Retry in a moment."
    return f"twitterapi.io error {status_code}: {body[:300]}"

async def _twitter_get_page(api_key: str, path: str, params: dict[str, Any]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"X-API-Key": api_key},
        )
        # twitterapi.io enforces a per-second QPS limit, so two tool calls
        # in quick succession can 429. One paced retry absorbs that burst
        # case; a sustained limit still surfaces as an error.
        if response.status_code == 429:
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            response = await client.get(
                f"{BASE_URL}{path}",
                params=params,
                headers={"X-API-Key": api_key},
            )
    if response.status_code != 200:
        raise TwitterAPIError(_error_message(response.status_code, response.text))
    data = response.json()
    # Some endpoints report failures inside a 200 body.
    if isinstance(data, dict) and data.get("status") == "error":
        raise TwitterAPIError(f"twitterapi.io error: {data.get('msg', 'unknown error')}")
    return data

async def _twitter_fetch_tweets(api_key: str,
    path: str,
    params: dict[str, Any],
    max_results: int,
    items_keys: tuple[str, ...] = ("tweets",),
) -> list[dict]:
    """Follow cursor pagination until max_results tweets or pages run out.

    Returns raw tweet dicts (not normalized) so callers can post-filter
    on fields the normalized shape drops.
    """
    max_results = max(1, min(max_results, MAX_TWEETS_PER_CALL))
    max_pages = -(-max_results // PAGE_SIZE)  # ceil

    collected: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        data = await _twitter_get_page(api_key, path, page_params)

        items: list[dict] = []
        for key in items_keys:
            found = data.get(key)
            if isinstance(found, list):
                items = found
                break
        collected.extend(items)

        if len(collected) >= max_results:
            break
        if not (data.get("has_next_page") and data.get("next_cursor")):
            break
        cursor = data["next_cursor"]

    return collected[:max_results]


ADVANCED_SEARCH_PATH = "/twitter/tweet/advanced_search"

TWEETS_BY_ID_PATH = "/twitter/tweets"

TWEET_REPLIES_PATH = "/twitter/tweet/replies"

CREDENTIAL_HEADER = "X-TwitterApiIo-Api-Key"

DEFAULT_ACCOUNTS_HEADER = "X-Default-Accounts"

_MISSING_KEY_ERROR = (
    "No twitterapi.io API key configured. Provide it via credentials "
    "or the TWITTERAPIIO_API_KEY env var. Get one at twitterapi.io/dashboard."
)

_ACCOUNTS_PER_QUERY = 10

LENS_TOPICS = {
    "tech": (
        '("AI agents" OR "agentic AI" OR LLM OR "large language model" '
        'OR "foundation model" OR "generative AI" OR "machine learning" OR AGI)'
    ),
    "business": (
        '("enterprise AI" OR "AI adoption" OR "AI strategy" OR "AI transformation" '
        'OR "AI ROI" OR "AI investment" OR "future of work")'
    ),
    "marketing": (
        '("AI marketing" OR "AI for marketers" OR "AI ads" OR "AI content" '
        'OR "marketing with AI" OR "brand strategy" AI OR "personalization" AI)'
    ),
}

DEFAULT_LENS = "tech"

async def _search(api_key: str, query: str, query_type: str, max_results: int
) -> list[dict]:
    raw = await _twitter_fetch_tweets(
        api_key,
        ADVANCED_SEARCH_PATH,
        {"query": query, "queryType": query_type},
        max_results,
    )
    return [normalize_tweet(t) for t in raw]



# credential_headers: ['X-TwitterApiIo-Api-Key', 'X-Default-Accounts']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: _twitter_get_page() -> httpx.get against the advanced-search path.
# The module contains no POST, PUT, PATCH or DELETE at all.
@server.tool(
    name='get_account_tweets',
    description="Latest tweets from one or more accounts (competitors, thought leaders), grouped per account. Multiple accounts are batched into single search calls, so monitoring a list is cheap. Call this on a schedule with hours_back matching the schedule interval to monitor accounts. Results are newest-first across the whole batch, so a high-volume account (e.g. a news wire) can crowd quieter accounts out of max_results — query firehose accounts separately from occasional posters.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-TwitterApiIo-Api-Key', 'X-Default-Accounts'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'accounts': {'type': 'array',
                                 'items': {'type': 'string'},
                                 'description': 'Twitter handles without @, e.g. '
                                                "['openai', 'anthropicai']. Omit to use "
                                                'the Default Accounts list configured on '
                                                'the platform connection.'},
                    'hours_back': {'type': 'number',
                                   'description': 'Only tweets from the last N hours '
                                                  '(default 24, 0 = no limit)',
                                   'default': 24},
                    'include_retweets': {'type': 'boolean',
                                         'description': "Include the accounts' retweets "
                                                        '(default true)',
                                         'default': True},
                    'include_replies': {'type': 'boolean',
                                        'description': "Include the accounts' replies "
                                                       '(default false)',
                                        'default': False},
                    'max_results': {'type': 'integer',
                                    'description': 'Maximum tweets per batch of 10 '
                                                   'accounts (default 50, max 100)',
                                    'default': 50}}})
@declare_output_schema({'properties': {'accounts': {'description': "Handle (lowercase) -> list of that account's "
                                                                   'tweets, newest first',
                                                    'type': 'object'},
                                       'accounts_with_no_tweets': {'type': 'array'},
                                       'total_results': {'type': 'integer'}},
                        'type': 'object'})
async def get_account_tweets(credentials: dict[str, str],
    accounts: list[str] | None = None,
    hours_back: float = 24,
    include_retweets: bool = True,
    include_replies: bool = False,
    max_results: int = 50,
) -> dict[str, Any]:
    api_key = resolve_api_key(credentials)
    if not api_key:
        return {"error": _MISSING_KEY_ERROR}

    # No explicit accounts -> the platform-configured default list, which
    # arrives as a comma-separated string via the X-Default-Accounts header
    # (a sensitive=False auth field stored in the platform DB).
    if not accounts:
        configured = (credentials or {}).get("default_accounts", "")
        accounts = re.split(r"[,\s]+", configured) if configured else []
    if not accounts:
        return {
            "error": (
                "No accounts given and no Default Accounts configured on the "
                "connection. Pass accounts=[...] or set the default list in "
                "the platform connection settings."
            )
        }

    handles = [a.strip().lstrip("@").lower() for a in accounts if a and a.strip().lstrip("@")]
    if not handles:
        return {"error": "No valid account handles provided."}
    handles = list(dict.fromkeys(handles))  # dedupe, keep order

    window = time_window_operators(hours_back)
    all_tweets: list[dict] = []
    try:
        for start in range(0, len(handles), _ACCOUNTS_PER_QUERY):
            chunk = handles[start : start + _ACCOUNTS_PER_QUERY]
            froms = " OR ".join(f"from:{h}" for h in chunk)
            query = f"({froms})"
            if include_retweets:
                query += " include:nativeretweets"
            if not include_replies:
                query += " -filter:replies"
            if window:
                query += f" {window}"
            all_tweets.extend(await _search(api_key, query, "Latest", max_results))
    except TwitterAPIError as e:
        return {"error": str(e)}

    grouped: dict[str, list[dict]] = {h: [] for h in handles}
    for tweet in sort_newest_first(all_tweets):
        handle = tweet.get("author", "").lower()
        # A retweet surfaces under the retweeting account's handle, which is
        # the watched one, so this bucket assignment holds for both cases.
        if handle in grouped:
            grouped[handle].append(tweet)

    return {
        "accounts": grouped,
        "total_results": len(all_tweets),
        "accounts_with_no_tweets": [h for h, ts in grouped.items() if not ts],
    }



# credential_headers: ['X-TwitterApiIo-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: _twitter_get_page() -> httpx.get against the advanced-search path.
# The module contains no POST, PUT, PATCH or DELETE at all.
@server.tool(
    name='get_tweet_thread',
    description="Fetch a single tweet plus its replies — for deep-diving a competitor announcement or a thought leader's thread found via the search tools.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-TwitterApiIo-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'tweet_id': {'type': 'string',
                                 'description': 'The tweet ID (the number at the end of a '
                                                'tweet URL)'},
                    'max_replies': {'type': 'integer',
                                    'description': 'Maximum replies to fetch (default 20, '
                                                   'max 100)',
                                    'default': 20}},
     'required': ['tweet_id']})
@declare_output_schema({'properties': {'replies': {'description': 'Replies, newest first', 'type': 'array'},
                                       'total_replies': {'type': 'integer'},
                                       'tweet': {'description': 'The requested tweet', 'type': 'object'}},
                        'type': 'object'})
async def get_tweet_thread(credentials: dict[str, str],
    tweet_id: str,
    max_replies: int = 20,
) -> dict[str, Any]:
    api_key = resolve_api_key(credentials)
    if not api_key:
        return {"error": _MISSING_KEY_ERROR}
    tweet_id = tweet_id.strip()
    if not tweet_id.isdigit():
        return {"error": f"Invalid tweet_id '{tweet_id}': expected a numeric ID."}

    try:
        found = await _twitter_fetch_tweets(
            api_key, TWEETS_BY_ID_PATH, {"tweet_ids": tweet_id}, max_results=1
        )
        if not found:
            return {"error": f"Tweet {tweet_id} not found."}
        # The replies endpoint has been seen keying its list as either
        # "tweets" or "replies"; accept both.
        raw_replies = await _twitter_fetch_tweets(
            api_key,
            TWEET_REPLIES_PATH,
            {"tweetId": tweet_id},
            max_results=max_replies,
            items_keys=("tweets", "replies"),
        )
    except TwitterAPIError as e:
        return {"error": str(e)}

    replies = sort_newest_first([normalize_tweet(t) for t in raw_replies])
    return {
        "tweet": normalize_tweet(found[0]),
        "replies": replies,
        "total_replies": len(replies),
    }



# credential_headers: ['X-TwitterApiIo-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: _twitter_get_page() -> httpx.get against the advanced-search path.
# The module contains no POST, PUT, PATCH or DELETE at all.
@server.tool(
    name='search_ai_trends',
    description="High-engagement tweets about AI trends through a chosen lens — tech (models/tooling), business (enterprise adoption, analyst calls), or marketing (AI in marketing/ads/content) — or a custom topic. Filtered to tweets above an engagement threshold and ranked by engagement. Good for 'what's the AI world talking about today'.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-TwitterApiIo-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'lens': {'type': 'string',
                             'enum': ['tech', 'business', 'marketing'],
                             'description': 'Curated topic set to use: tech = '
                                            'models/tooling/research, business = '
                                            'enterprise adoption/strategy/analyst calls, '
                                            'marketing = AI in marketing/ads/content '
                                            '(default tech). Ignored when topic is set.',
                             'default': 'tech'},
                    'topic': {'type': 'string',
                              'description': "Optional custom topic, e.g. 'multimodal "
                                             "models' or 'AI coding agents'. Overrides "
                                             'lens when set.',
                              'default': ''},
                    'min_likes': {'type': 'integer',
                                  'description': 'Minimum likes for a tweet to qualify '
                                                 '(default 100)',
                                  'default': 100},
                    'hours_back': {'type': 'number',
                                   'description': 'Look-back window in hours (default 24)',
                                   'default': 24},
                    'language': {'type': 'string',
                                 'description': "Language filter (default 'en', '' = any)",
                                 'default': 'en'},
                    'max_results': {'type': 'integer',
                                    'description': 'Maximum tweets to return (default 30, '
                                                   'max 100)',
                                    'default': 30}}})
@declare_output_schema({'properties': {'query': {'type': 'string'},
                                       'total_results': {'type': 'integer'},
                                       'tweets': {'description': 'Tweets sorted by likes, highest first',
                                                  'type': 'array'}},
                        'type': 'object'})
async def search_ai_trends(credentials: dict[str, str],
    lens: str = DEFAULT_LENS,
    topic: str = "",
    min_likes: int = 100,
    hours_back: float = 24,
    language: str = "en",
    max_results: int = 30,
) -> dict[str, Any]:
    api_key = resolve_api_key(credentials)
    if not api_key:
        return {"error": _MISSING_KEY_ERROR}

    topic = topic.strip()
    if topic:
        # Quote a plain multi-word phrase; leave operator-bearing input as-is.
        has_operators = any(ch in topic for ch in '"():') or " OR " in topic
        subject = topic if has_operators or " " not in topic else f'"{topic}"'
    else:
        subject = LENS_TOPICS.get(lens, LENS_TOPICS[DEFAULT_LENS])

    parts = [subject, "-filter:replies"]
    if min_likes > 0:
        parts.append(f"min_faves:{min_likes}")
    if language:
        parts.append(f"lang:{language}")
    window = time_window_operators(hours_back)
    if window:
        parts.append(window)
    query = " ".join(parts)

    try:
        tweets = await _search(api_key, query, "Top", max_results)
    except TwitterAPIError as e:
        return {"error": str(e)}

    tweets.sort(key=lambda t: t.get("likes", 0), reverse=True)
    return {"tweets": tweets, "total_results": len(tweets), "query": query}



# credential_headers: ['X-TwitterApiIo-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: _twitter_get_page() -> httpx.get against the advanced-search path.
# The module contains no POST, PUT, PATCH or DELETE at all.
@server.tool(
    name='search_tweets',
    description="Search tweets with Twitter advanced-search syntax (from:, min_faves:, lang:, exact phrases, OR, -filter:replies, etc.). The general-purpose tool when the curated ones don't fit. Costs scale with tweets returned, so keep max_results modest.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-TwitterApiIo-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': "Advanced search query, e.g. 'from:openai' or "
                                             '\'"AI agents" min_faves:500 lang:en\''},
                    'query_type': {'type': 'string',
                                   'enum': ['Latest', 'Top'],
                                   'description': 'Latest = chronological, Top = highest '
                                                  'engagement (default Latest)',
                                   'default': 'Latest'},
                    'hours_back': {'type': 'number',
                                   'description': 'Only tweets from the last N hours. 0 '
                                                  '(default) = no time filter. Ignored if '
                                                  'the query already contains since/until '
                                                  'operators.',
                                   'default': 0},
                    'max_results': {'type': 'integer',
                                    'description': 'Maximum tweets to return (default 30, '
                                                   'max 100)',
                                    'default': 30}},
     'required': ['query']})
@declare_output_schema({'properties': {'query': {'description': 'The final query sent to the API', 'type': 'string'},
                                       'total_results': {'type': 'integer'},
                                       'tweets': {'description': 'Normalized tweets, newest first', 'type': 'array'}},
                        'type': 'object'})
async def search_tweets(credentials: dict[str, str],
    query: str,
    query_type: str = "Latest",
    hours_back: float = 0,
    max_results: int = 30,
) -> dict[str, Any]:
    api_key = resolve_api_key(credentials)
    if not api_key:
        return {"error": _MISSING_KEY_ERROR}
    if query_type not in ("Latest", "Top"):
        query_type = "Latest"

    final_query = query.strip()
    # A caller-supplied since/until wins over hours_back.
    if hours_back > 0 and "since" not in final_query and "until" not in final_query:
        final_query = f"{final_query} {time_window_operators(hours_back)}"

    try:
        tweets = await _search(api_key, final_query, query_type, max_results)
    except TwitterAPIError as e:
        return {"error": str(e)}

    return {
        "tweets": sort_newest_first(tweets),
        "total_results": len(tweets),
        "query": final_query,
    }
