"""Binary framing and fenced route contracts for full browser desktops.

The desktop transport is intentionally separate from the QR portal protocol.
It carries an opaque RFB byte stream and never accepts a host or port from a
browser client or from a center-issued route.
"""

from __future__ import annotations

import struct
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DESKTOP_MAGIC = b"BDS1"
DESKTOP_PROTOCOL = "opencli.browser-desktop.v1"
DESKTOP_MAX_PAYLOAD_BYTES = 64 * 1024
DESKTOP_MAX_BUFFERED_FRAMES = 16
_HEADER = struct.Struct("!4s16sBQI")


class BrowserDesktopRouteV1(BaseModel):
    """Center-owned route bound to one authenticated node connection."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["opencli.browser-desktop.v1"] = DESKTOP_PROTOCOL
    route_id: str = Field(min_length=36, max_length=36)
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    profile_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=36)
    boot_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=36)
    command_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    tunnel_handle: str = Field(min_length=1, max_length=255)
    tunnel_auth_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime

    @model_validator(mode="after")
    def validate_route(self) -> BrowserDesktopRouteV1:
        uuid.UUID(self.route_id)
        expires_at = (
            self.expires_at
            if self.expires_at.tzinfo is not None
            else self.expires_at.replace(tzinfo=UTC)
        )
        if expires_at <= datetime.now(UTC):
            raise ValueError("desktop route is expired")
        return self


def encode_desktop_frame(
    route_id: str,
    direction: Literal[0, 1],
    sequence: int,
    payload: bytes,
) -> bytes:
    """Encode one bounded RFB stream chunk with monotonic routing metadata."""

    if isinstance(direction, bool) or direction not in {0, 1}:
        raise ValueError("desktop frame direction is invalid")
    if isinstance(sequence, bool) or sequence < 1:
        raise ValueError("desktop frame sequence must be positive")
    if not payload or len(payload) > DESKTOP_MAX_PAYLOAD_BYTES:
        raise ValueError("desktop frame payload is outside the limit")
    route_bytes = uuid.UUID(route_id).bytes
    return _HEADER.pack(DESKTOP_MAGIC, route_bytes, direction, sequence, len(payload)) + payload


def decode_desktop_frame(data: bytes) -> tuple[str, Literal[0, 1], int, bytes]:
    """Decode one exact frame and reject truncation, padding, or wrong magic."""

    if len(data) < _HEADER.size:
        raise ValueError("desktop frame is truncated")
    magic, route_bytes, direction, sequence, payload_length = _HEADER.unpack_from(data)
    if magic != DESKTOP_MAGIC:
        raise ValueError("desktop frame magic is invalid")
    if direction not in {0, 1}:
        raise ValueError("desktop frame direction is invalid")
    if sequence < 1:
        raise ValueError("desktop frame sequence must be positive")
    if not 0 < payload_length <= DESKTOP_MAX_PAYLOAD_BYTES:
        raise ValueError("desktop frame payload is outside the limit")
    if len(data) != _HEADER.size + payload_length:
        raise ValueError("desktop frame length is invalid")
    return str(uuid.UUID(bytes=route_bytes)), direction, sequence, data[_HEADER.size :]


def desktop_frame_wire_limit() -> int:
    return _HEADER.size + DESKTOP_MAX_PAYLOAD_BYTES

