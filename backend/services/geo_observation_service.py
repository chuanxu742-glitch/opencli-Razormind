from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.geo_observation import GeoAnswerObservation


async def list_geo_answer_observations(
    session: AsyncSession,
    *,
    source_id: str | None = None,
    task_id: str | None = None,
    task_run_id: str | None = None,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[GeoAnswerObservation], int]:
    query = select(GeoAnswerObservation).order_by(
        GeoAnswerObservation.observed_at.desc(), GeoAnswerObservation.id.desc()
    )
    count_query = select(func.count()).select_from(GeoAnswerObservation)
    filters = []
    if source_id:
        filters.append(GeoAnswerObservation.source_id == source_id)
    if task_id:
        filters.append(GeoAnswerObservation.task_id == task_id)
    if task_run_id:
        filters.append(GeoAnswerObservation.task_run_id == task_run_id)
    for criterion in filters:
        query = query.where(criterion)
        count_query = count_query.where(criterion)
    total = (await session.execute(count_query)).scalar_one()
    rows = await session.execute(query.offset((page - 1) * limit).limit(limit))
    return list(rows.scalars().all()), total
