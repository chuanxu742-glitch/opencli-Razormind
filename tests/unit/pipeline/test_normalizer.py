"""Unit tests for the pipeline normalizer."""

from backend.pipeline.normalizer import normalize_item, normalize_items


def test_normalize_item_standard_fields():
    raw = {
        "title": "Test Article",
        "url": "https://example.com/article",
        "content": "Article body text",
        "author": "Alice",
        "published": "2024-01-15",
    }
    normalized, content_hash = normalize_item(raw, "source-123")
    assert normalized["title"] == "Test Article"
    assert normalized["url"] == "https://example.com/article"
    assert normalized["content"] == "Article body text"
    assert normalized["author"] == "Alice"
    assert normalized["published_at"] == "2024-01-15"
    assert normalized["source_id"] == "source-123"
    assert len(content_hash) == 64  # SHA-256 hex


def test_normalize_item_alternate_keys():
    raw = {"headline": "Breaking News", "link": "https://news.com/1", "body": "Full text"}
    normalized, _ = normalize_item(raw, "src")
    assert normalized["title"] == "Breaking News"
    assert normalized["url"] == "https://news.com/1"
    assert normalized["content"] == "Full text"


def test_normalize_item_extra_fields():
    raw = {"title": "Test", "url": "https://ex.com", "custom_score": 42}
    normalized, _ = normalize_item(raw, "src")
    assert normalized.get("extra_custom_score") == 42


def test_normalize_item_dedup_consistency():
    raw = {"title": "Same", "url": "https://ex.com", "content": "text"}
    _, hash1 = normalize_item(raw, "src1")
    _, hash2 = normalize_item(raw, "src1")
    assert hash1 == hash2


def test_normalize_item_different_sources():
    raw = {"title": "Same", "url": "https://ex.com", "content": "text"}
    _, hash1 = normalize_item(raw, "src1")
    _, hash2 = normalize_item(raw, "src2")
    assert hash1 != hash2


def test_normalize_items_batch():
    items = [
        {"title": "A", "url": "https://a.com"},
        {"title": "B", "url": "https://b.com"},
    ]
    triples = normalize_items(items, "src")
    assert len(triples) == 2
    raw, normalized, content_hash = triples[0]
    assert raw == items[0]
    assert normalized["title"] == "A"
    assert len(content_hash) == 64


# ── opencli site-specific field mapping tests ─────────────────────────────────


def test_weibo_word_maps_to_title():
    """weibo hot: uses 'word' as the topic name."""
    raw = {
        "rank": 1,
        "word": "福建一鸭子活吞41只小鸡",
        "hot_value": 126652,
        "category": "民生新闻",
        "url": "https://s.weibo.com/...",
    }
    normalized, _ = normalize_item(raw, "weibo-src")
    assert normalized["title"] == "福建一鸭子活吞41只小鸡"
    assert "extra_hot_value" in normalized
    assert "extra_category" in normalized
    # 'word' should not appear as extra_ since it maps to title
    assert "extra_word" not in normalized


def test_twitter_trending_topic_maps_to_title():
    """twitter trending: uses 'topic' as the trend name."""
    raw = {"rank": 1, "topic": "OpenAI", "tweets": "150K"}
    normalized, _ = normalize_item(raw, "twitter-src")
    assert normalized["title"] == "OpenAI"
    assert "extra_tweets" in normalized
    assert "extra_topic" not in normalized


def test_youtube_channel_maps_to_author():
    """youtube search: uses 'channel' as the uploader."""
    raw = {
        "rank": 1,
        "title": "Python Tutorial",
        "channel": "TechChannel",
        "views": "1.2M",
        "duration": "15:30",
        "url": "https://youtube.com/...",
    }
    normalized, _ = normalize_item(raw, "yt-src")
    assert normalized["author"] == "TechChannel"
    assert "extra_channel" not in normalized


def test_linkedin_listed_maps_to_published_at():
    """linkedin search: uses 'listed' as the posting date."""
    raw = {
        "rank": 1,
        "title": "AI Engineer",
        "company": "ACME",
        "location": "Remote",
        "listed": "2 days ago",
        "salary": "$150K",
        "url": "https://linkedin.com/...",
    }
    normalized, _ = normalize_item(raw, "li-src")
    assert normalized["published_at"] == "2 days ago"


