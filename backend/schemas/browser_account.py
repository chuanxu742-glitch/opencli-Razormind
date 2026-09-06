"""Frozen account-cluster persistence and cross-slice wire contracts.

Persistent request models deliberately reject unknown fields and contain no secret
credential fields. Portal payload models are transient only; callers must not put
them in commands, audit records, traces, or database JSON columns.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

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
    BrowserAccountErrorCode.CAPABILITY_MISSING: 503,
    BrowserAccountErrorCode.STALE_GENERATION: 409,
    BrowserAccountErrorCode.LEASE_LOST: 409,
    BrowserAccountErrorCode.LOGIN_RULE_UNKNOWN: 409,
    BrowserAccountErrorCode.AMBIGUOUS_LOGIN_REGION: 409,
    BrowserAccountErrorCode.CREDENTIAL_POLICY_UNVERIFIED: 409,
    BrowserAccountErrorCode.PERMISSION_DENIED: 403,
    BrowserAccountErrorCode.SESSION_EXPIRED: 410,
    BrowserAccountErrorCode.SAVE_FAILED: 409,
}
ACCOUNT_API_ROUTE = "/api/v1/workspaces/{workspace_id}/browser-accounts"
LOGIN_SESSIONS_ROUTE = f"{ACCOUNT_API_ROUTE}/{{account_id}}/login-sessions"
LOGIN_SESSION_ROUTE = f"{LOGIN_SESSIONS_ROUTE}/{{session_id}}"
LOGIN_SESSION_VIEW_ROUTE = f"{LOGIN_SESSION_ROUTE}/view"
LOGIN_SESSION_TAKEOVER_ROUTE = f"{LOGIN_SESSION_ROUTE}/takeover"
LOGIN_SESSION_CONFIRM_ROUTE = f"{LOGIN_SESSION_ROUTE}/confirm"
LOGIN_SESSION_CLOSE_ROUTE = f"{LOGIN_SESSION_ROUTE}/close"
PORTAL_TICKET_ROUTE = f"{LOGIN_SESSION_ROUTE}/portal-ticket"
PORTAL_TICKET_ISSUE_ROUTE = f"{PORTAL_TICKET_ROUTE}/issue"
PORTAL_TICKET_REDEEM_ROUTE = f"{PORTAL_TICKET_ROUTE}/redeem"
PORTAL_WS_ROUTE = f"{LOGIN_SESSION_ROUTE}/portal"
IDEMPOTENCY_HEADER = "Idempotency-Key"


QRAC2_CONTRACT_VERSION = 1
MAX_PORTAL_FRAME_BYTES = 4_000_000
MAX_PORTAL_INPUT_BYTES = 4_096
REVISION_HEADER = "If-Match"


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _sanitized_validation_error(type(self), exc) from None

    def to_wire(self) -> dict[str, Any]:
        """Validate, normalize, and serialize one canonical contract value."""
        return type(self).model_validate(self.model_dump(mode="python")).model_dump(
            mode="json", exclude_none=True
        )

    @classmethod
    def from_wire(cls, value: Any) -> Any:
        """Validate one canonical wire value before it reaches a consumer."""
        try:
            return cls.model_validate(value)
        except ValidationError as exc:
            raise _sanitized_validation_error(cls, exc) from None


def _sanitized_validation_error(model: type[BaseModel], exc: ValidationError) -> ValidationError:
    """Do not let secret-bearing input values appear in contract errors."""
    line_errors = []
    for error in exc.errors(include_context=False):
        line_errors.append(
            {
                "type": "value_error",
                "loc": error["loc"],
                "msg": "Value error, invalid contract",
                "input": None,
                "ctx": {"error": ValueError("invalid contract")},
            }
        )
    return ValidationError.from_exception_data(model.__name__, line_errors)


class AccountRef(_ContractModel):
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    source_binding_revision_id: str | None = Field(default=None, min_length=1, max_length=36)

class ExecutionContextV1(_ContractModel):
    """Trusted execution/run references supplied by the existing execution chain."""

    account_ref: AccountRef
    execution_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    caller_id: str = Field(min_length=1, max_length=128)
    source_binding_revision_id: str | None = Field(
        default=None, min_length=1, max_length=36
    )


class BrowserLoginSessionCreateV1(_ContractModel):
    """Body for creating one leased login or execution session."""

    purpose: Literal["login", "execution"]
    execution_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_binding_revision_id: str | None = Field(
        default=None, min_length=1, max_length=36
    )
    expected_revision: int = Field(ge=0)


class SessionTargetV1(_ContractModel):
    tab_id: int | str | None = None
    frame_id: int | str | None = None
    document_id: int | str | None = None
    origin: str | None = Field(default=None, min_length=1, max_length=2048)

    def is_complete(self) -> bool:
        return all(
            value is not None
            for value in (self.tab_id, self.frame_id, self.document_id, self.origin)
        )

    def require_complete(self) -> "SessionTargetV1":
        if not self.is_complete():
            raise ValueError("page operations require a complete real target")
        return self


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
        return self
class SessionResolutionWaitingV1(_ContractModel):
    status: Literal["waiting"] = "waiting"
    account_ref: AccountRef
    account_revision: int = Field(ge=0)
    reason: Literal[
        "node_unavailable",
        "capacity_missing",
        "lease_waiting",
        "profile_restore_waiting",
    ]
    retry_at: datetime | None = None


class SessionResolutionBlockedV1(_ContractModel):
    status: Literal["blocked"] = "blocked"
    account_ref: AccountRef
    account_revision: int = Field(ge=0)
    error_code: BrowserAccountErrorCode


SessionResolutionV1 = SessionEnvelopeV1 | SessionResolutionWaitingV1 | SessionResolutionBlockedV1


class EmptyCommandPayloadV1(_ContractModel):
    """Payload for commands whose context is carried by the outer envelope."""


class LoginRuleCommandPayloadV1(_ContractModel):
    login_rule_id: str = Field(min_length=1, max_length=128)
    login_rule_version: str = Field(min_length=1, max_length=64)


class RefreshLoginCommandPayloadV1(_ContractModel):
    trigger: Literal["interval", "qr_expired", "navigation", "manual"]
    expected_view_generation: int = Field(ge=0)


class StopAndSaveCommandPayloadV1(_ContractModel):
    expected_profile_version: int | None = Field(default=None, ge=1)
    profile_manifest_ref: str | None = Field(default=None, min_length=1, max_length=255)


class ExecuteReferenceCommandPayloadV1(_ContractModel):
    execution_id: str = Field(min_length=1, max_length=128)
    source_binding_revision_id: str | None = Field(default=None, min_length=1, max_length=36)


class CloseSessionCommandPayloadV1(_ContractModel):
    reason: Literal["completed", "cancelled", "expired", "error"]


class IsolateCommandPayloadV1(_ContractModel):
    isolation_evidence_ref: str = Field(min_length=1, max_length=255)


class MigrateCommandPayloadV1(_ContractModel):
    target_node_id: str = Field(min_length=1, max_length=36)
    snapshot_ref: str = Field(min_length=1, max_length=255)


CommandPayloadV1 = (
    EmptyCommandPayloadV1
    | LoginRuleCommandPayloadV1
    | RefreshLoginCommandPayloadV1
    | StopAndSaveCommandPayloadV1
    | ExecuteReferenceCommandPayloadV1
    | CloseSessionCommandPayloadV1
    | IsolateCommandPayloadV1
    | MigrateCommandPayloadV1
)


_COMMAND_PAYLOAD_TYPES: dict[BrowserCommandKind, type[_ContractModel]] = {
    BrowserCommandKind.START_LOGIN: EmptyCommandPayloadV1,
    BrowserCommandKind.APPLY_LOGIN_RULE: LoginRuleCommandPayloadV1,
    BrowserCommandKind.REFRESH_LOGIN: RefreshLoginCommandPayloadV1,
    BrowserCommandKind.STOP_AND_SAVE: StopAndSaveCommandPayloadV1,
    BrowserCommandKind.EXECUTE_REFERENCE: ExecuteReferenceCommandPayloadV1,
    BrowserCommandKind.CLOSE_SESSION: CloseSessionCommandPayloadV1,
    BrowserCommandKind.ISOLATE: IsolateCommandPayloadV1,
    BrowserCommandKind.MIGRATE: MigrateCommandPayloadV1,
}


class DurableCommandV1(_ContractModel):
    # Version is frozen before persistence; unsupported contracts fail here.
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
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
    # Captured from BrowserAccount.revision at enqueue; never from membership
    # revision or view_generation.
    expected_revision: int = Field(ge=0)
    available_at: datetime
    expires_at: datetime
    status: BrowserCommandStatus = BrowserCommandStatus.QUEUED
    session_id: str | None = Field(default=None, min_length=1, max_length=36)
    payload: CommandPayloadV1 = Field(default_factory=EmptyCommandPayloadV1)

    @model_validator(mode="before")
    @classmethod
    def validate_payload_for_kind(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        kind_value = values.get("kind")
        payload = values.get("payload", {})
        try:
            kind = BrowserCommandKind(kind_value)
            payload_type = _COMMAND_PAYLOAD_TYPES[kind]
            values = dict(values)
            values["payload"] = payload_type.model_validate(payload)
        except (KeyError, TypeError, ValueError):
            raise ValueError("unsupported command payload")
        return values

    @model_validator(mode="after")
    def validate_payload_type(self) -> "DurableCommandV1":
        expected_type = _COMMAND_PAYLOAD_TYPES[self.kind]
        if not isinstance(self.payload, expected_type):
            raise ValueError("command payload does not match command kind")
        if self.expires_at <= self.available_at:
            raise ValueError("expires_at must be after available_at")
        return self




class NodeClaimV1(_ContractModel):
    # Claim identity is carried from the durable command, not inferred from URL.
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
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
class NodeIdentityV1(_ContractModel):
    """Authenticated node identity; URL and client-supplied fleet tokens are not identity."""

    node_id: str = Field(min_length=1, max_length=36)
    boot_id: str = Field(min_length=1, max_length=128)


class ClaimedCommandV1(_ContractModel):
    """Single scheduler result reused by every downstream execution consumer."""

    command: DurableCommandV1
    claim: NodeClaimV1
    session: SessionEnvelopeV1


class CommandExecutionGuardV1(_ContractModel):
    """Pre-side-effect admission gate for one frozen command/claim/session tuple."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    command: DurableCommandV1
    claim: NodeClaimV1
    session: SessionEnvelopeV1

    @model_validator(mode="after")
    def validate_association_before_side_effects(self) -> "CommandExecutionGuardV1":
        if self.command.contract_version != self.contract_version:
            raise ValueError("unsupported command contract version")
        if self.claim.contract_version != self.contract_version:
            raise ValueError("unsupported claim contract version")
        if self.command.workspace_id != self.claim.workspace_id:
            raise ValueError("command and claim workspace mismatch")
        if self.command.account_id != self.claim.account_id:
            raise ValueError("command and claim account mismatch")
        if self.command.command_id != self.claim.command_id:
            raise ValueError("command and claim id mismatch")
        if self.command.session_id != self.claim.session_id:
            raise ValueError("command and claim session mismatch")
        if self.command.epoch != self.claim.epoch:
            raise ValueError("command and claim epoch mismatch")
        if self.command.expected_revision != self.claim.expected_revision:
            raise ValueError("command and claim revision mismatch")
        if self.session.workspace_id != self.claim.workspace_id:
            raise ValueError("claim and session workspace mismatch")
        if self.session.account_id != self.claim.account_id:
            raise ValueError("claim and session account mismatch")
        if self.session.session_id != self.claim.session_id:
            raise ValueError("claim and session id mismatch")
        if self.session.epoch != self.claim.epoch:
            raise ValueError("claim and session epoch mismatch")
        if self.session.node_id != self.claim.node_id:
            raise ValueError("claim and session node mismatch")
        if self.session.node_boot_id != self.claim.boot_id:
            raise ValueError("claim and session boot mismatch")
        if self.session.version != self.contract_version:
            raise ValueError("unsupported session contract version")
        if self.command.node_id is not None and self.command.node_id != self.claim.node_id:
            raise ValueError("command and claim node mismatch")
        if self.command.kind == BrowserCommandKind.APPLY_LOGIN_RULE:
            payload = self.command.payload
            assert isinstance(payload, LoginRuleCommandPayloadV1)
            if (
                self.session.login_rule_id != payload.login_rule_id
                or self.session.login_rule_version != payload.login_rule_version
            ):
                raise ValueError("stale login rule version")
        if self.command.kind == BrowserCommandKind.REFRESH_LOGIN:
            payload = self.command.payload
            assert isinstance(payload, RefreshLoginCommandPayloadV1)
            if payload.expected_view_generation != self.session.view_generation:
                raise ValueError("stale view generation")
        return self


