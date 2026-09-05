import hashlib
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TimestampMixin


class BrowserBinding(TimestampMixin):
    """Maps an opencli site to a specific Chrome CDP endpoint."""

    __tablename__ = "browser_bindings"
    __table_args__ = (UniqueConstraint("site", name="uq_browser_bindings_site"),)

    browser_endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    site: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class BrowserRuntimeBundle(TimestampMixin):
    """Immutable, versioned manifest selecting browser runtime capabilities."""

    __tablename__ = "browser_runtime_bundles"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_browser_runtime_bundle_version"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    trust_level: Mapped[str] = mapped_column(String(30), nullable=False, default="trusted")
    source: Mapped[str] = mapped_column(String(255), nullable=False, default="local")


class BrowserInstance(TimestampMixin):
    """Persistent desired configuration for a single browser runtime slot."""

    __tablename__ = "browser_instances"
    __table_args__ = (UniqueConstraint("profile_name", name="uq_browser_instances_profile_name"),)

    endpoint: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # "bridge" → opencli daemon+extension; "cdp" → direct CDP.
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="bridge")
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    agent_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_protocol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Profile contains only login and site state. Runtime components are never
    # derived from this writable volume.
    profile_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="authenticated")
    profile_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    runtime_bundle_id: Mapped[str | None] = mapped_column(
        ForeignKey("browser_runtime_bundles.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    resource_class: Mapped[str] = mapped_column(String(100), nullable=False, default="standard")
    startup_pages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    network_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


@event.listens_for(BrowserInstance, "before_insert")
def _default_profile_name(_mapper, _connection, target: BrowserInstance) -> None:
    if not target.profile_name:
        if len(target.endpoint) <= 100:
            target.profile_name = target.endpoint
        else:
            target.profile_name = f"endpoint-{hashlib.sha256(target.endpoint.encode()).hexdigest()[:64]}"


class BrowserRuntimeDeployment(TimestampMixin):
    """Loaded runtime fact reported by a slot; never a desired-state projection."""

    __tablename__ = "browser_runtime_deployments"
    __table_args__ = (
        UniqueConstraint("browser_instance_id", name="uq_browser_runtime_deployment_slot"),
    )

    browser_instance_id: Mapped[str] = mapped_column(
        ForeignKey("browser_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    loaded_bundle_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    loaded_bundle_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    loaded_components: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    self_check: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="DEGRADED")
    diagnostics: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class BrowserCapabilityInvocation(TimestampMixin):
    """Auditable structured capability call with the complete runtime lineage."""

    __tablename__ = "browser_capability_invocations"

    browser_instance_id: Mapped[str] = mapped_column(
        ForeignKey("browser_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    capability: Mapped[str] = mapped_column(String(255), nullable=False)
    desired_bundle_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    desired_bundle_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    loaded_bundle_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    component_versions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    page_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    page_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    risk: Mapped[str] = mapped_column(String(20), nullable=False)
    gate: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)

class BrowserAccountStatus(StrEnum):
    """Authoritative lifecycle for a workspace browser account."""

    OPENING = "opening"
    PRESENTING = "presenting"
    REFRESHING = "refreshing"
    VERIFYING = "verifying"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"
    SAVING = "saving"
    SAVED = "saved"
    DORMANT = "dormant"
    EXPIRED = "expired"
    CLOSED = "closed"
    ERROR = "error"


class BrowserAuthEvidence(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    INVALID = "invalid"


class BrowserEvidenceSource(StrEnum):
    RULE_VERIFIED = "rule_verified"
    MANUAL_FALLBACK = "manual_fallback"


class BrowserLeaseStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    QUARANTINED = "quarantined"


class BrowserCommandKind(StrEnum):
    START_LOGIN = "start_login"
    APPLY_LOGIN_RULE = "apply_login_rule"
    REFRESH_LOGIN = "refresh_login"
    STOP_AND_SAVE = "stop_and_save"
    EXECUTE_REFERENCE = "execute_reference"
    CLOSE_SESSION = "close_session"
    ISOLATE = "isolate"
    MIGRATE = "migrate"


class BrowserCommandStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ProfileInventoryStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_PRESENT = "not_present"
    PRESENT = "present"
    BLOCKED = "blocked"
    VERIFIED = "verified"


class BrowserAccount(TimestampMixin):
    """Workspace-owned account identity and committed profile pointer."""

    __tablename__ = "browser_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_browser_accounts_workspace_id"),
        UniqueConstraint("profile_id", name="uq_browser_accounts_profile_id"),
        CheckConstraint(
            "auth_evidence IN ('unknown', 'valid', 'invalid')",
            name="ck_browser_accounts_auth_evidence",
        ),
        CheckConstraint(
            "evidence_source IN ('rule_verified', 'manual_fallback')",
            name="ck_browser_accounts_evidence_source",
        ),
        CheckConstraint(
            "status IN ('opening', 'presenting', 'refreshing', 'verifying', 'challenge', "
            "'unknown', 'saving', 'saved', 'dormant', 'expired', 'closed', 'error')",
            name="ck_browser_accounts_status",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_manifest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runtime_bundle_id: Mapped[str | None] = mapped_column(
        ForeignKey("browser_runtime_bundles.id", ondelete="RESTRICT"), nullable=True
    )
    runtime_bundle_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    login_rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    login_rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    platform_identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auth_evidence: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserAuthEvidence.UNKNOWN.value
    )
    evidence_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    evidence_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    manual_confirmed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserAccountStatus.DORMANT.value
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BrowserLoginSession(TimestampMixin):
    """Ephemeral login or execution session bound to one account generation."""

    __tablename__ = "browser_login_sessions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_browser_login_sessions_workspace_id"),
        CheckConstraint(
            "purpose IN ('login', 'execution')",
            name="ck_browser_login_sessions_purpose",
        ),
        CheckConstraint(
            "status IN ('opening', 'presenting', 'refreshing', 'verifying', 'challenge', "
            "'unknown', 'saving', 'saved', 'expired', 'closed', 'error')",
            name="ck_browser_login_sessions_status",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["browser_accounts.workspace_id", "browser_accounts.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    instance_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="RESTRICT"), nullable=True
    )
    node_boot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_state: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    login_rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    login_rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tab_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    frame_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    origin: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    view_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="login")
    execution_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    command_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserAccountStatus.OPENING.value
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BrowserAccountLease(TimestampMixin):
    """Persistent account lease used for fencing, never a process-local lock."""

    __tablename__ = "browser_account_leases"
    __table_args__ = (
        UniqueConstraint("lease_id", name="uq_browser_account_leases_lease_id"),
        CheckConstraint(
            "status IN ('active', 'expired', 'released', 'quarantined')",
            name="ck_browser_account_leases_status",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["browser_accounts.workspace_id", "browser_accounts.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    node_boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_id: Mapped[str] = mapped_column(String(36), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserLeaseStatus.ACTIVE.value
    )
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    isolation_evidence_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BrowserDurableCommand(TimestampMixin):
    """Structured persistent command; it cannot carry shell or credential input."""

    __tablename__ = "browser_durable_commands"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_scope",
            "idempotency_key",
            name="uq_browser_commands_idempotency",
        ),
        CheckConstraint(
            "kind IN ('start_login', 'apply_login_rule', 'refresh_login', 'stop_and_save', "
            "'execute_reference', 'close_session', 'isolate', 'migrate')",
            name="ck_browser_commands_kind",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'running', 'succeeded', 'failed', 'expired', 'cancelled')",
            name="ck_browser_commands_status",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["browser_accounts.workspace_id", "browser_accounts.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    binding_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserCommandStatus.QUEUED.value
    )
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BrowserProfileManifest(TimestampMixin):
    """Metadata/reference for a complete, immutable stopped-browser snapshot."""

    __tablename__ = "browser_profile_manifests"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_browser_profile_manifest_version"),
        CheckConstraint(
            "password_inventory_status IN ('unknown', 'not_present', 'present', 'blocked', 'verified')",
            name="ck_browser_profile_manifests_password_inventory",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["browser_accounts.workspace_id", "browser_accounts.id"],
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="RESTRICT"), nullable=False
    )
    command_id: Mapped[str] = mapped_column(
        ForeignKey("browser_durable_commands.id", ondelete="RESTRICT"), nullable=False
    )
    writer_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_name: Mapped[str] = mapped_column(String(100), nullable=False)
    browser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    files_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_manifest_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    complete_marker: Mapped[str] = mapped_column(String(255), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    password_inventory_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProfileInventoryStatus.UNKNOWN.value
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="committed")

Index(
    "ix_browser_accounts_workspace_status_id",
    BrowserAccount.workspace_id,
    BrowserAccount.status,
    BrowserAccount.id,
)
Index(
    "ix_browser_durable_commands_node_status_available_id",
    BrowserDurableCommand.node_id,
    BrowserDurableCommand.status,
    BrowserDurableCommand.available_at,
    BrowserDurableCommand.id,
)
Index(
    "uq_browser_account_leases_active_account",
    BrowserAccountLease.workspace_id,
    BrowserAccountLease.account_id,
    unique=True,
    sqlite_where=BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
    postgresql_where=BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
)