def test_douyin_create_time_epoch_maps_to_published_at():
    normalized, _ = normalize_item(
        {"title": "Douyin item", "create_time": 1784512800},
        "douyin",
    )

    assert normalized["published_at"] == "1784512800"
    assert "extra_create_time" not in normalized
    assert "extra_listed" not in normalized


def test_announcement_display_time_wins_over_date_only_time():
    raw = {
        "title": "公司公告",
        "time": "2026-07-24 00:00:00",
        "noticeDate": "2026-07-24 00:00:00",
        "displayTime": "2026-07-23 21:23:02:245",
    }

    normalized, _ = normalize_item(raw, "announcement-src")

    assert normalized["published_at"] == "2026-07-23 21:23:02:245"
    assert "extra_displayTime" not in normalized
    assert "extra_noticeDate" not in normalized


def test_xueqiu_text_maps_to_content():
    """xueqiu feed: uses 'text' as post content (no title)."""
    raw = {
        "rank": 1,
        "author": "某投资者",
        "text": "今日大盘分析...",
        "likes": 42,
        "url": "https://xueqiu.com/...",
    }
    normalized, _ = normalize_item(raw, "xueqiu-src")
    assert normalized["content"] == "今日大盘分析..."
    assert normalized["author"] == "某投资者"
    assert "extra_text" not in normalized


def test_case_insensitive_standard_keys_not_duplicated():
    """Fields matched by standard keys should not appear as extra_* regardless of case."""
    raw = {"Title": "Some Article", "URL": "https://example.com", "Author": "Bob"}
    normalized, _ = normalize_item(raw, "src")
    assert normalized["title"] == "Some Article"
    assert normalized["url"] == "https://example.com"
    assert normalized["author"] == "Bob"
    assert "extra_Title" not in normalized
    assert "extra_URL" not in normalized
    assert "extra_Author" not in normalized


def test_fallback_hash_for_empty_standard_fields():
    """When title/url/content all empty, hash should use full raw data."""
    raw1 = {"rank": 1, "word": "topic A", "hot_value": 1000}
    raw2 = {"rank": 1, "word": "topic B", "hot_value": 2000}
    # Both have 'word' which maps to title, so fallback won't trigger
    _, hash1 = normalize_item(raw1, "src")
    _, hash2 = normalize_item(raw2, "src")
    assert hash1 != hash2


def test_no_standard_fields_uses_raw_hash():
    """Records with no recognizable standard fields get hash from raw data."""
    raw1 = {"metric_a": 100, "metric_b": "foo"}
    raw2 = {"metric_a": 200, "metric_b": "bar"}
    _, hash1 = normalize_item(raw1, "src")
    _, hash2 = normalize_item(raw2, "src")
    assert hash1 != hash2


def test_product_facts_change_without_conflating_entity_source_or_observation():
    from backend.channels.ecommerce import adapt_items

    raw = {
        "asin": "B000000001", "title": "Same title",
        "product_url": "https://www.amazon.com/dp/B000000001",
        "price_value": 19.99, "currency": "USD",
        "fetched_at": "2026-09-05T00:00:00Z", "rank": 1,
    }

    def normalized(overrides=None, source="source-a"):
        item = adapt_items("amazon", "search", [{**raw, **(overrides or {})}])[0]
        return normalize_item(item, source)

    first, first_hash = normalized()
    observed, observed_hash = normalized({"fetched_at": "2026-09-06T00:00:00Z", "rank": 7})
    changed, changed_hash = normalized({"price_value": 29.99})
    other_source, other_source_hash = normalized(source="source-b")
    _, other_asin_hash = normalized({
        "asin": "B000000002", "product_url": "https://www.amazon.com/dp/B000000002",
    })

    assert observed_hash == first_hash
    assert observed["ecommerce"]["fact_version"] == first["ecommerce"]["fact_version"]
    assert observed["ecommerce"]["observed_at"] != first["ecommerce"]["observed_at"]
    assert changed_hash != first_hash
    assert changed["ecommerce"]["fact_version"] != first["ecommerce"]["fact_version"]
    assert other_source_hash != first_hash
    assert other_source["ecommerce"]["fact_version"] == first["ecommerce"]["fact_version"]
    assert other_asin_hash != first_hash