class ExternalIdentityV1(_ContractModel):
    """Minimal non-secret identity proof; never includes cookies or tokens."""

    provider: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, min_length=1, max_length=255)


class NodeEvidenceV1(_ContractModel):
    """Allowlisted operational evidence, not raw exceptions or page content."""

    auth_evidence: BrowserAuthEvidence | None = None
    runtime_status: Literal["healthy", "stopped", "unavailable", "unknown"] | None = None
    state: Literal["opening", "presenting", "refreshing", "verifying", "challenge", "unknown", "saving", "saved", "error"] | None = None
    view_generation: int | None = Field(default=None, ge=0)
    profile_manifest_ref: str | None = Field(default=None, min_length=1, max_length=255)
    isolation_evidence_ref: str | None = Field(default=None, min_length=1, max_length=255)
    reason_code: BrowserAccountErrorCode | None = None


class NodeResultV1(_ContractModel):
    # Results are admitted only as the frozen v1 envelope.
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    command_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    node_id: str = Field(min_length=1, max_length=36)
    boot_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    status: Literal["succeeded", "failed", "blocked", "stopped"]
    error_code: BrowserAccountErrorCode | None = None
    evidence: NodeEvidenceV1 = Field(default_factory=NodeEvidenceV1)
    external_identity: ExternalIdentityV1 | None = None
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
    """Rule observation handed to A for the same-session evidence CAS."""

    # S claim and authenticated node identity are mandatory ownership evidence.
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    claim: NodeClaimV1
    node_identity: NodeIdentityV1
    account_ref: AccountRef
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
    external_identity: ExternalIdentityV1 | None = None
    observed_at: datetime
    error_code: BrowserAccountErrorCode | None = None

    @model_validator(mode="after")
    def validate_trusted_success(self) -> "LoginObservationV1":
        self.target.require_complete()
        if self.contract_version != QRAC2_CONTRACT_VERSION:
            raise ValueError("unsupported observation contract version")
        if self.claim.workspace_id != self.account_ref.workspace_id:
            raise ValueError("observation claim workspace mismatch")
        if self.claim.account_id != self.account_ref.account_id:
            raise ValueError("observation claim account mismatch")
        if self.claim.session_id != self.session_id:
            raise ValueError("observation claim session mismatch")
        if self.claim.epoch != self.epoch:
            raise ValueError("observation claim epoch mismatch")
        if self.node_identity.node_id != self.claim.node_id:
            raise ValueError("observation node mismatch")
        if self.node_identity.boot_id != self.claim.boot_id:
            raise ValueError("observation boot mismatch")
        if not self.claim.claimed_at <= self.observed_at <= self.claim.expires_at:
            raise ValueError("observation is outside claim lifetime")
        if self.claim.expires_at <= self.claim.claimed_at:
            raise ValueError("observation claim deadline is invalid")
        if self.evidence_kind is BrowserAuthEvidence.VALID and (
            self.external_identity is None or self.state not in {"verifying", "saved"}
        ):
            raise ValueError("valid login observation requires identity and verifying state")
        return self
