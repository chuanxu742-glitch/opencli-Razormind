from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.models.geo_observation import GeoAnswerObservation
from backend.models.record import CollectedRecord
from backend.models.source import DataSource
from backend.models.task import CollectionTask, TaskRun
from backend.pipeline.geo_observations import geo_observation_capture_enabled
from backend.pipeline.storer import store_records


def _triple(source_id: str) -> tuple[dict, dict, str]:
    return (
        {"title": "same question", "answer": "same answer"},
        {
            "title": "same question",
            "content": "same answer",
            "source_id": source_id,
        },
        "a" * 64,
    )


@pytest.mark.asyncio
async def test_opted_in_geo_answer_is_preserved_for_each_task_run(db_session):
    source = DataSource(
        name="GEO answer source",
        channel_type="doubao_research",
        channel_config={"question": "same question"},
    )
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()
    first_run = TaskRun(task_id=task.id, status="completed")
    second_run = TaskRun(task_id=task.id, status="completed")
    db_session.add_all([first_run, second_run])
    await db_session.flush()

    first_seen = datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC)
    second_seen = datetime(2026, 9, 15, 4, 5, 6, tzinfo=UTC)
    first, first_skipped = await store_records(
        db_session,
        task.id,
        source.id,
        [_triple(source.id)],
        channel_type="doubao_research",
        capture_geo_observations=True,
        task_run_id=first_run.id,
        observed_at=first_seen,
    )
    second, second_skipped = await store_records(
        db_session,
        task.id,
        source.id,
        [_triple(source.id)],
        channel_type="doubao_research",
        capture_geo_observations=True,
        task_run_id=second_run.id,
        observed_at=second_seen,
    )

    assert len(first) == 1
    assert first_skipped == 0
    assert second == []
    assert second_skipped == 1
    assert len((await db_session.execute(select(CollectedRecord))).scalars().all()) == 1

    observations = (
        await db_session.execute(
            select(GeoAnswerObservation).order_by(GeoAnswerObservation.observed_at)
        )
    ).scalars().all()
    assert [
        (item.task_id, item.task_run_id, item.observed_at.replace(tzinfo=UTC))
        for item in observations
    ] == [
        (task.id, first_run.id, first_seen),
        (task.id, second_run.id, second_seen),
    ]
    assert all(item.source_id == source.id for item in observations)
    assert all(item.raw_data["answer"] == "same answer" for item in observations)


@pytest.mark.asyncio
async def test_geo_observation_capture_is_idempotent_within_one_task_run(db_session):
    source = DataSource(name="GEO", channel_type="doubao_research", channel_config={})
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()
    run = TaskRun(task_id=task.id, status="completed")
    db_session.add(run)
    await db_session.flush()

    kwargs = {
        "channel_type": "doubao_research",
        "capture_geo_observations": True,
        "task_run_id": run.id,
    }
    await store_records(db_session, task.id, source.id, [_triple(source.id)], **kwargs)
    await store_records(db_session, task.id, source.id, [_triple(source.id)], **kwargs)

    observations = (await db_session.execute(select(GeoAnswerObservation))).scalars().all()
    assert len(observations) == 1


def test_geo_observation_config_is_explicit_and_limited_to_doubao():
    config = {"observation_capture": {"version": "1", "mode": "append_per_run"}}
    assert geo_observation_capture_enabled("doubao_research", config) is True
    assert geo_observation_capture_enabled("doubao_research", {}) is False
    with pytest.raises(ValueError, match="only for doubao_research"):
        geo_observation_capture_enabled("rss", config)
    with pytest.raises(ValueError, match="append_per_run"):
        geo_observation_capture_enabled(
            "doubao_research", {"observation_capture": {"version": "1"}}
        )


@pytest.mark.asyncio
async def test_geo_observation_capture_rejects_odp_only_before_collection(db_session):
    from backend.pipeline.pipeline import run_pipeline

    source = DataSource(
        name="GEO ODP-only",
        channel_type="doubao_research",
        channel_config={
            "question": "same question",
            "observation_capture": {"version": "1", "mode": "append_per_run"},
        },
        write_strategy="odp_only",
    )
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()

    with patch("backend.pipeline.collector.collect", new=AsyncMock()) as collect:
        result = await run_pipeline(
            task.id, source, enable_ai=False, enable_notifications=False
        )

    assert result.success is False
    assert "write_strategy=odp_only" in result.error
    collect.assert_not_awaited()
