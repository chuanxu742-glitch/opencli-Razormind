"""ODP event identity, product observation, and timestamp boundary contracts."""

from datetime import datetime

from backend.odp.mapper import RecordEventMapper, provider_for_channel


def _normalized(**over):
    base = {
        "title": "Hello",
        "url": "https://x/a",
        "content": "body",
        "author": "",
        "published_at": "2026-06-30T12:00:00Z",
        "source_id": "src-1",
    }
    base.update(over)
    return base


def test_provider_for_channel():
    assert provider_for_channel("rss") == "opencli-admin/rss"




def test_from_triple_coerces_ids_to_str():
    ev = RecordEventMapper.from_triple(
        channel_type="api",
        source_id=42,
        task_id=7,
        raw={},
        normalized=_normalized(),
        content_hash="h",
    )
    assert ev.source_id == "42"
    assert ev.task_id == "7"


def test_source_ts_falls_back_to_now_when_missing():
    ev = RecordEventMapper.from_triple(
        channel_type="rss",
        source_id="s",
        task_id="t",
        raw={},
        normalized=_normalized(published_at=""),
        content_hash="h",
    )
    # Value is non-deterministic (now), but must be valid RFC3339 ending in Z.
    assert ev.source_ts.endswith("Z")
    datetime.fromisoformat(ev.source_ts.replace("Z", "+00:00"))


def test_source_ts_falls_back_to_now_when_unparseable():
    ev = RecordEventMapper.from_triple(
        channel_type="rss",
        source_id="s",
        task_id="t",
        raw={},
        normalized=_normalized(published_at="not-a-date"),
        content_hash="h",
    )
    assert ev.source_ts.endswith("Z")
    datetime.fromisoformat(ev.source_ts.replace("Z", "+00:00"))


def test_naive_published_at_assumed_utc():
    ev = RecordEventMapper.from_triple(
        channel_type="rss",
        source_id="s",
        task_id="t",
        raw={},
        normalized=_normalized(published_at="2026-06-30T12:00:00"),
        content_hash="h",
    )
    assert ev.source_ts == "2026-06-30T12:00:00Z"




def test_product_wire_dedup_tracks_facts_not_observation_or_task():
    from backend.channels.ecommerce import adapt_items
    from backend.pipeline.normalizer import normalize_item

    raw = {
        "asin": "B000000001", "title": "Fixture",
        "product_url": "https://www.amazon.com/dp/B000000001",
        "price_value": 19.99, "currency": "USD",
        "fetched_at": "2026-09-05T00:00:00Z",
        "published_at": "2020-01-01T00:00:00Z",
    }

    def event(overrides=None, task="task-a", command="product"):
        item = adapt_items("amazon", command, [{**raw, **(overrides or {})}])[0]
        normalized, content_hash = normalize_item(item, "source")
        return RecordEventMapper.from_triple(
            channel_type="opencli", source_id="source", task_id=task,
            raw=item, normalized=normalized, content_hash=content_hash,
        ).to_wire()

    first = event()
    observation = event({"fetched_at": "2026-09-06T00:00:00Z"}, task="task-b")
    changed = event({"price_value": 29.99})
    offer = event(command="offer")
    assert first["event_id"] == observation["event_id"]
    assert changed["event_id"] != first["event_id"]
    assert offer["event_id"] != first["event_id"]
    assert observation["payload"]["ecommerce"]["observed_at"] != first["payload"]["ecommerce"]["observed_at"]
    assert changed["payload"]["ecommerce"]["facts"]["price_value"] == 29.99
    assert datetime.fromisoformat(first["source_ts"].replace("Z", "+00:00")) == datetime.fromisoformat("2026-09-05T00:00:00+00:00")
    assert datetime.fromisoformat(observation["source_ts"].replace("Z", "+00:00")) == datetime.fromisoformat("2026-09-06T00:00:00+00:00")
    missing_publication = event({"published_at": None})
    assert missing_publication["source_ts"] == first["source_ts"]


def test_nonproduct_wire_retains_legacy_content_hash_identity():
    first = RecordEventMapper.from_triple(
        channel_type="rss", source_id="source", task_id="task-a",
        raw={"id": "feed-entry"}, normalized=_normalized(), content_hash="legacy-hash",
    ).to_wire()
    retried = RecordEventMapper.from_triple(
        channel_type="rss", source_id="source", task_id="task-b",
        raw={"id": "feed-entry"}, normalized=_normalized(), content_hash="legacy-hash",
    ).to_wire()
    assert first["event_id"] == retried["event_id"] == "legacy-hash"
    assert "ecommerce" not in first["payload"]