class AccountStateSnapshotV1(_ContractModel):
    """Single trusted source for identity, auth, status, target, and revisions."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    account_ref: AccountRef
    account_revision: int = Field(ge=0)
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    epoch: int = Field(ge=0)
    trusted_identity: ExternalIdentityV1 | None = None
    auth_evidence: BrowserAuthEvidence
    status: BrowserAccountStatus
    target: SessionTargetV1
    rule_id: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=64)
    view_generation: int = Field(ge=0)
    validated_source: Literal["browser_account_session_snapshot"] = (
        "browser_account_session_snapshot"
    )
    validated_at: datetime
    freshness_deadline: datetime

    @model_validator(mode="after")
    def validate_source(self) -> "AccountStateSnapshotV1":
        self.target.require_complete()
        if self.contract_version != QRAC2_CONTRACT_VERSION:
            raise ValueError("unsupported state snapshot contract version")
        if self.freshness_deadline <= self.validated_at:
            raise ValueError("state snapshot freshness deadline must be in the future")
        if (self.freshness_deadline - self.validated_at).total_seconds() > 1:
            raise ValueError("state snapshot freshness window exceeds one second")
        if self.auth_evidence is BrowserAuthEvidence.VALID and self.trusted_identity is None:
            raise ValueError("valid account snapshot requires trusted identity")
        return self


class AccountStateViewV1(_ContractModel):
    """Fresh revision state consumed by the account transition CAS."""
    snapshot: AccountStateSnapshotV1
    # These values must be projected from one validated DB snapshot.
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    trusted_identity: ExternalIdentityV1 | None = None
    auth_evidence: BrowserAuthEvidence
    status: BrowserAccountStatus
    session_epoch: int = Field(ge=0)
    target: SessionTargetV1
    rule_id: str = Field(min_length=1, max_length=128)
    rule_version: str = Field(min_length=1, max_length=64)
    freshness_deadline: datetime
    view_generation: int = Field(ge=0)
    validated_source: Literal["browser_account_session_snapshot"] = (
        "browser_account_session_snapshot"
    )
    validated_at: datetime

    @model_validator(mode="after")
    def validate_trusted_snapshot(self) -> "AccountStateViewV1":
        self.target.require_complete()
        if self.contract_version != QRAC2_CONTRACT_VERSION:
            raise ValueError("unsupported state-view contract version")
        if self.auth_evidence is BrowserAuthEvidence.VALID and self.trusted_identity is None:
            raise ValueError("valid account state requires trusted identity")
        if self.status in {
            BrowserAccountStatus.VERIFYING,
            BrowserAccountStatus.SAVED,
            BrowserAccountStatus.DORMANT,
        } and self.auth_evidence is BrowserAuthEvidence.VALID and self.trusted_identity is None:
            raise ValueError("authenticated status requires trusted identity")
        if self.freshness_deadline <= self.validated_at:
            raise ValueError("state view freshness deadline must be in the future")
        if (self.freshness_deadline - self.validated_at).total_seconds() > 1:
            raise ValueError("state view freshness window exceeds one second")
        if (
            self.snapshot.account_ref != self.account_ref
            or self.snapshot.account_revision != self.account_revision
            or self.snapshot.session_id != self.session_id
            or self.snapshot.session_revision != self.session_revision
            or self.snapshot.epoch != self.session_epoch
            or self.snapshot.trusted_identity != self.trusted_identity
            or self.snapshot.auth_evidence != self.auth_evidence
            or self.snapshot.status != self.status
            or self.snapshot.target != self.target
            or self.snapshot.rule_id != self.rule_id
            or self.snapshot.rule_version != self.rule_version
            or self.snapshot.view_generation != self.view_generation
            or self.snapshot.validated_source != self.validated_source
            or self.snapshot.validated_at != self.validated_at
            or self.snapshot.freshness_deadline != self.freshness_deadline
        ):
            raise ValueError("state view does not match its trusted snapshot")
        return self

    account_ref: AccountRef
    account_revision: int = Field(ge=0)
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)


class AccountStateTransitionV1(_ContractModel):
    """Result of one atomic account/session state transition."""

    accepted: bool
    account_ref: AccountRef
    account_revision: int = Field(ge=0)
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    status: BrowserAccountStatus
    error_code: BrowserAccountErrorCode | None = None


class SensitiveSessionBindingV1(_ContractModel):
    """Binds the account session to a real page and existing record listener."""

    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    target: SessionTargetV1
    view_generation: int = Field(ge=0)
    record_session_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def validate_binding_target(self) -> "SensitiveSessionBindingV1":
        self.target.require_complete()
        return self


class SensitiveGuardStateV1(_ContractModel):
    status: Literal["enabled", "disabled"]
    binding: SensitiveSessionBindingV1
    listener_revoked: bool
    pending_events_drained: bool
    completed_at: datetime



class PortalSensitivePayloadV1(_ContractModel):
    """Short-lived sensitive input; SecretStr prevents repr/log disclosure."""

    value: SecretStr | None = None
    key: str | None = Field(default=None, min_length=1, max_length=32)
    x: int | None = Field(default=None, ge=0, le=4096)
    y: int | None = Field(default=None, ge=0, le=4096)

    @field_validator("value")
    @classmethod
    def bound_value(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) > MAX_PORTAL_INPUT_BYTES:
            raise ValueError("sensitive input exceeds transient limit")
        return value

    @model_validator(mode="after")
    def require_value(self) -> "PortalSensitivePayloadV1":
        if self.value is None and self.key is None and (self.x is None or self.y is None):
            raise ValueError("sensitive payload is empty")
        return self


class PortalClipV1(_ContractModel):
    x: int = Field(ge=0, le=8192)
    y: int = Field(ge=0, le=8192)
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)


class PortalRegionFocusV1(_ContractModel):
    """L-produced allowlist carried unchanged into the R owner boundary."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    target: SessionTargetV1
    view_generation: int = Field(ge=0)
    region_kind: Literal["qr", "form", "approved"]
    approved_regions: list[PortalClipV1] = Field(min_length=1, max_length=32)
    focused_field_ref: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_region_focus(self) -> "PortalRegionFocusV1":
        self.target.require_complete()
        if self.region_kind == "form" and self.focused_field_ref is None:
            raise ValueError("form projection requires a focused field")
        return self


