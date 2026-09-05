"""Frozen account-cluster persistence and cross-slice wire contracts.

Persistent request models deliberately reject unknown fields and contain no secret
credential fields. Portal payload models are transient only; callers must not put
them in commands, audit records, traces, or database JSON columns.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.browser import (
    BrowserAccountStatus,
    BrowserAuthEvidence,
    BrowserCommandKind,
    BrowserCommandStatus,
    BrowserEvidenceSource,
    BrowserLeaseStatus,
    ProfileInventoryStatus,
)


class BrowserAccountErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    ACCOUNT_IDENTITY_MISMATCH = "account_identity_mismatch"
    ACCOUNT_MIGRATION_REQUIRED = "account_migration_required"
    PROFILE_LOST = "profile_lost"
    PROFILE_CORRUPT = "profile_corrupt"
    ISOLATION_REQUIRED = "isolation_required"
    NODE_UNAVAILABLE = "node_unavailable"
    CAPABILITY_MISSING = "capability_missing"
    STALE_GENERATION = "stale_generation"
    LEASE_LOST = "lease_lost"
    LOGIN_RULE_UNKNOWN = "login_rule_unknown"
    AMBIGUOUS_LOGIN_REGION = "ambiguous_login_region"
    CREDENTIAL_POLICY_UNVERIFIED = "credential_policy_unverified"
    PERMISSION_DENIED = "permission_denied"
    SESSION_EXPIRED = "session_expired"
    SAVE_FAILED = "save_failed"


ERROR_HTTP_STATUS: dict[BrowserAccountErrorCode, int] = {
    BrowserAccountErrorCode.AUTH_REQUIRED: 409,
    BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH: 409,
    BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED: 409,
    BrowserAccountErrorCode.PROFILE_LOST: 409,
    BrowserAccountErrorCode.PROFILE_CORRUPT: 409,
    BrowserAccountErrorCode.ISOLATION_REQUIRED: 409,
    BrowserAccountErrorCode.NODE_UNAVAILABLE: 503,
    BrowserAccountErrorCode.CAPABILITY_MISSING: 409,
    BrowserAccountErrorCode.STALE_GENERATION: 409,
    BrowserAccountErrorCode.LEASE_LOST: 409,
    BrowserAccountErrorCode.LOGIN_RULE_UNKNOWN: 409,
    BrowserAccountErrorCode.AMBIGUOUS_LOGIN_REGION: 409,
    BrowserAccountErrorCode.CREDENTIAL_POLICY_UNVERIFIED: 409,
    BrowserAccountErrorCode.PERMISSION_DENIED: 403,
    BrowserAccountErrorCode.SESSION_EXPIRED: 410,
    BrowserAccountErrorCode.SAVE_FAILED: 409,
}

PUBLIC_ERROR_HTTP_STATUS = ERROR_HTTP_STATUS


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountRef(_ContractModel):
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    source_binding_revision_id: str | None = Field(default=None, min_length=1, max_length=36)


class SessionTargetV1(_ContractModel):
    tab_id: int | str | None = None
    frame_id: int | str | None = None
    document_id: int | str | None = None
    origin: str | None = Field(default=None, min_length=1, max_length=2048)


class SessionEnvelopeV1(_ContractModel):
    version: Literal[1] = 1
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    profile_version: int | None = Field(default=None, ge=1)
    node_id: str = Field(min_length=1, max_length=36)
    node_boot_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    lease_expires_at: datetime
    runtime_bundle_id: str = Field(min_length=1, max_length=36)
    runtime_bundle_version: str = Field(min_length=1, max_length=100)
    login_rule_id: str | None = Field(default=None, min_length=1, max_length=128)
    login_rule_version: str | None = Field(default=None, min_length=1, max_length=64)
    target: SessionTargetV1 = Field(default_factory=SessionTargetV1)
    view_generation: int = Field(ge=0)
    purpose: Literal["login", "execution"]
    execution_id: str | None = Field(default=None, min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=36)
    profile_state: Literal["new", "uncommitted", "committed"] = "new"
    profile_manifest_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def validate_profile_boundary(self) -> "SessionEnvelopeV1":
        if self.purpose == "execution" and (
            self.profile_state != "committed"
            or self.profile_id is None
            or self.profile_version is None
            or self.profile_manifest_id is None
        ):
            raise ValueError("execution sessions require a committed profile manifest")
        if self.profile_state == "committed" and (
            self.profile_id is None or self.profile_version is None
        ):
            raise ValueError("committed sessions require profile_id and profile_version")
        if self.target.origin is None and self.profile_state == "committed":
            raise ValueError("committed sessions require a verified target origin")
        return self


class DurableCommandV1(_ContractModel):
    command_id: str = Field(min_length=1, max_length=36)
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    node_id: str | None = Field(default=None, min_length=1, max_length=36)
    kind: BrowserCommandKind
    idempotency_scope: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    execution_id: str | None = Field(default=None, min_length=1, max_length=128)
    binding_revision_id: str | None = Field(default=None, min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    available_at: datetime
    expires_at: datetime
    status: BrowserCommandStatus = BrowserCommandStatus.QUEUED
    session_id: str | None = Field(default=None, min_length=1, max_length=36)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_deadline(self) -> "DurableCommandV1":
        if self.expires_at <= self.available_at:
            raise ValueError("expires_at must be after available_at")
        return self


class NodeClaimV1(_ContractModel):
    command_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    node_id: str = Field(min_length=1, max_length=36)
    boot_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    claimed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_deadline(self) -> "NodeClaimV1":
        if self.expires_at <= self.claimed_at:
            raise ValueError("expires_at must be after claimed_at")
        return self


class NodeResultV1(_ContractModel):
    command_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    node_id: str = Field(min_length=1, max_length=36)
    boot_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    status: Literal["succeeded", "failed", "blocked", "stopped"]
    error_code: BrowserAccountErrorCode | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    external_identity: dict[str, Any] | None = None
    profile_manifest_ref: str | None = Field(default=None, min_length=1, max_length=255)
    isolation_evidence_ref: str | None = Field(default=None, min_length=1, max_length=255)


class ProfileManifestV1(_ContractModel):
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    profile_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    node_id: str = Field(min_length=1, max_length=36)
    command_id: str = Field(min_length=1, max_length=36)
    writer_epoch: int = Field(ge=0)
    bundle: str = Field(min_length=1, max_length=100)
    browser_version: str = Field(min_length=1, max_length=100)
    files_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    checksum_manifest_ref: str = Field(min_length=1, max_length=255)
    complete_marker: str = Field(min_length=1, max_length=255)
    committed_at: datetime
    password_inventory_status: ProfileInventoryStatus


class NodeCapacityFactV1(_ContractModel):
    node_id: str = Field(min_length=1, max_length=36)
    boot_id: str = Field(min_length=1, max_length=128)
    slot_limit: int = Field(ge=0)
    occupied_slots: int = Field(ge=0)
    disk_available: int = Field(ge=0)
    observed_at: datetime
    expires_at: datetime
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capacity(self) -> "NodeCapacityFactV1":
        if self.occupied_slots > self.slot_limit:
            raise ValueError("occupied_slots cannot exceed slot_limit")
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        return self


class RefreshPolicyV1(_ContractModel):
    trigger: Literal["interval", "qr_expired", "navigation", "manual"]
    interval: int = Field(ge=1, le=86_400)
    max_attempts: int = Field(ge=0, le=100)


class RuleLocatorV1(_ContractModel):
    id: str = Field(min_length=1, max_length=128)
    selector: str = Field(min_length=1, max_length=2048)
    kind: Literal["qr", "form", "identity", "authenticated_state"]
    frame: str | None = Field(default=None, max_length=255)
    region: dict[str, int] | None = None


class RuleSuccessV1(_ContractModel):
    authenticated_evidence: list[str] = Field(min_length=1, max_length=16)
    identity_extractor: str = Field(min_length=1, max_length=128)
    page_state: str = Field(min_length=1, max_length=128)


class LoginRuleV1(_ContractModel):
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=100)
    allowed_origins: list[str] = Field(min_length=1, max_length=32)
    login_url: str = Field(min_length=1, max_length=2048)
    allowed_redirect_origins: list[str] = Field(default_factory=list, max_length=32)
    frames: list[str] = Field(default_factory=list, max_length=32)
    modes: list[Literal["qr", "form", "native"]] = Field(min_length=1, max_length=8)
    selectors: list[RuleLocatorV1] = Field(default_factory=list, max_length=64)
    sensitive_regions: list[dict[str, int]] = Field(default_factory=list, max_length=32)
    refresh: RefreshPolicyV1
    success: RuleSuccessV1
    challenge_conditions: list[str] = Field(default_factory=list, max_length=32)


class LoginObservationV1(_ContractModel):
    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    rule_id: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=64)
    target: SessionTargetV1
    view_generation: int = Field(ge=0)
    state: Literal[
        "opening",
        "presenting",
        "refreshing",
        "verifying",
        "challenge",
        "unknown",
        "saving",
        "saved",
        "error",
    ]
    evidence_kind: BrowserAuthEvidence
    external_identity: dict[str, Any] | None = None
    observed_at: datetime
    error_code: BrowserAccountErrorCode | None = None


class PortalControlMessageV1(_ContractModel):
    """Transient control message; never persist or include in command payloads."""

    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    target: SessionTargetV1
    view_generation: int = Field(ge=0)
    sequence: int = Field(ge=1)
    kind: Literal["field_input", "pointer", "key", "request_view", "takeover"]
    field_ref: str | None = Field(default=None, min_length=1, max_length=128)
    sensitive_payload: dict[str, Any] | None = None


class PortalPixelFrameV1(_ContractModel):
    """Transient clipped frame; only approved regions may be sent to the client."""

    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    view_generation: int = Field(ge=0)
    sequence: int = Field(ge=1)
    region_kind: Literal["qr", "form", "approved"]
    expires_at: datetime
    clip: dict[str, int] = Field(min_length=1)
    masked_regions: list[dict[str, int]] = Field(default_factory=list)
    frame_bytes: bytes = Field(min_length=1)


class PortalTransientV1(_ContractModel):
    """Envelope for in-memory portal transport; not a persistence model."""

    control: PortalControlMessageV1 | None = None
    pixel: PortalPixelFrameV1 | None = None

    @model_validator(mode="after")
    def require_one_message(self) -> "PortalTransientV1":
        if (self.control is None) == (self.pixel is None):
            raise ValueError("portal transient must contain exactly one control or pixel message")
        return self


class BrowserAccountCreate(_ContractModel):
    workspace_id: str = Field(min_length=1, max_length=36)
    site: str = Field(min_length=1, max_length=255)
    label: str = Field(min_length=1, max_length=255)
    node_id: str | None = Field(default=None, min_length=1, max_length=36)
    runtime_bundle_id: str | None = Field(default=None, min_length=1, max_length=36)
    login_rule_id: str | None = Field(default=None, min_length=1, max_length=128)
    login_rule_version: str | None = Field(default=None, min_length=1, max_length=64)


class BrowserAccountRead(_ContractModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    workspace_id: str
    site: str
    label: str
    node_id: str | None
    profile_id: str | None
    profile_version: int | None
    profile_manifest_id: str | None
    runtime_bundle_id: str | None
    runtime_bundle_version: str | None
    login_rule_id: str | None
    login_rule_version: str | None
    auth_required: bool
    platform_identity: dict[str, Any] | None
    auth_evidence: BrowserAuthEvidence
    evidence_source: BrowserEvidenceSource | None
    evidence_observed_at: datetime | None
    manual_confirmed_by: str | None
    status: BrowserAccountStatus
    revision: int
    status_reason_code: str | None
    created_at: datetime
    updated_at: datetime


class BrowserAccountLeaseRead(_ContractModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    workspace_id: str
    account_id: str
    node_id: str
    node_boot_id: str
    lease_id: str
    epoch: int
    owner_id: str
    status: BrowserLeaseStatus
    acquired_at: datetime
    renewed_at: datetime | None
    expires_at: datetime
    released_at: datetime | None
    isolation_evidence_ref: str | None


class BrowserAccountUpdate(_ContractModel):
    auth_required: bool | None = None
    status: BrowserAccountStatus | None = None
    status_reason_code: BrowserAccountErrorCode | None = None
    expected_revision: int = Field(ge=0)


class BrowserLoginSessionRead(_ContractModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    workspace_id: str
    account_id: str
    instance_id: str | None
    node_id: str | None
    node_boot_id: str | None
    lease_id: str | None
    epoch: int
    profile_id: str | None
    profile_version: int | None
    profile_state: Literal["new", "uncommitted", "committed"]
    login_rule_id: str | None
    login_rule_version: str | None
    tab_id: str | None
    frame_id: str | None
    document_id: str | None
    origin: str | None
    view_generation: int
    purpose: Literal["login", "execution"]
    execution_id: str | None
    command_id: str | None
    status: BrowserAccountStatus
    expires_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DurableCommandRead(_ContractModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    workspace_id: str
    account_id: str
    node_id: str | None
    kind: BrowserCommandKind
    idempotency_scope: str
    idempotency_key: str
    execution_id: str | None
    binding_revision_id: str | None
    epoch: int
    expected_revision: int
    available_at: datetime
    expires_at: datetime
    status: BrowserCommandStatus
    session_id: str | None
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error_code: BrowserAccountErrorCode | None
    claimed_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProfileManifestRead(_ContractModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    workspace_id: str
    account_id: str
    profile_id: str
    version: int
    node_id: str
    command_id: str
    writer_epoch: int
    bundle_name: str
    browser_version: str
    files_count: int
    total_bytes: int
    checksum_manifest_ref: str
    complete_marker: str
    committed_at: datetime
    password_inventory_status: ProfileInventoryStatus
    state: str
    created_at: datetime
    updated_at: datetime


class NodeCapacityFactRead(_ContractModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: str
    node_id: str
    boot_id: str
    slot_limit: int
    occupied_slots: int
    disk_available: int
    observed_at: datetime
    expires_at: datetime
    capabilities: dict[str, Any]
    valid: bool
    created_at: datetime
    updated_at: datetime


# Short aliases used by service consumers while the V1 wire names remain the
# canonical serialization contract.
NodeClaim = NodeClaimV1
NodeResult = NodeResultV1
DurableCommand = DurableCommandV1
LoginRule = LoginRuleV1
LoginObservation = LoginObservationV1
