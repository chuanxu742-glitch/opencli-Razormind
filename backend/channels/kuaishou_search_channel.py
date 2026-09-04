"""Collect public Kuaishou video search results through a real browser session."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from backend.browser_act.cdp_session import CdpBrowserActSession, CdpSessionError
from backend.channels.base import (
    AbstractChannel,
    Capabilities,
    ChannelResult,
)
from backend.channels.registry import register_channel

_KUAISHOU_SEARCH_URL = "https://www.kuaishou.com/search/video?searchKey={}"
_FEED_PATH = "/rest/v/search/feed"
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50
_MAX_SEARCH_PAGES = 5
_SCROLL_AMOUNT = 1400
_DEFAULT_COMMENT_LIMIT = 20
_DEFAULT_COMMENT_VIDEOS = 5

_COMMENTS_JS = """
(limit) => [...document.querySelectorAll('.comment-item.comment-list-item')]
  .slice(0, limit)
  .map((item) => ({
    author: item.querySelector('.author-name')?.textContent?.trim() || '',
    time: item.querySelector('.comment-item-time')?.textContent?.trim() || '',
    text: item.querySelector('.comment-item-content')?.textContent?.trim() || '',
    likes: item.querySelector('.comment-item-operation-op')?.textContent?.trim() || '',
    avatar_url: item.querySelector('.comment-item-portrait')?.getAttribute('src') || '',
  }))