class PortalControlMessageV1(_ContractModel):
    """Transient message with a trusted outer workspace/account binding.

    Authentication, authorization, and lease checks are runtime obligations;
    these fields are not proof merely because schema validation succeeded.
    """

    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    target: SessionTargetV1
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    view_generation: int = Field(ge=0)
    sequence: int = Field(ge=1)
    kind: Literal["field_input", "pointer", "key", "request_view", "takeover"]
    field_ref: str | None = Field(default=None, min_length=1, max_length=128)
    sensitive_payload: PortalSensitivePayloadV1 | None = None

    @model_validator(mode="after")
    def validate_target_boundary(self) -> "PortalControlMessageV1":
        self.target.require_complete()
        if self.kind == "field_input" and self.sensitive_payload is None:
            raise ValueError("field input requires transient payload")
        return self


class PortalPixelFrameV1(_ContractModel):
    """Transient clipped frame; only approved regions may be sent to the client."""
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION

    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    target: SessionTargetV1
    view_generation: int = Field(ge=0)
    sequence: int = Field(ge=1)
    region_kind: Literal["qr", "form", "approved"]
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    expires_at: datetime
    clip: PortalClipV1
    byte_length: int = Field(gt=0, le=MAX_PORTAL_FRAME_BYTES)
    masked_regions: list[PortalClipV1] = Field(default_factory=list, max_length=32)
    frame_bytes: SecretBytes = Field(min_length=1)

    @field_validator("frame_bytes")
    @classmethod
    def bound_frame(cls, value: SecretBytes) -> SecretBytes:
        if len(value.get_secret_value()) > MAX_PORTAL_FRAME_BYTES:
            raise ValueError("transient pixel frame exceeds size limit")
        return value

    @model_validator(mode="after")
    def validate_target_boundary(self) -> "PortalPixelFrameV1":
        self.target.require_complete()
        raw = self.frame_bytes.get_secret_value()
        if len(raw) != self.byte_length:
            raise ValueError("pixel byte length does not match frame bytes")
        if self.mime_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("pixel bytes do not match image/png")
        if self.mime_type == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"):
            raise ValueError("pixel bytes do not match image/jpeg")
        if self.mime_type == "image/webp" and (
            len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP"
        ):
            raise ValueError("pixel bytes do not match image/webp")
        return self


