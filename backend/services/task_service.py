from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.task import CollectionTask, TaskRun


async def create_task(
    session: AsyncSession,
    source_id: str,
    trigger_type: str,
    parameters: dict,
    priority: int = 5,
    agent_id: str | None = None,
    requested_by_user_id: str | None = None,
    task_id: str | None = None,
    retry_of_task_id: str | None = None,
    recovery_mode: str | None = None,
    recovery_reason: str | None = None,
    initiating_actor: str | None = None,
    recovery_idempotency_key: str | None = None,
) -> CollectionTask:
    values = dict(
        source_id=source_id,
        agent_id=agent_id,
        trigger_type=trigger_type,
        requested_by_user_id=requested_by_user_id,
        parameters=parameters,
        priority=priority,
        status="pending",
        retry_of_task_id=retry_of_task_id,
        recovery_mode=recovery_mode,
        recovery_reason=recovery_reason,
        initiating_actor=initiating_actor,
        recovery_idempotency_key=recovery_idempotency_key,
    )
    if task_id is not None:
        values["id"] = task_id
    task = CollectionTask(**values)
    session.add(task)
    await session.flush()
    await session.refresh(task)
    return task


async def get_task(session: AsyncSession, task_id: str) -> CollectionTask | None:
    result = await session.execute(select(CollectionTask).where(CollectionTask.id == task_id))
    return result.scalar_one_or_none()


async def get_recovery_by_idempotency_key(
    session: AsyncSession, key: str
) -> CollectionTask | None:
    result = await session.execute(
        select(CollectionTask).where(CollectionTask.recovery_idempotency_key == key)
    )
    return result.scalar_one_or_none()


async def get_inflight_recovery(
    session: AsyncSession, task_id: str
) -> CollectionTask | None:
    result = await session.execute(
        select(CollectionTask)
        .where(
            CollectionTask.retry_of_task_id == task_id,
            CollectionTask.status.in_(["pending", "running"]),
        )
        .order_by(CollectionTask.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_tasks(
    session: AsyncSession,
    source_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[CollectionTask], int]:
    query = select(CollectionTask).order_by(CollectionTask.created_at.desc())
    count_query = select(func.count()).select_from(CollectionTask)

    if source_id:
        query = query.where(CollectionTask.source_id == source_id)
        count_query = count_query.where(CollectionTask.source_id == source_id)
    if status:
        query = query.where(CollectionTask.status == status)
        count_query = count_query.where(CollectionTask.status == status)

    total = (await session.execute(count_query)).scalar_one()
    offset = (page - 1) * limit
    result = await session.execute(query.offset(offset).limit(limit))
    return result.scalars().all(), total


async def list_task_runs(
    session: AsyncSession,
    task_id: str,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[TaskRun], int]:
    count_query = select(func.count()).select_from(TaskRun).where(TaskRun.task_id == task_id)
    total = (await session.execute(count_query)).scalar_one()

    result = await session.execute(
        select(TaskRun)
        .where(TaskRun.task_id == task_id)
        .order_by(TaskRun.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    return result.scalars().all(), total
