from datetime import datetime
from typing import Any

from backend.schemas.common import UTCModel


class GeoAnswerObservationRead(UTCModel):
    id: str
    task_id: str
    task_run_id: str
    source_id: str
    provider: str
    observed_at: datetime
    content_hash: str
    raw_data: dict[str, Any]
    normalized_data: dict[str, Any]
    lineage: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