class PortalOuterBindingV1(_ContractModel):
    """Trusted server-side binding supplied outside user-controlled payloads."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION

    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    epoch: int = Field(ge=0)
    target: SessionTargetV1
    view_generation: int = Field(ge=0)


class PortalTransientV1(_ContractModel):
    """In-memory transport envelope; runtime binds it to the authorized session."""
    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION

    binding: PortalOuterBindingV1
    control: PortalControlMessageV1 | None = None
    pixel: PortalPixelFrameV1 | None = None

    @model_validator(mode="after")
    def require_one_message(self) -> "PortalTransientV1":
        if (self.control is None) == (self.pixel is None):
            raise ValueError("portal transient must contain exactly one control or pixel message")
        message = self.control or self.pixel
        assert message is not None
        if (
            message.workspace_id != self.binding.workspace_id
            or message.account_id != self.binding.account_id
            or message.session_id != self.binding.session_id
            or message.epoch != self.binding.epoch
            or message.view_generation != self.binding.view_generation
            or message.target != self.binding.target
        ):
            raise ValueError("portal message does not match trusted outer binding")
        return self


class PortalWireFrameV1(_ContractModel):
    """Explicit C2/A/R websocket frame layout; payload never enters durable storage."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    protocol: Literal["qrac2.portal.v1"] = "qrac2.portal.v1"
    sequence: int = Field(ge=1)
    encoding: Literal["control-json", "pixel-binary"]
    content_type: Literal["application/json", "application/octet-stream"]
    mime_type: Literal["application/json", "image/png", "image/jpeg", "image/webp"]
    byte_length: int | None = Field(default=None, ge=1, le=MAX_PORTAL_FRAME_BYTES)
    transient: PortalTransientV1

    @model_validator(mode="after")
    def validate_encoding(self) -> "PortalWireFrameV1":
        if self.encoding == "control-json":
            if self.content_type != "application/json" or self.mime_type != "application/json":
                raise ValueError("control frames require application/json")
            if self.transient.control is None or self.byte_length is not None:
                raise ValueError("control framing requires a control message and no binary length")
        if self.encoding == "pixel-binary":
            pixel = self.transient.pixel
            if (
                self.content_type != "application/octet-stream"
                or pixel is None
                or self.mime_type != pixel.mime_type
                or self.byte_length != pixel.byte_length
            ):
                raise ValueError("pixel framing must carry the bounded pixel MIME and byte length")
        if self.contract_version != self.transient.contract_version:
            raise ValueError("wire and transient contract versions differ")
        return self


