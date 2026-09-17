from datetime import UTC, datetime

import pytest

from backend.models.geo_observation import GeoAnswerObservation
from backend.models.source import DataSource
from backend.models.task import CollectionTask, TaskRun


@pytest.mark.asyncio
async def test_geo_observations_api_filters_by_source_and_exposes_lineage(client, db_session):
    source = DataSource(name="GEO", channel_type="doubao_research", channel_config={})
    other_source = DataSource(name="Other", channel_type="doubao_research", channel_config={})
    db_session.add_all([source, other_source])
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    other_task = CollectionTask(source_id=other_source.id, trigger_type="manual", parameters={})
    db_session.add_all([task, other_task])
    await db_session.flush()
    run = TaskRun(task_id=task.id, status="completed")
    other_run = TaskRun(task_id=other_task.id, status="completed")
    db_session.add_all([run, other_run])
    await db_session.flush()
    db_session.add_all(
        [
            GeoAnswerObservation(
                task_id=task.id,
                task_run_id=run.id,
                source_id=source.id,
                provider="doubao_research",
                observed_at=datetime(2026, 9, 8, tzinfo=UTC),
                content_hash="a" * 64,
                raw_data={"answer": "answer"},
                normalized_data={"content": "answer"},
                lineage={"collection_run_id": run.id},
            ),
            GeoAnswerObservation(
                task_id=other_task.id,
                task_run_id=other_run.id,
                source_id=other_source.id,
                provider="doubao_research",
                observed_at=datetime(2026, 9, 9, tzinfo=UTC),
                content_hash="b" * 64,
                raw_data={"answer": "other"},
                normalized_data={"content": "other"},
            ),
        ]
    )
    await db_session.flush()

    response = await client.get(f"/api/v1/geo-observations?source_id={source.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == 1
    assert payload["data"][0]["source_id"] == source.id
    assert payload["data"][0]["task_id"] == task.id
    assert payload["data"][0]["task_run_id"] == run.id
    assert payload["data"][0]["lineage"] == {"collection_run_id": run.id}
