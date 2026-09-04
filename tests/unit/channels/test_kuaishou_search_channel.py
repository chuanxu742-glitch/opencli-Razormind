import pytest

from backend.channels.kuaishou_search_channel import (
    KuaishouSearchChannel,
    _MAX_LIMIT,
    _comment_items,
    _items_from_payload,
    _search_url,
)


def _payload() -> dict:
    return {
        "result": 1,
        "pcursor": "1",
        "searchSessionId": "session-1",
        "feeds": [
            {
                "tags": [{"name": "耳机", "type": 1}],
                "photo": {
                    "id": "3xphoto123",
                    "caption": "蓝牙耳机测评",
                    "timestamp": 1783059067250,
                    "likeCount": 14221,
                    "collectCount": 18,
                    "viewCount": 1753079,
                    "duration": 34833,
                    "width": 720,
                    "height": 1280,
                    "coverUrl": "https://image.example/cover.jpg",
                    "manifestH265": {
                        "adaptationSet": [
                            {
                                "representation": [
                                    {"url": "https://video.example/play.mp4"}
                                ]
                            }
                        ]
                    },
                },
                "comment": {"us_c": 36},
                "author": {
                    "id": "author-1",
                    "name": "测试作者",
                    "headerUrl": "https://image.example/avatar.jpg",
                },
            },
            {"photo": {"caption": "缺少视频 ID"}},
        ],
    }


def test_search_url_encodes_keyword():
    assert _search_url("蓝牙 耳机") == (
        "https://www.kuaishou.com/search/video?searchKey=%E8%93%9D%E7%89%99%20%E8%80%B3%E6%9C%BA"
    )


def test_items_extract_video_metadata_and_ignore_invalid_feeds():
    items = _items_from_payload(_payload())

    assert len(items) == 1
    item = items[0]
    assert item["photo_id"] == "3xphoto123"
    assert item["url"] == "https://www.kuaishou.com/short-video/3xphoto123"
    assert item["author"] == "测试作者"
    assert item["author_id"] == "author-1"
    assert item["author_avatar"] == "https://image.example/avatar.jpg"
    assert item["statistics"] == {
        "like_count": 14221,
        "comment_count": 36,
        "collect_count": 18,
        "view_count": 1753079,
    }
    assert item["media"] == {
        "type": "video",
        "play_url": "https://video.example/play.mp4",
        "cover_url": "https://image.example/cover.jpg",
        "duration_ms": 34833,
        "width": 720,
        "height": 1280,
    }
    assert item["tags"] == ["耳机"]


def test_comment_items_normalize_visible_comment_nodes():
    assert _comment_items(
        [
            {
                "author": "  评论用户 ",
                "time": " 4小时前 ",
                "text": "  好看 ",
                "likes": "0",
                "avatar_url": "https://image.example/avatar.jpg",
            },
            {"author": "", "text": "discard"},
            "invalid",
        ]
    ) == [
        {
            "author": "评论用户",
            "time": "4小时前",
            "text": "好看",
            "likes": "0",
            "avatar_url": "https://image.example/avatar.jpg",
        }
    ]


@pytest.mark.asyncio
async def test_validate_config_requires_query_and_checks_numeric_limits():
    channel = KuaishouSearchChannel()

    assert _MAX_LIMIT == 50
    assert await channel.validate_config({"query": "耳机", "limit": 50}) == []
    errors = await channel.validate_config({"limit": "many"})
    assert "'query' is required for Kuaishou video search" in errors
    assert "'limit' must be an integer" in errors