class PortalOwnerRouteV1(_ContractModel):
    """A-owned bounded route passed to R with session-page-record lineage."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    owner: Literal["browser_account_service"] = "browser_account_service"
    binding: SensitiveSessionBindingV1
    node_identity: NodeIdentityV1
    region_focus: PortalRegionFocusV1
    session_revision: int = Field(ge=0)
    route_expires_at: datetime
    max_frame_bytes: int = Field(gt=0, le=MAX_PORTAL_FRAME_BYTES)
    max_input_bytes: int = Field(gt=0, le=MAX_PORTAL_INPUT_BYTES)

    @model_validator(mode="after")
    def validate_owner_boundary(self) -> "PortalOwnerRouteV1":
        if self.binding.record_session_id is None:
            raise ValueError("owner route requires a record session binding")
        if self.binding.target != self.region_focus.target:
            raise ValueError("owner route target does not match L region focus")
        if self.binding.view_generation != self.region_focus.view_generation:
            raise ValueError("owner route generation does not match L region focus")
        self.binding.target.require_complete()
        return self


class PortalAuthorizationFactsV1(_ContractModel):
    """Fresh bounded batch facts used by every active portal replica."""

    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    membership_exists: bool
    role: Literal["admin", "maintainer", "operator", "viewer"] | None = None
    user_disabled: bool
    workspace_active: bool
    # Read from BrowserAccount.revision in the same fresh DB snapshot.
    account_revision: int = Field(ge=0)
    session_revoked: bool
    session_revision: int = Field(ge=0)
    session_expires_at: datetime
    checked_at: datetime
    freshness_deadline: datetime

    @model_validator(mode="after")
    def validate_freshness(self) -> "PortalAuthorizationFactsV1":
        if self.freshness_deadline <= self.checked_at:
            raise ValueError("authorization freshness deadline must be in the future")
        if (self.freshness_deadline - self.checked_at).total_seconds() > 1:
            raise ValueError("authorization freshness deadline exceeds one second")
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
    platform_identity: ExternalIdentityV1 | None
    auth_evidence: BrowserAuthEvidence
    evidence_source: BrowserEvidenceSource | None
    evidence_observed_at: datetime | None
    manual_confirmed_by: str | None
    status: BrowserAccountStatus
    revision: int
    paused: bool
    status_reason_code: str | None
    created_at: datetime
    updated_at: datetime
class BrowserAccountListV1(_ContractModel):
    """Keyset page; no total/offset scan is part of the account contract."""

    items: list[BrowserAccountRead] = Field(default_factory=list, max_length=200)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)


class PortalTicketIssueRequestV1(_ContractModel):
    """First-ticket HTTP request; the ticket is issued only in the body."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    first_entry: Literal["initial"] = "initial"
    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    expected_session_revision: int = Field(ge=0)
    csrf_token: SecretStr = Field(min_length=16, max_length=512)