"""

# Kuaishou embeds the initial search response in an obfuscated INIT_STATE key.
# The value shape is stable and avoids depending on the obfuscated key name.
_READ_STATE_JS = """
(() => {
  const states = Object.values(window.INIT_STATE || {});
  const state = states.find((value) => value && Array.isArray(value.feeds));
  return state || {feeds: [], pcursor: "", searchSessionId: ""};
})()
"""

_SCROLL_JS = """
(amount) => {
  const container = document.querySelector('.wb-content');
  const target = container || document.scrollingElement || document.documentElement;
  target.scrollBy(0, amount);
  return {scrollTop: target.scrollTop, scrollHeight: target.scrollHeight};
}
"""


def _search_url(query: str) -> str:
    return _KUAISHOU_SEARCH_URL.format(quote(query, safe=""))


def _first_url(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, str) and item), None)
    return None


def _video_url(photo_id: str) -> str:
    return f"https://www.kuaishou.com/short-video/{photo_id}"


def _play_url(photo: dict[str, Any]) -> str | None:
    for manifest_name in ("manifestH265", "manifest"):
        manifest = photo.get(manifest_name)
        if not isinstance(manifest, dict):
            continue
        for adaptation in manifest.get("adaptationSet") or []:
            if not isinstance(adaptation, dict):
                continue
            for representation in adaptation.get("representation") or []:
                if not isinstance(representation, dict):
                    continue
                url = representation.get("url")
                if isinstance(url, str) and url:
                    return url
    return None


def _published_at(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _feed_item(feed: Any) -> dict[str, Any] | None:
    if not isinstance(feed, dict):
        return None
    photo = feed.get("photo")
    author = feed.get("author")
    comment = feed.get("comment")
    if not isinstance(photo, dict):
        return None
    photo_id = str(photo.get("id") or "").strip()
    if not photo_id:
        return None
    author = author if isinstance(author, dict) else {}
    comment = comment if isinstance(comment, dict) else {}
    caption = str(photo.get("caption") or "").strip()
    cover_url = _first_url(photo.get("coverUrl"))
    play_url = _play_url(photo)
    return {
        "title": caption or f"Kuaishou video {photo_id}",
        "content": caption,
        "author": str(author.get("name") or "").strip(),
        "author_id": str(author.get("id") or "").strip() or None,
        "author_avatar": _first_url(author.get("headerUrl")),
        "url": _video_url(photo_id),
        "photo_id": photo_id,
        "create_time": photo.get("timestamp"),
        "published_at": _published_at(photo.get("timestamp")),
        "cover_url": cover_url,
        "play_url": play_url,
        "statistics": {
            key: value
            for key, value in {
                "like_count": photo.get("likeCount"),
                "comment_count": comment.get("us_c"),
                "collect_count": photo.get("collectCount"),
                "view_count": photo.get("viewCount"),
                "share_count": photo.get("shareCount"),
            }.items()
            if value is not None
        },
        "media": {
            "type": "video",
            "play_url": play_url,
            "cover_url": cover_url,
            "duration_ms": photo.get("duration"),
            "width": photo.get("width"),
            "height": photo.get("height"),
        },
        "tags": [
            str(tag.get("name"))
            for tag in feed.get("tags") or []
            if isinstance(tag, dict) and tag.get("name")
        ],
    }


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []
    for feed in payload.get("feeds") or []:
        item = _feed_item(feed)
        if item:
            items.append(item)
    return items


def _next_cursor(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("pcursor") or "").strip()


def _is_feed_response(response: Any) -> bool:
    request = response.request
    return _FEED_PATH in response.url and request.method == "POST"


async def _state_payload(page: Any) -> dict[str, Any]:
    payload = await page.evaluate(_READ_STATE_JS)
    return payload if isinstance(payload, dict) else {}


async def _response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = await response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _comment_items(nodes: Any) -> list[dict[str, Any]]:
    if not isinstance(nodes, list):
        return []
    comments: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        author = str(node.get("author") or "").strip()
        if not author:
            continue
        comments.append(
            {
                "author": author,
                "time": str(node.get("time") or "").strip() or None,
                "text": str(node.get("text") or "").strip(),
                "likes": str(node.get("likes") or "").strip() or None,
                "avatar_url": _first_url(node.get("avatar_url")),
            }
        )
    return comments


async def _collect_comments(
    page: Any,
    items: list[dict[str, Any]],
    comment_limit: int,
    max_videos: int,
) -> int:
    errors = 0
    for item in items[:max_videos]:
        try:
            await page.goto(
                str(item["url"]), wait_until="domcontentloaded", timeout=20000
            )
            await page.wait_for_selector(
                ".comment-item.comment-list-item", state="attached", timeout=10000
            )
            nodes = await page.evaluate(_COMMENTS_JS, comment_limit)
            item["comments"] = _comment_items(nodes)
        except Exception:
            item["comments"] = []
            errors += 1
    return errors


def _is_enabled(value: Any) -> bool:
    return value is True or (
        isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}
    )


async def _collect_from_page(
    page: Any, url: str, limit: int
) -> tuple[list[dict[str, Any]], int]:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    try:
        async with page.expect_response(
            _is_feed_response, timeout=15000
        ) as response_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        payload = await _response_payload(await response_info.value)
    except PlaywrightTimeoutError:
        payload = await _state_payload(page)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages_fetched = 0

    for page_number in range(_MAX_SEARCH_PAGES):
        pages_fetched += 1
        for item in _items_from_payload(payload):
            photo_id = item.get("photo_id")
            if photo_id in seen:
                continue
            seen.add(photo_id)
            item["rank"] = len(items) + 1
            items.append(item)
            if len(items) >= limit:
                return items[:limit], pages_fetched

        cursor = _next_cursor(payload)
        if not cursor or cursor in {"-1", "no_more"}:
            break

        try:
            async with page.expect_response(
                _is_feed_response, timeout=10000
            ) as response_info:
                container = page.locator(".wb-content")
                if await container.count():
                    await container.hover()
                    await page.mouse.wheel(0, _SCROLL_AMOUNT)
                else:
                    await page.evaluate(_SCROLL_JS, _SCROLL_AMOUNT)
            payload = await _response_payload(await response_info.value)
        except PlaywrightTimeoutError:
            break

    return items[:limit], pages_fetched


@register_channel
class KuaishouSearchChannel(AbstractChannel):
    """Search Kuaishou videos by keyword from the logged-in browser page."""

    channel_type = "kuaishou_search"
    capabilities = Capabilities(
        auth_kind="session",
        session_affinity=True,
        default_rate="12/min",
    )

    async def collect(
        self, config: dict[str, Any], parameters: dict[str, Any]
    ) -> ChannelResult:
        query = str(parameters.get("query") or config.get("query") or "").strip()
        if not query:
            return ChannelResult.fail("'query' is required for Kuaishou video search")

        try:
            limit = max(
                1,
                min(
                    int(
                        parameters.get("limit") or config.get("limit") or _DEFAULT_LIMIT
                    ),
                    _MAX_LIMIT,
                ),
            )
        except (TypeError, ValueError):
            return ChannelResult.fail("'limit' must be an integer")

        with_comments = _is_enabled(
            parameters.get("with_comments", config.get("with_comments", False))
        )
        try:
            comment_limit = max(
                1,
                min(
                    int(
                        parameters.get("comment_limit")
                        or config.get("comment_limit")
                        or _DEFAULT_COMMENT_LIMIT
                    ),
                    100,
                ),
            )
            max_comment_videos = max(
                1,
                min(
                    int(
                        parameters.get("max_comment_videos")
                        or config.get("max_comment_videos")
                        or _DEFAULT_COMMENT_VIDEOS
                    ),
                    20,
                ),
            )
        except (TypeError, ValueError):
            return ChannelResult.fail(
                "'comment_limit' and 'max_comment_videos' must be integers"
            )

        comment_errors = 0

        endpoint = str(
            parameters.get("chrome_endpoint")
            or parameters.get("cdp_endpoint")
            or config.get("chrome_endpoint")
            or config.get("cdp_endpoint")
            or ""
        ).strip()
        url = _search_url(query)

        try:
            if endpoint:
                async with CdpBrowserActSession(endpoint, target_url=url) as session:
                    items, pages_fetched = await _collect_from_page(
                        session.page, url, limit
                    )
                    if with_comments:
                        comment_errors = await _collect_comments(
                            session.page,
                            items,
                            comment_limit,
                            max_comment_videos,
                        )
            else:
                from backend.browser_pool import get_pool

                pool = get_pool()
                async with pool.acquire(endpoint=None) as cdp_endpoint:
                    if pool.get_mode(cdp_endpoint) != "cdp":
                        return ChannelResult.fail(
                            "Kuaishou search requires a direct CDP browser endpoint"
                        )
                    async with CdpBrowserActSession(
                        cdp_endpoint, target_url=url
                    ) as session:
                        items, pages_fetched = await _collect_from_page(
                            session.page, url, limit
                        )
                        if with_comments:
                            comment_errors = await _collect_comments(
                                session.page,
                                items,
                                comment_limit,
                                max_comment_videos,
                            )
        except CdpSessionError as exc:
            return ChannelResult.fail(
                f"Kuaishou browser session failed: {exc}", error_type="ConnectionError"
            )
        except Exception as exc:
            return ChannelResult.fail(
                f"Kuaishou search failed: {exc}", error_type=type(exc).__name__
            )

        if not items:
            return ChannelResult.fail(
                "Kuaishou search returned no video results",
                error_type="NoResults",
            )
        return ChannelResult.ok(
            items,
            query=query,
            canonical_url=url,
            pages_fetched=pages_fetched,
            comments_requested=with_comments,
            comment_videos=min(len(items), max_comment_videos) if with_comments else 0,
            comment_errors=comment_errors,
        )

    async def validate_config(self, config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if not str(config.get("query") or "").strip():
            errors.append("'query' is required for Kuaishou video search")
        for key in ("limit", "comment_limit", "max_comment_videos"):
            if key in config:
                try:
                    int(config[key])
                except (TypeError, ValueError):
                    errors.append(f"'{key}' must be an integer")
        return errors

    def identity(self, item: dict[str, Any]) -> str | None:
        value = item.get("photo_id")
        return str(value) if value else None
