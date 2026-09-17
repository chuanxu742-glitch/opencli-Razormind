"""Opt-in contract for append-only GEO answer observations."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class GeoObservationCaptureConfig(BaseModel):
    """The only accepted source-config opt-in for GEO answer sampling."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["1"]
    mode: Literal["append_per_run"]


def geo_observation_capture_enabled(
    channel_type: str, channel_config: dict[str, Any] | None
) -> bool:
    """Validate and resolve the explicit GEO sampling opt-in.

    It is intentionally limited to the answer-producing Doubao adapter.  A
    normal content source must never become append-only merely because its
    output happens to repeat.
    """
    config = channel_config or {}
    capture = config.get("observation_capture")
    if capture is None:
        return False
    if channel_type != "doubao_research":
        raise ValueError(
            "observation_capture is supported only for doubao_research sources"
        )
    try:
        GeoObservationCaptureConfig.model_validate(capture)
    except ValidationError as exc:
        raise ValueError(
            "observation_capture must be {\"version\": \"1\", "
            "\"mode\": \"append_per_run\"}"
        ) from exc
    return True