class PortalTicketIssuedV1(_ContractModel):
    """One-time ticket and CSRF secret returned in the response body."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    status: Literal["issued"] = "issued"
    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    ticket: SecretStr = Field(min_length=16, max_length=512)
    csrf_token: SecretStr = Field(min_length=16, max_length=512)
    issued_at: datetime
    expires_at: datetime
    hard_expires_at: datetime
    http_status: Literal[200] = 200

    @model_validator(mode="after")
    def validate_ticket_window(self) -> "PortalTicketIssuedV1":
        if self.expires_at <= self.issued_at or self.hard_expires_at < self.expires_at:
            raise ValueError("invalid portal ticket lifetime")
        if (self.hard_expires_at - self.issued_at).total_seconds() > 1800:
            raise ValueError("portal ticket hard lifetime exceeds thirty minutes")
        return self


class PortalTicketRecordV1(_ContractModel):
    """Persisted ticket metadata; only digests, never raw ticket or CSRF."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    ticket_id: str = Field(min_length=1, max_length=36)
    ticket_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    csrf_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str = Field(min_length=1, max_length=255)
    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    issued_at: datetime
    expires_at: datetime
    hard_expires_at: datetime
    consumed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_ticket_window(self) -> "PortalTicketRecordV1":
        if self.expires_at <= self.issued_at or self.hard_expires_at < self.expires_at:
            raise ValueError("invalid portal ticket lifetime")
        if (self.hard_expires_at - self.issued_at).total_seconds() > 1800:
            raise ValueError("portal ticket hard lifetime exceeds thirty minutes")
        return self


