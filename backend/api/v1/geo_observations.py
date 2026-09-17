"""Read API for append-only GEO answer observations."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.schemas.common import ApiResponse, PaginationMeta
from backend.schemas.geo_observation import GeoAnswerObservationRead
from backend.services.geo_observation_service import list_geo_answer_observations

router = APIRouter(prefix="/geo-observations", tags=["geo-observations"])


@router.get("", response_model=ApiResponse[list[GeoAnswerObservationRead]])
async def list_observations(
    source_id: str | None = None,
    task_id: str | None = None,
    task_run_id: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    observations, total = await list_geo_answer_observations(
        db,
        source_id=source_id,
        task_id=task_id,
        task_run_id=task_run_id,
        page=page,
        limit=limit,
    )
    return ApiResponse.ok(
        data=[GeoAnswerObservationRead.model_validate(item) for item in observations],
        meta=PaginationMeta(
            total=total, page=page, limit=limit, pages=max(1, -(-total // limit))
        ),
    )