def test_offer_observation_does_not_create_a_new_fact_version():
    from backend.channels.ecommerce import adapt_items

    raw = {
        "asin": "B000000001", "product_url": "https://www.amazon.com/dp/B000000001",
        "sold_by": "Fixture seller", "price_value": 19.99, "currency": "USD",
        "fetched_at": "2026-09-05T00:00:00Z",
    }
    first, first_hash = normalize_item(adapt_items("amazon", "offer", [raw])[0], "source")
    observed, observed_hash = normalize_item(adapt_items(
        "amazon", "offer", [{**raw, "fetched_at": "2026-09-06T00:00:00Z"}],
    )[0], "source")
    assert observed_hash == first_hash
    assert observed["ecommerce"]["fact_version"] == first["ecommerce"]["fact_version"]


def test_1688_quantity_tiers_and_missing_price_remain_distinct_facts():
    from backend.channels.ecommerce import adapt_items

    raw = {
        "offer_id": "887904326744", "title": "Fixture",
        "item_url": "https://detail.1688.com/offer/887904326744.html",
        "price_tiers": [{"quantity_min": 10, "price_text": "12", "price": 12, "currency": "CNY"}, {"quantity_min": 100, "price_text": "9", "price": 9, "currency": "CNY"}],
        "currency": "CNY", "moq_value": 10,
    }
    first, first_hash = normalize_item(adapt_items("1688", "item", [raw])[0], "source")
    changed_raw = {**raw, "price_tiers": [{"quantity_min": 10, "price_text": "12", "price": 12, "currency": "CNY"}, {"quantity_min": 100, "price_text": "8", "price": 8, "currency": "CNY"}]}
    _, changed_hash = normalize_item(adapt_items("1688", "item", [changed_raw])[0], "source")
    assert first["ecommerce"]["facts"]["price_tiers"] == raw["price_tiers"]
    assert changed_hash != first_hash

    missing = {"asin": "B000000001", "title": "Fixture", "product_url": "https://www.amazon.com/dp/B000000001", "price_value": None}
    _, missing_hash = normalize_item(adapt_items("amazon", "product", [missing])[0], "source")
    _, zero_hash = normalize_item(adapt_items("amazon", "product", [{**missing, "price_value": 0}])[0], "source")
    assert missing_hash != zero_hash


def test_unrelated_ecommerce_named_dictionary_remains_ordinary_source_data():
    from backend.channels.ecommerce import adapt_items, ecommerce_identity

    raw = {"title": "Social post", "url": "https://example.com/post/1", "_ecommerce": {"campaign": "summer"}}
    item = adapt_items("twitter", "search", [raw])[0]
    normalized, content_hash = normalize_item(item, "source")
    _, original_hash = normalize_item({"title": raw["title"], "url": raw["url"]}, "source")
    assert ecommerce_identity(item) is None
    assert "ecommerce" not in normalized
    assert normalized["extra__ecommerce"] == raw["_ecommerce"]
    assert content_hash == original_hash


def test_product_link_tracking_does_not_create_fact_versions():
    from backend.channels.ecommerce import adapt_items

    raw = {
        "asin": "B000000001", "title": "Fixture",
        "product_url": "https://www.amazon.com/dp/B000000001",
        "review_url": "https://www.amazon.com/product-reviews/B000000001?ref=first",
        "qa_url": "https://www.amazon.com/ask/questions/asin/B000000001?ref=first",
    }

    def version(values):
        item = adapt_items("amazon", "product", [values])[0]
        return normalize_item(item, "source")[1]

    tracked = {**raw, "review_url": raw["review_url"].replace("first", "second"),
               "qa_url": raw["qa_url"].replace("first", "second")}
    assert version(tracked) == version(raw)