class PortalTicketRedeemRequestV1(_ContractModel):
    """Body-only one-time exchange; no secret may appear in the URL."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    first_entry: Literal["initial", "reconnect"]
    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    expected_session_revision: int = Field(ge=0)
    ticket_id: str = Field(min_length=1, max_length=36)
    ticket: SecretStr = Field(min_length=16, max_length=512)
    csrf_token: SecretStr = Field(min_length=16, max_length=512)

class PortalTicketGrantV1(_ContractModel):
    """Non-secret routing facts returned after a successful ticket exchange."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    status: Literal["granted"] = "granted"

    http_status: Literal[200] = 200
    workspace_id: str = Field(min_length=1, max_length=36)
    account_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    session_id: str = Field(min_length=1, max_length=36)
    issued_at: datetime
    expires_at: datetime
    hard_expires_at: datetime
    cookie_name: str = Field(min_length=1, max_length=64)
    websocket_path: str = Field(min_length=1, max_length=512)
    # HTTP-only cookie attributes are part of the boundary, not implementation detail.
    cookie_http_only: Literal[True] = True
    cookie_secure: Literal[True] = True
    same_site: Literal["strict", "lax"] = "strict"
    origin_required: Literal[True] = True
    csrf_bound: Literal[True] = True

    @model_validator(mode="after")
    def validate_ticket_window(self) -> "PortalTicketGrantV1":
        if self.expires_at <= self.issued_at or self.hard_expires_at < self.expires_at:
            raise ValueError("invalid portal ticket lifetime")
        if (self.hard_expires_at - self.issued_at).total_seconds() > 1800:
            raise ValueError("portal ticket hard lifetime exceeds thirty minutes")
        return self


class PortalEntryWaitingV1(_ContractModel):
    """Typed non-success response while the fixed account waits for capacity."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    status: Literal["waiting"] = "waiting"
    http_status: Literal[202] = 202
    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    reason: Literal[
        "node_unavailable",
        "capacity_missing",
        "lease_waiting",
        "profile_restore_waiting",
    ]
    retry_at: datetime | None = None


class PortalEntryBlockedV1(_ContractModel):
    """Typed non-success response; it cannot be decoded as a login grant."""

    contract_version: Literal[1] = QRAC2_CONTRACT_VERSION
    status: Literal["blocked"] = "blocked"
    http_status: int = Field(ge=400, le=599)
    account_ref: AccountRef
    session_id: str = Field(min_length=1, max_length=36)
    session_revision: int = Field(ge=0)
    error_code: BrowserAccountErrorCode

    @model_validator(mode="after")
    def validate_http_status(self) -> "PortalEntryBlockedV1":
        if self.http_status != ERROR_HTTP_STATUS[self.error_code]:
            raise ValueError("portal error status does not match error code")
        return self



PortalEntryResponseV1 = PortalTicketGrantV1 | PortalEntryWaitingV1 | PortalEntryBlockedV1



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
    paused: bool | None = None
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
    # Authoritative portal CAS revision from browser_login_sessions.revision.
    revision: int = Field(ge=0)
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
    payload: dict[str, object]
    result: NodeEvidenceV1 | None
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
