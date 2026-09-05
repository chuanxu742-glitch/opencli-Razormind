"""Unit tests for pipeline storer."""

import pytest

from backend.pipeline.storer import store_records


@pytest.mark.asyncio
async def test_store_new_records(db_session):
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask

    # Create source and task for FK constraints
    source = DataSource(
        name="Test Source",
        channel_type="rss",
        channel_config={"feed_url": "https://example.com/feed.xml"},
    )
    db_session.add(source)
    await db_session.flush()

    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    triples = [
        (
            {"title": "Article 1"},
            {
                "title": "Article 1", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            "hash_abc123_1",
        ),
        (
            {"title": "Article 2"},
            {
                "title": "Article 2", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            "hash_abc123_2",
        ),
    ]

    new_records, skipped = await store_records(db_session, task.id, source.id, triples)
    assert len(new_records) == 2
    assert skipped == 0


@pytest.mark.asyncio
async def test_store_deduplication(db_session):
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask

    source = DataSource(
        name="Dedup Source",
        channel_type="rss",
        channel_config={"feed_url": "https://example.com/feed.xml"},
    )
    db_session.add(source)
    await db_session.flush()

    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    triple = (
        {"title": "Same Article"},
        {
            "title": "Same", "url": "", "content": "", "author": "",
            "published_at": "", "source_id": source.id,
        },
        "same_hash_xyz",
    )

    # First store: new record
    records1, skipped1 = await store_records(db_session, task.id, source.id, [triple])
    assert len(records1) == 1
    assert skipped1 == 0

    # Second store: duplicate should be skipped
    records2, skipped2 = await store_records(db_session, task.id, source.id, [triple])
    assert len(records2) == 0
    assert skipped2 == 1


@pytest.mark.asyncio
async def test_store_dedup_within_single_batch(db_session):
    """Two triples in the SAME store_records() call sharing a content_hash,
    neither previously stored (no identity involved): only one row lands,
    same net result as test_store_deduplication's cross-run case above but
    within one call — e.g. a channel returning the same item twice in one
    fetch (pagination overlap, a feed listing an entry twice).

    Two layers cooperate to guarantee this, both already covered elsewhere:
    the in-memory ``seen_in_batch`` fast path (this test's primary target)
    skips the second triple before it ever reaches flush(); if that guard
    were bypassed, the (source_id, content_hash) unique constraint plus the
    per-record retry-on-IntegrityError path (test_store_survives_concurrent_
    race_on_flush) would still catch it at flush() — confirmed empirically
    by temporarily disabling seen_in_batch and observing this test still
    pass via that path. This test pins the end-to-end guarantee; it does not
    by itself distinguish which layer fired."""
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask

    source = DataSource(
        name="Batch Dedup Source",
        channel_type="rss",
        channel_config={"feed_url": "https://example.com/feed.xml"},
    )
    db_session.add(source)
    await db_session.flush()

    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    triples = [
        (
            {"title": "Same Article"},
            {
                "title": "Same", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            "batch_dup_hash",
        ),
        (
            {"title": "Same Article (repeat)"},
            {
                "title": "Same", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            "batch_dup_hash",
        ),
    ]

    new_records, skipped = await store_records(db_session, task.id, source.id, triples)
    assert len(new_records) == 1
    assert skipped == 1

    from sqlalchemy import select as sa_select

    from backend.models.record import CollectedRecord

    rows = (
        await db_session.execute(
            sa_select(CollectedRecord).where(CollectedRecord.source_id == source.id)
        )
    ).scalars().all()
    assert len(rows) == 1  # DB agrees: one row, not two
    assert rows[0].content_hash == "batch_dup_hash"


@pytest.mark.asyncio
async def test_store_distinct_items_in_one_batch_all_land(db_session):
    """Sanity complement to the dedup tests above: several genuinely distinct
    items in one batch are all stored, none mistaken for duplicates of each
    other (proves the guards above key on content_hash equality, not on
    batch position or count)."""
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask

    source = DataSource(
        name="Distinct Items Source",
        channel_type="rss",
        channel_config={"feed_url": "https://example.com/feed.xml"},
    )
    db_session.add(source)
    await db_session.flush()

    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    triples = [
        (
            {"title": f"Article {i}"},
            {
                "title": f"Article {i}", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            f"distinct_hash_{i}",
        )
        for i in range(3)
    ]

    new_records, skipped = await store_records(db_session, task.id, source.id, triples)
    assert skipped == 0
    assert len(new_records) == 3
    assert {r.content_hash for r in new_records} == {
        "distinct_hash_0", "distinct_hash_1", "distinct_hash_2",
    }


@pytest.mark.asyncio
async def test_store_empty_input(db_session):
    new_records, skipped = await store_records(db_session, "task-id", "src-id", [])
    assert new_records == []
    assert skipped == 0


@pytest.mark.asyncio
async def test_store_survives_concurrent_race_on_flush(db_session):
    """A concurrent writer can land the same content_hash between our
    existence check and our flush (e.g. a celery retry racing the original
    attempt now that retries are real, PR-B). flush() must not crash the
    whole batch — the colliding row is skipped, everything else survives."""
    from backend.models.record import CollectedRecord
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask

    source = DataSource(
        name="Race Source", channel_type="rss",
        channel_config={"feed_url": "https://example.com/feed.xml"},
    )
    db_session.add(source)
    await db_session.flush()

    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    # The "other writer" wins the race and commits first. Committed (not just
    # flushed) so the recovery path's rollback() — which only unwinds this
    # test's own uncommitted work, same as a fresh production session — can't
    # undo it, matching how store_records is actually called (a session
    # opened fresh per write_batch, source/task already committed earlier).
    winner = CollectedRecord(
        task_id=task.id, source_id=source.id, raw_data={}, normalized_data={},
        content_hash="race_hash", status="normalized",
    )
    db_session.add(winner)
    await db_session.commit()

    triples = [
        (
            {"title": "Loser"},
            {
                "title": "Loser", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            "race_hash",
        ),
        (
            {"title": "Clean"},
            {
                "title": "Clean", "url": "", "content": "", "author": "",
                "published_at": "", "source_id": source.id,
            },
            "clean_hash",
        ),
    ]

    # Simulate the race window: the existence-check SELECT ran a moment
    # before the winner committed, so it comes back blind to "race_hash".
    real_execute = db_session.execute
    calls = {"n": 0}

    async def blind_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        return await real_execute(*args, **kwargs)

    db_session.execute = blind_first_call
    try:
        new_records, skipped = await store_records(db_session, task.id, source.id, triples)
    finally:
        db_session.execute = real_execute

    # The colliding row is skipped (not crashed), the clean one still lands.
    assert skipped == 1
    assert len(new_records) == 1
    assert new_records[0].content_hash == "clean_hash"


async def _setup_source_task(db_session, channel_type="rss"):
    from backend.models.source import DataSource
    from backend.models.task import CollectionTask

    source = DataSource(
        name="Identity Source", channel_type=channel_type,
        channel_config={"feed_url": "https://example.com/feed.xml"},
    )
    db_session.add(source)
    await db_session.flush()

    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()
    return source, task


def _triple(source_id, title, content_hash):
    return (
        {"title": title},
        {
            "title": title, "url": "", "content": "", "author": "",
            "published_at": "", "source_id": source_id,
        },
        content_hash,
    )


# ── C7: identity()-based dedup/update-in-place ──────────────────────────────

@pytest.mark.asyncio
async def test_store_identity_match_updates_in_place(db_session):
    """An item whose identity() matches an existing row, but whose content
    changed (e.g. a feed fixed a typo in the title), updates that row in
    place instead of inserting a duplicate (C7's fix)."""
    source, task = await _setup_source_task(db_session)

    first = _triple(source.id, "Original Title", "hash_v1")
    records1, skipped1 = await store_records(
        db_session, task.id, source.id, [first], identities=["entry-42"],
    )
    assert len(records1) == 1
    assert skipped1 == 0
    assert records1[0].identity_key == "entry-42"

    edited = _triple(source.id, "Original Title (fixed)", "hash_v2")
    records2, skipped2 = await store_records(
        db_session, task.id, source.id, [edited], identities=["entry-42"],
    )

    # Updated in place, not inserted as a new row.
    assert skipped2 == 0
    assert len(records2) == 1
    assert records2[0].content_hash == "hash_v2"
    assert records2[0].id == records1[0].id

    from sqlalchemy import select as sa_select

    from backend.models.record import CollectedRecord

    result = await db_session.execute(
        sa_select(CollectedRecord).where(CollectedRecord.source_id == source.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1  # still just one row for this source-native item
    assert rows[0].content_hash == "hash_v2"


@pytest.mark.asyncio
async def test_store_identity_match_same_hash_is_plain_duplicate(db_session):
    """Same identity AND same content_hash: a genuine duplicate, skipped —
    exactly like the content_hash-only path always did for an unedited
    re-fetch of the same item."""
    source, task = await _setup_source_task(db_session)

    triple = _triple(source.id, "Same", "same_hash")
    await store_records(db_session, task.id, source.id, [triple], identities=["entry-1"])
    records2, skipped2 = await store_records(
        db_session, task.id, source.id, [triple], identities=["entry-1"]
    )

    assert len(records2) == 0
    assert skipped2 == 1


@pytest.mark.asyncio
async def test_store_without_identities_is_unchanged(db_session):
    """Channels without identity() (identities=None, the default) keep
    deduplicating on content_hash alone — completely unaffected by C7."""
    source, task = await _setup_source_task(db_session)

    triple = _triple(source.id, "No Identity", "no_identity_hash")
    records1, skipped1 = await store_records(db_session, task.id, source.id, [triple])
    assert len(records1) == 1
    assert records1[0].identity_key is None

    # An "edit" with no identity info at all is content_hash-only: a
    # different hash is a brand new row, not an update — pre-C7 behavior.
    edited = _triple(source.id, "No Identity Edited", "no_identity_hash_v2")
    records2, skipped2 = await store_records(db_session, task.id, source.id, [edited])
    assert len(records2) == 1
    assert records2[0].id != records1[0].id  # inserted as a new row, not updated


@pytest.mark.asyncio
async def test_store_mixed_batch_some_items_without_identity(db_session):
    """identities can mix real values and None per item — items with None
    fall back to content_hash dedup individually, others use identity."""
    source, task = await _setup_source_task(db_session)

    triples = [
        _triple(source.id, "Has Identity", "hash_a"),
        _triple(source.id, "No Identity", "hash_b"),
    ]
    records, skipped = await store_records(
        db_session, task.id, source.id, triples, identities=["entry-x", None],
    )
    assert skipped == 0
    by_hash = {r.content_hash: r for r in records}
    assert by_hash["hash_a"].identity_key == "entry-x"
    assert by_hash["hash_b"].identity_key is None


@pytest.mark.asyncio
async def test_store_preserves_workflow_ownership_with_identity(db_session):
    """The integrated store seam keeps both workflow lineage and identity dedup."""
    source, task = await _setup_source_task(db_session)

    records, skipped = await store_records(
        db_session,
        task.id,
        source.id,
        [_triple(source.id, "Workflow record", "workflow_hash")],
        workflow_id="workflow-1",
        workflow_run_id="run-1",
        identities=["entry-workflow-1"],
    )

    assert skipped == 0
    assert len(records) == 1
    assert records[0].workflow_id == "workflow-1"
    assert records[0].workflow_run_id == "run-1"
    assert records[0].identity_key == "entry-workflow-1"


@pytest.mark.asyncio
async def test_store_identity_duplicated_within_same_batch(db_session):
    """Two triples in the same batch sharing an identity (e.g. a feed listed
    the same entry twice): keep the first, skip the rest."""
    source, task = await _setup_source_task(db_session)

    triples = [
        _triple(source.id, "First", "hash_first"),
        _triple(source.id, "Duplicate Within Batch", "hash_dup"),
    ]
    records, skipped = await store_records(
        db_session, task.id, source.id, triples,
        identities=["entry-same", "entry-same"],
    )
    assert len(records) == 1
    assert skipped == 1
    assert records[0].content_hash == "hash_first"


# ── C15: dedup lookup chunking across the SQLite variable limit ────────────

@pytest.mark.asyncio
async def test_store_dedup_chunks_across_batches(db_session, monkeypatch):
    """A dedup lookup spanning more hashes than one chunk still correctly
    finds every pre-existing hash, regardless of which chunk it falls into
    (C15) — proves the union-across-chunks logic, not just 'doesn't crash'."""
    import backend.pipeline.storer as storer_module

    monkeypatch.setattr(storer_module, "_HASH_CHUNK_SIZE", 2)

    source, task = await _setup_source_task(db_session)

    # Seed 5 existing rows — spans more than one chunk of size 2.
    seed_triples = [_triple(source.id, f"Seed {i}", f"seed_hash_{i}") for i in range(5)]
    await store_records(db_session, task.id, source.id, seed_triples)

    # A new batch mixing all 5 pre-existing hashes with 2 genuinely new ones.
    batch = seed_triples + [
        _triple(source.id, "New A", "new_hash_a"),
        _triple(source.id, "New B", "new_hash_b"),
    ]
    new_records, skipped = await store_records(db_session, task.id, source.id, batch)

    assert skipped == 5  # every seeded hash correctly detected, across chunks
    assert len(new_records) == 2
    assert {r.content_hash for r in new_records} == {"new_hash_a", "new_hash_b"}


@pytest.mark.asyncio
async def test_store_identity_lookup_chunks_across_batches(db_session, monkeypatch):
    """The identity-based existence lookup is chunked the same way (C7+C15
    share the same chunk size) — a batch with more identities than one
    chunk still matches every existing one correctly."""
    import backend.pipeline.storer as storer_module

    monkeypatch.setattr(storer_module, "_HASH_CHUNK_SIZE", 2)

    source, task = await _setup_source_task(db_session)

    seed_triples = [_triple(source.id, f"Seed {i}", f"seed_hash_{i}") for i in range(5)]
    seed_identities = [f"entry-{i}" for i in range(5)]
    await store_records(
        db_session, task.id, source.id, seed_triples, identities=seed_identities,
    )

    # Re-submit all 5 with edited content (same identities, new hashes) —
    # every one should be matched and updated in place, none inserted.
    edited_triples = [
        _triple(source.id, f"Seed {i} edited", f"seed_hash_{i}_v2") for i in range(5)
    ]
    records, skipped = await store_records(
        db_session, task.id, source.id, edited_triples, identities=seed_identities,
    )

    assert skipped == 0
    assert len(records) == 5
    assert {r.content_hash for r in records} == {f"seed_hash_{i}_v2" for i in range(5)}


@pytest.mark.asyncio
async def test_product_store_derives_identity_and_keeps_source_evidence_separate(db_session):
    from sqlalchemy import select

    from backend.channels.ecommerce import adapt_items
    from backend.models.record import CollectedRecord
    from backend.pipeline.normalizer import normalize_items

    source_a, task_a = await _setup_source_task(db_session, "opencli")
    source_b, task_b = await _setup_source_task(db_session, "opencli")
    raw = {
        "asin": "B000000001", "title": "Fixture",
        "product_url": "https://www.amazon.com/dp/B000000001",
        "price_value": 19.99, "currency": "USD",
    }
    products = adapt_items("amazon", "product", [raw])
    for source, task in [(source_a, task_a), (source_b, task_b)]:
        await store_records(db_session, task.id, source.id, normalize_items(products, source.id))
    updated = adapt_items("amazon", "product", [{**raw, "price_value": 29.99}])
    await store_records(db_session, task_a.id, source_a.id, normalize_items(updated, source_a.id))
    rows = (await db_session.scalars(select(CollectedRecord))).all()
    assert len(rows) == 2
    by_source = {row.source_id: row for row in rows}
    assert by_source[source_a.id].identity_key == by_source[source_b.id].identity_key
    assert by_source[source_a.id].identity_key is not None
    assert by_source[source_a.id].normalized_data["ecommerce"]["facts"]["price_value"] == 29.99
    assert by_source[source_b.id].normalized_data["ecommerce"]["facts"]["price_value"] == 19.99


@pytest.mark.asyncio
async def test_product_snapshot_rejects_stale_observations_and_tracks_accepted_run_provenance(db_session):
    from sqlalchemy import select

    from backend.channels.ecommerce import adapt_items
    from backend.models.record import CollectedRecord
    from backend.models.task import CollectionTask
    from backend.pipeline.normalizer import normalize_items

    source, initial_task = await _setup_source_task(db_session, "opencli")
    current_task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    stale_task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add_all([current_task, stale_task])
    await db_session.flush()
    raw = {"asin": "B000000001", "title": "Fixture", "product_url": "https://www.amazon.com/dp/B000000001", "currency": "USD"}

    async def observe(task, price, at, workflow):
        items = adapt_items("amazon", "product", [{**raw, "price_value": price, "fetched_at": at}])
        return await store_records(
            db_session, task.id, source.id, normalize_items(items, source.id),
            workflow_id=workflow, workflow_run_id=workflow + "-run",
            lineage={"task_id": task.id, "source_id": source.id, "collection_run_id": workflow + "-run"},
        )

    await observe(initial_task, 19.99, "2026-09-05T00:00:00Z", "original")
    await observe(current_task, 29.99, "2026-09-07T00:00:00Z", "current")
    # Same facts at a newer observation still move provenance to that accepted run.
    await observe(current_task, 29.99, "2026-09-08T00:00:00Z", "latest")
    observed = (await db_session.scalars(select(CollectedRecord))).one()
    assert observed.workflow_id == "latest"
    assert observed.task_id == current_task.id
    # Coarse clocks may report changed facts at exactly the same timestamp.
    await observe(current_task, 39.99, "2026-09-08T00:00:00Z", "same-time-change")
    for stale_price in (39.99, 9.99):
        await observe(stale_task, stale_price, "2026-09-06T00:00:00Z", "stale")
    row = (await db_session.scalars(select(CollectedRecord))).one()
    assert row.normalized_data["ecommerce"]["facts"]["price_value"] == 39.99
    assert row.normalized_data["ecommerce"]["observed_at"].startswith("2026-09-08")
    assert row.task_id == current_task.id
    assert row.workflow_id == "same-time-change"
    assert row.workflow_run_id == "same-time-change-run"
    assert row.lineage["task_id"] == current_task.id
    assert row.lineage["collection_run_id"] == "same-time-change-run"


@pytest.mark.asyncio
async def test_same_batch_product_updates_account_for_every_input_once(db_session):
    from sqlalchemy import select

    from backend.channels.ecommerce import adapt_items
    from backend.models.record import CollectedRecord
    from backend.pipeline.normalizer import normalize_items

    source, task = await _setup_source_task(db_session, "opencli")
    raw = {"asin": "B000000001", "title": "Fixture", "product_url": "https://www.amazon.com/dp/B000000001", "currency": "USD"}
    items = adapt_items("amazon", "product", [
        {**raw, "price_value": 19.99, "fetched_at": "2026-09-05T00:00:00Z"},
        {**raw, "price_value": 29.99, "fetched_at": "2026-09-06T00:00:00Z"},
        {**raw, "price_value": 39.99, "fetched_at": "2026-09-07T00:00:00Z"},
    ])
    records, skipped = await store_records(db_session, task.id, source.id, normalize_items(items, source.id))
    assert len(records) == 1
    assert skipped == 2
    assert len(records) + skipped == len(items)
    rows = (await db_session.scalars(select(CollectedRecord))).all()
    assert len(rows) == 1
    assert rows[0].normalized_data["ecommerce"]["facts"]["price_value"] == 39.99
