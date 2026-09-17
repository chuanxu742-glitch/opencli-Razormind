"""Durable, append-only snapshots of configured GEO answer observations."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import TimestampMixin

if TYPE_CHECKING:
    from backend.models.task import CollectionTask, TaskRun


class GeoAnswerObservation(TimestampMixin):
    """One answer observed from one opted-in source during one task run.

    This is deliberately separate from ``collected_records``.  The latter is
    a current-content projection with cross-run content de-duplication; GEO
    evaluation needs a time series in which an unchanged answer remains
    observable on every independently executed sample.
    """

    __tablename__ = "geo_answer_observations"
    __table_args__ = (
        UniqueConstraint(
            "task_run_id", "source_id", "content_hash",
            name="uq_geo_observation_run_source_content",
        ),
        Index("ix_geo_observations_source_observed_at", "source_id", "observed_at"),
        Index("ix_geo_observations_task_id", "task_id"),
    )

    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("collection_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Collector-owned timestamp set immediately after a successful provider
    # response, rather than a timestamp reconstructed during a later read.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    normalized_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    lineage: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    task: Mapped["CollectionTask"] = relationship("CollectionTask")
    task_run: Mapped["TaskRun"] = relationship("TaskRun")
