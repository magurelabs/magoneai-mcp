"""Thin async client for the twitterapi.io REST API.

Centralizes the base URL, auth header, cursor pagination, and error
mapping so tool code deals only in normalized tweet dicts.

twitterapi.io bills per tweet returned, so every fetch goes through a
hard page cap — callers say how many tweets they want and the client
never fetches more pages than needed to satisfy that.
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import httpx

BASE_URL = "https://api.twitterapi.io"
REQUEST_TIMEOUT = 30.0
RATE_LIMIT_RETRY_DELAY = 5.0

# advanced_search returns ~20 tweets per page. The absolute ceiling keeps a
# runaway max_results from turning one tool call into a large bill.
PAGE_SIZE = 20
MAX_TWEETS_PER_CALL = 100

# twitterapi.io returns createdAt in classic Twitter format,
# e.g. "Tue Dec 10 07:00:30 +0000 2024".
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


class TwitterAPIError(Exception):
    """Raised on any non-success response; message is user-presentable."""


def _error_message(status_code: int, body: str) -> str:
    if status_code == 401:
        return "twitterapi.io rejected the API key. Check the key in your credentials."
    if status_code == 402:
        return "twitterapi.io account has insufficient credits. Top up at twitterapi.io."
    if status_code == 429:
        return "twitterapi.io rate limit hit. Retry in a moment."
    return f"twitterapi.io error {status_code}: {body[:300]}"


class TwitterAPIClient:
    """Mixin exposing paginated twitterapi.io fetches to tool mixins."""

    async def _twitter_get_page(
        self, api_key: str, path: str, params: dict[str, Any]
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

    async def _twitter_fetch_tweets(
        self,
        api_key: str,
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
            data = await self._twitter_get_page(api_key, path, page_params)

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
