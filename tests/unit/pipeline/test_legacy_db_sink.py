"""Real isolated persistence contracts for product and nonproduct records."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.models.record import CollectedRecord
from backend.models.source import DataSource
from backend.models.task import CollectionTask
from backend.pipeline.sinks import LegacyDbSink, RunContext


async def _sink_context(db_engine, monkeypatch, provider="opencli"):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", factory)
    async with factory() as session:
        source = DataSource(name="Isolated fixture", channel_type=provider, channel_config={})
        session.add(source)
        await session.flush()
        task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
        session.add(task)
        await session.commit()
        return factory, RunContext(task_id=task.id, source_id=source.id, provider=provider)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("site", "command", "raw", "price_key", "changed_price"),
    [
        ("taobao", "search", {"item_id": "827563850178", "title": "Fixture", "url": "https://item.taobao.com/item.htm?id=827563850178", "price": "¥19.90"}, "price", "¥29.90"),
        ("jd", "search", {"sku": "100291143898", "title": "Fixture", "url": "https://item.jd.com/100291143898.html", "price": "¥19.90"}, "price", "¥29.90"),
        ("1688", "item", {"offer_id": "887904326744", "title": "Fixture", "item_url": "https://detail.1688.com/offer/887904326744.html", "currency": "CNY", "moq_value": 10, "price_tiers": [{"quantity_min": 10, "price_text": "12", "price": 12, "currency": "CNY"}]}, "price_tiers", [{"quantity_min": 10, "price_text": "11", "price": 11, "currency": "CNY"}]),
        ("xianyu", "item", {"item_id": "1040754408976", "title": "Fixture", "description": "Used fixture", "item_url": "https://www.goofish.com/item?id=1040754408976", "price": "¥19.90"}, "price", "¥29.90"),
        ("amazon", "product", {"asin": "B000000001", "title": "Fixture", "product_url": "https://www.amazon.com/dp/B000000001", "price_value": 19.9, "currency": "USD"}, "price_value", 29.9),
        ("coupang", "product", {"product_id": "123456789", "title": "Fixture", "url": "https://www.coupang.com/vp/products/123456789?itemId=111&vendorItemId=222", "price": 12900}, "price", 13900),
        ("ebay", "product", {"itemId": "v1|123456789012|0", "title": "Fixture", "itemWebUrl": "https://www.ebay.com/itm/123456789012", "price": {"value": "19.90", "currency": "USD"}, "marketplace": "EBAY_US"}, "price", {"value": "29.90", "currency": "USD"}),
    ],
)
async def test_seven_platform_price_updates_persist_without_duplicate_facts(
    db_engine, monkeypatch, site, command, raw, price_key, changed_price,
):
    from backend.channels.ecommerce import adapt_items

    factory, ctx = await _sink_context(db_engine, monkeypatch)
    sink = LegacyDbSink(forward_to_odp=False)

    def items(value, observed):
        row = {**deepcopy(raw), price_key: deepcopy(value), "fetched_at": observed}
        return adapt_items(site, command, [row])

    initial = await sink.write_batch(ctx, items(raw[price_key], "2026-09-05T00:00:00Z"))
    record_id = initial.records[0].id
    initial_version = initial.records[0].normalized_data["ecommerce"]["fact_version"]
    updated = await sink.write_batch(ctx, items(changed_price, "2026-09-06T00:00:00Z"))
    assert updated.accepted == 1
    assert updated.records[0].id == record_id
    changed_version = updated.records[0].normalized_data["ecommerce"]["fact_version"]
    assert changed_version != initial_version
    repeated = await sink.write_batch(ctx, items(changed_price, "2026-09-07T00:00:00Z"))
    assert repeated.duplicates == 1
    async with factory() as session:
        rows = (await session.scalars(select(CollectedRecord).where(CollectedRecord.source_id == ctx.source_id))).all()
        assert len(rows) == 1
        product = rows[0].normalized_data["ecommerce"]
        assert product["facts"][price_key] == changed_price
        assert product["fact_version"] == changed_version
        assert product["observed_at"].startswith("2026-09-07")
        if site == "coupang":
            assert "itemId=111" in rows[0].normalized_data["url"]
            assert "vendorItemId=222" in rows[0].normalized_data["url"]


@pytest.mark.asyncio
async def test_product_facets_and_same_title_entities_survive_independent_updates(db_engine, monkeypatch):
    from backend.channels.ecommerce import adapt_items

    factory, ctx = await _sink_context(db_engine, monkeypatch)
    sink = LegacyDbSink(forward_to_odp=False)
    product = {"asin": "B000000001", "title": "Same title", "product_url": "https://www.amazon.com/dp/B000000001", "price_value": 19.9, "currency": "USD"}
    other = {**product, "asin": "B000000002", "product_url": "https://www.amazon.com/dp/B000000002"}
    await sink.write_batch(ctx, adapt_items("amazon", "product", [product, other]))
    await sink.write_batch(ctx, adapt_items("amazon", "offer", [{"asin": product["asin"], "product_url": product["product_url"], "sold_by": "Seller one"}]))
    await sink.write_batch(ctx, adapt_items("amazon", "discussion", [{"asin": product["asin"], "product_url": product["product_url"], "average_rating_value": 4.5, "review_samples": []}]))
    await sink.write_batch(ctx, adapt_items("amazon", "search", [product]))
    await sink.write_batch(ctx, adapt_items("amazon", "offer", [{"asin": product["asin"], "product_url": product["product_url"], "sold_by": "Seller two"}]))
    async with factory() as session:
        rows = (await session.scalars(select(CollectedRecord).where(CollectedRecord.source_id == ctx.source_id))).all()
        assert len(rows) == 5
        assert len({r.identity_key for r in rows}) == 5
        product_rows = [r for r in rows if r.normalized_data["ecommerce"]["facet"] == "product"]
        assert len(product_rows) == 2
        assert all(r.normalized_data["title"] == "Same title" for r in product_rows)
        offer = next(r for r in rows if r.normalized_data["ecommerce"]["facet"] == "offer")
        assert offer.normalized_data["ecommerce"]["facts"]["sold_by"] == "Seller two"
        assert all(r.normalized_data["ecommerce"]["facts"]["price_value"] == 19.9 for r in product_rows)


@pytest.mark.asyncio
async def test_legacy_sink_nonproduct_native_identity_still_updates_in_place(db_engine, monkeypatch):
    factory, ctx = await _sink_context(db_engine, monkeypatch, provider="rss")
    sink = LegacyDbSink(forward_to_odp=False)
    first = await sink.write_batch(ctx, [{"id": "feed-entry-1", "title": "Original", "url": "https://example.com/1"}])
    second = await sink.write_batch(ctx, [{"id": "feed-entry-1", "title": "Corrected", "url": "https://example.com/1"}])
    assert second.records[0].id == first.records[0].id
    async with factory() as session:
        rows = (await session.scalars(select(CollectedRecord))).all()
        assert len(rows) == 1
        assert rows[0].normalized_data["title"] == "Corrected"
        assert "ecommerce" not in rows[0].normalized_data


@pytest.mark.asyncio
async def test_concurrent_product_writers_keep_one_identity_and_newest_observation(tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.channels.ecommerce import adapt_items
    from backend.database import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "concurrent-products.sqlite").as_posix(),
        connect_args={"timeout": 30},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory, ctx = await _sink_context(engine, monkeypatch)
        sink = LegacyDbSink(forward_to_odp=False)
        start = asyncio.Event()
        raw = {"asin": "B000000001", "title": "Fixture", "product_url": "https://www.amazon.com/dp/B000000001", "currency": "USD"}

        async def writer(price, observed_at):
            await start.wait()
            return await sink.write_batch(ctx, adapt_items(
                "amazon", "product", [{**raw, "price_value": price, "fetched_at": observed_at}],
            ))

        writers = [
            asyncio.create_task(writer(19.99, "2026-09-05T00:00:00Z")),
            asyncio.create_task(writer(29.99, "2026-09-07T00:00:00Z")),
            asyncio.create_task(writer(9.99, "2026-09-06T00:00:00Z")),
        ]
        start.set()
        await asyncio.gather(*writers)
        async with factory() as session:
            rows = (await session.scalars(select(CollectedRecord))).all()
            assert len(rows) == 1
            assert rows[0].normalized_data["ecommerce"]["facts"]["price_value"] == 29.99
            assert rows[0].normalized_data["ecommerce"]["observed_at"].startswith("2026-09-07")
    finally:
        await engine.dispose()
