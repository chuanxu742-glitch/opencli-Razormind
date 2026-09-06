"""Workspace-scoped browser account lifecycle and session fencing.

This module owns the mutable account/session facts behind the frozen QRAC2
contracts.  Runtime execution and node scheduling remain consumers: this
service never chooses an endpoint, accepts shell input, or treats a browser
cookie as proof of authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserAccountStatus,
    BrowserAuthEvidence,
    BrowserCommandKind,
    BrowserDurableCommand,
    BrowserEvidenceSource,
    BrowserLeaseStatus,
    BrowserLoginSession,
    BrowserProfileManifest,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership
from backend.models.source_binding import SourceBindingRevision
from backend.schemas.browser_account import (
    AccountRef,
    BrowserAccountCreate,
    BrowserAccountErrorCode,
    BrowserAccountUpdate,
    BrowserAuthEvidence as _SchemaBrowserAuthEvidence,
    CloseSessionCommandPayloadV1,
    EmptyCommandPayloadV1,
    ExecuteReferenceCommandPayloadV1,
    ExternalIdentityV1,
    LoginObservationV1,
    MigrateCommandPayloadV1,
    NodeResultV1,
    PortalAuthorizationFactsV1,
    RefreshLoginCommandPayloadV1,
    SessionEnvelopeV1,
    SessionTargetV1,
    StopAndSaveCommandPayloadV1,
)


_ACTIVE_SESSION_STATUSES = (
    BrowserAccountStatus.OPENING.value,
    BrowserAccountStatus.PRESENTING.value,
    BrowserAccountStatus.REFRESHING.value,
    BrowserAccountStatus.VERIFYING.value,
    BrowserAccountStatus.CHALLENGE.value,
    BrowserAccountStatus.UNKNOWN.value,
    BrowserAccountStatus.SAVING.value,
)
_TERMINAL_SESSION_STATUSES = (
    BrowserAccountStatus.EXPIRED.value,
    BrowserAccountStatus.CLOSED.value,
)
_COMMAND_TTL = timedelta(minutes=30)


class BrowserAccountError(RuntimeError):
    """Stable router-neutral error for account and session operations."""

    def __init__(self, code: str | BrowserAccountErrorCode, message: str, status_code: int | None = None):
        self.code = code.value if isinstance(code, BrowserAccountErrorCode) else str(code)
        self.status_code = status_code or _status_for_code(self.code)
        super().__init__(message)


def _status_for_code(code: str) -> int:
    if code == BrowserAccountErrorCode.PERMISSION_DENIED.value:
        return 403
    if code == BrowserAccountErrorCode.NODE_UNAVAILABLE.value:
        return 503
    if code == BrowserAccountErrorCode.SESSION_EXPIRED.value:
        return 410
    if code == "not_found":
        return 404
    if code.startswith("invalid_"):
        return 422
    return 409


@dataclass(frozen=True)
class AccountSessionResolution:
    """C1 result: a ready envelope or an explicit waiting/blocked outcome."""

    status: Literal["ready", "waiting", "blocked"]
    session: SessionEnvelopeV1 | None = None
    error_code: BrowserAccountErrorCode | None = None
    reason: str | None = None

    @property
    def waiting(self) -> bool:
        return self.status == "waiting"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes (SQLite may return timezone-naive values)."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _identity_dump(value: ExternalIdentityV1 | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = value if isinstance(value, ExternalIdentityV1) else ExternalIdentityV1.from_wire(value)
    return parsed.to_wire()


async def _workspace_or_error(db: AsyncSession, workspace_id: str) -> Workspace:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None or not workspace.active:
        raise BrowserAccountError("not_found", "workspace not found", 404)
    return workspace


async def _account_or_error(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    *,
    for_update: bool = False,
) -> BrowserAccount:
    statement = select(BrowserAccount).where(
        BrowserAccount.workspace_id == workspace_id,
        BrowserAccount.id == account_id,
    )
    if for_update:
        statement = statement.with_for_update()
    account = await db.scalar(statement)
    if account is None:
        raise BrowserAccountError("not_found", "browser account not found", 404)
    return account


async def _session_or_error(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    for_update: bool = False,
) -> BrowserLoginSession:
    statement = select(BrowserLoginSession).where(
        BrowserLoginSession.workspace_id == workspace_id,
        BrowserLoginSession.account_id == account_id,
        BrowserLoginSession.id == session_id,
    )
    if for_update:
        statement = statement.with_for_update()
    session = await db.scalar(statement)
    if session is None:
        raise BrowserAccountError("not_found", "login session not found", 404)
    return session


async def _validate_node(db: AsyncSession, node_id: str) -> EdgeNode:
    node = await db.get(EdgeNode, node_id)
    if node is None:
        raise BrowserAccountError(BrowserAccountErrorCode.NODE_UNAVAILABLE, "node not found")
    if node.quarantined or not node.account_capable:
        raise BrowserAccountError(
            BrowserAccountErrorCode.CAPABILITY_MISSING,
            "node is not registered for browser-account work",
        )
    return node


async def _runtime_bundle_or_error(db: AsyncSession, bundle_id: str) -> BrowserRuntimeBundle:
    bundle = await db.get(BrowserRuntimeBundle, bundle_id)
    if bundle is None:
        raise BrowserAccountError("not_found", "runtime bundle not found", 404)
    return bundle


async def create_browser_account(
    db: AsyncSession,
    workspace_id: str,
    body: BrowserAccountCreate | Mapping[str, Any],
) -> BrowserAccount:
    """Create only durable metadata; credentials and portal input never enter DB."""

    payload = body if isinstance(body, BrowserAccountCreate) else BrowserAccountCreate.model_validate(body)
    if payload.workspace_id != workspace_id:
        raise BrowserAccountError(
            BrowserAccountErrorCode.PERMISSION_DENIED,
            "account workspace does not match request workspace",
        )
    await _workspace_or_error(db, workspace_id)
    if bool(payload.login_rule_id) != bool(payload.login_rule_version):
        raise BrowserAccountError("invalid_login_rule", "login_rule_id and version must be supplied together", 422)
    node = await _validate_node(db, payload.node_id) if payload.node_id else None
    bundle = await _runtime_bundle_or_error(db, payload.runtime_bundle_id) if payload.runtime_bundle_id else None
    account = BrowserAccount(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        site=payload.site,
        label=payload.label,
        node_id=node.id if node else None,
        runtime_bundle_id=bundle.id if bundle else None,
        runtime_bundle_version=bundle.version if bundle else None,
        login_rule_id=payload.login_rule_id,
        login_rule_version=payload.login_rule_version,
        auth_required=False,
        auth_evidence=BrowserAuthEvidence.UNKNOWN.value,
        evidence_source=None,
        status=BrowserAccountStatus.DORMANT.value,
        paused=False,
        revision=0,
    )
    db.add(account)
    await db.flush()
    await db.refresh(account)
    return account


async def list_browser_accounts(
    db: AsyncSession,
    workspace_id: str,
    *,
    limit: int = 50,
    after_id: str | None = None,
    status: BrowserAccountStatus | None = None,
) -> tuple[list[BrowserAccount], str | None]:
    await _workspace_or_error(db, workspace_id)
    if not 1 <= limit <= 200:
        raise BrowserAccountError("invalid_limit", "limit must be between 1 and 200", 422)
    statement = select(BrowserAccount).where(BrowserAccount.workspace_id == workspace_id)
    if status is not None:
        statement = statement.where(BrowserAccount.status == status.value)
    if after_id:
        statement = statement.where(BrowserAccount.id > after_id)
    rows = list(
        (
            await db.execute(
                statement.order_by(BrowserAccount.id.asc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    next_cursor = rows.pop().id if len(rows) > limit else None
    return rows, next_cursor


async def get_browser_account(db: AsyncSession, workspace_id: str, account_id: str) -> BrowserAccount:
    await _workspace_or_error(db, workspace_id)
    return await _account_or_error(db, workspace_id, account_id)


async def _active_lease(db: AsyncSession, workspace_id: str, account_id: str) -> BrowserAccountLease | None:
    now = _now()
    return await db.scalar(
        select(BrowserAccountLease)
        .where(
            BrowserAccountLease.workspace_id == workspace_id,
            BrowserAccountLease.account_id == account_id,
            BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
            BrowserAccountLease.expires_at > now,
        )
        .order_by(BrowserAccountLease.epoch.desc())
    )


async def _enqueue_command(
    db: AsyncSession,
    account: BrowserAccount,
    kind: BrowserCommandKind,
    *,
    idempotency_scope: str,
    idempotency_key: str,
    session_id: str | None = None,
    execution_id: str | None = None,
    payload: Any | None = None,
    binding_revision_id: str | None = None,
    expires_at: datetime | None = None,
) -> BrowserDurableCommand:
    existing = await db.scalar(
        select(BrowserDurableCommand).where(
            BrowserDurableCommand.workspace_id == account.workspace_id,
            BrowserDurableCommand.idempotency_scope == idempotency_scope,
            BrowserDurableCommand.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing
    lease = await _active_lease(db, account.workspace_id, account.id)
    now = _now()
    command = BrowserDurableCommand(
        id=str(uuid.uuid4()),
        workspace_id=account.workspace_id,
        account_id=account.id,
        node_id=lease.node_id if lease else account.node_id,
        kind=kind.value,
        idempotency_scope=idempotency_scope,
        idempotency_key=idempotency_key,
        execution_id=execution_id,
        binding_revision_id=binding_revision_id,
        epoch=lease.epoch if lease else 0,
        expected_revision=account.revision,
        available_at=now,
        expires_at=expires_at or now + _COMMAND_TTL,
        status="queued",
        session_id=session_id,
        payload=(payload.to_wire() if hasattr(payload, "to_wire") else dict(payload or {})),
    )
    try:
        async with db.begin_nested():
            db.add(command)
            await db.flush()
    except IntegrityError as exc:
        # Another request won the idempotency race.  Keep the caller on the
        # canonical command rather than returning a second durable command.
        existing = await db.scalar(
            select(BrowserDurableCommand).where(
                BrowserDurableCommand.workspace_id == account.workspace_id,
                BrowserDurableCommand.idempotency_scope == idempotency_scope,
                BrowserDurableCommand.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing
        raise BrowserAccountError("command_conflict", "durable command could not be created") from exc
    return command


def _set_revision(account: BrowserAccount) -> None:
    account.revision += 1


async def update_browser_account(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    body: BrowserAccountUpdate,
    *,
    confirmed_by: str | None = None,
) -> BrowserAccount:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    if body.expected_revision != account.revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "account revision is stale")
    changed = False
    if body.auth_required is not None and body.auth_required != account.auth_required:
        account.auth_required = body.auth_required
        changed = True
        if body.auth_required and account.status in _ACTIVE_SESSION_STATUSES:
            account.status = BrowserAccountStatus.UNKNOWN.value
            account.status_reason_code = BrowserAccountErrorCode.AUTH_REQUIRED.value
    if body.paused is not None and body.paused != account.paused:
        account.paused = body.paused
        changed = True
        if body.paused:
            account.status = BrowserAccountStatus.DORMANT.value
            account.status_reason_code = "suspended"
        elif account.status == BrowserAccountStatus.DORMANT.value:
            account.status_reason_code = None
    if body.status is not None and body.status.value != account.status:
        target = body.status.value
        if target in _TERMINAL_SESSION_STATUSES:
            raise BrowserAccountError("invalid_transition", "account closure is controlled by session close")
        if target == BrowserAccountStatus.SAVED.value and not (
            account.auth_evidence == BrowserAuthEvidence.VALID.value
            and account.profile_id
            and account.profile_version
            and account.profile_manifest_id
            and not account.auth_required
            and not account.paused
        ):
            raise BrowserAccountError("invalid_transition", "saved requires verified identity and a committed profile")
        if target not in {
            BrowserAccountStatus.DORMANT.value,
            BrowserAccountStatus.UNKNOWN.value,
            BrowserAccountStatus.CHALLENGE.value,
            BrowserAccountStatus.ERROR.value,
            BrowserAccountStatus.SAVING.value,
            BrowserAccountStatus.SAVED.value,
        }:
            raise BrowserAccountError("invalid_transition", "status is controlled by the account workflow")
        account.status = target
        changed = True
    if body.status_reason_code is not None:
        reason = body.status_reason_code.value
        if reason != account.status_reason_code:
            account.status_reason_code = reason
            changed = True
    if changed:
        _set_revision(account)
    if confirmed_by and changed:
        account.manual_confirmed_by = confirmed_by
    await db.flush()
    await db.refresh(account)
    return account


async def suspend_browser_account(db: AsyncSession, workspace_id: str, account_id: str, expected_revision: int) -> BrowserAccount:
    return await update_browser_account(
        db,
        workspace_id,
        account_id,
        BrowserAccountUpdate(paused=True, expected_revision=expected_revision),
    )


async def resume_browser_account(db: AsyncSession, workspace_id: str, account_id: str, expected_revision: int) -> BrowserAccount:
    return await update_browser_account(
        db,
        workspace_id,
        account_id,
        BrowserAccountUpdate(paused=False, expected_revision=expected_revision),
    )


async def mark_auth_required(db: AsyncSession, workspace_id: str, account_id: str, expected_revision: int) -> BrowserAccount:
    return await update_browser_account(
        db,
        workspace_id,
        account_id,
        BrowserAccountUpdate(
            auth_required=True,
            status=BrowserAccountStatus.UNKNOWN,
            status_reason_code=BrowserAccountErrorCode.AUTH_REQUIRED,
            expected_revision=expected_revision,
        ),
    )


async def queue_account_migration(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    *,
    target_node_id: str,
    snapshot_ref: str,
    expected_revision: int,
) -> BrowserAccount:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    if expected_revision != account.revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "account revision is stale")
    await _validate_node(db, target_node_id)
    active_session = await db.scalar(
        select(BrowserLoginSession.id).where(
            BrowserLoginSession.workspace_id == workspace_id,
            BrowserLoginSession.account_id == account_id,
            BrowserLoginSession.status.in_(_ACTIVE_SESSION_STATUSES),
        )
    )
    if active_session is not None:
        raise BrowserAccountError(BrowserAccountErrorCode.ISOLATION_REQUIRED, "stop the existing session before migration")
    await _enqueue_command(
        db,
        account,
        BrowserCommandKind.MIGRATE,
        idempotency_scope=f"account:{account.id}:migration",
        idempotency_key=f"{expected_revision}:{target_node_id}:{snapshot_ref}",
        payload=MigrateCommandPayloadV1(target_node_id=target_node_id, snapshot_ref=snapshot_ref),
    )
    account.status = BrowserAccountStatus.DORMANT.value
    account.status_reason_code = "migration_pending"
    _set_revision(account)
    await db.flush()
    await db.refresh(account)
    return account


async def create_login_session(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    *,
    purpose: Literal["login", "execution"] = "login",
    execution_id: str | None = None,
    expires_in_seconds: int = 1_800,
    takeover: bool = False,
) -> BrowserLoginSession:
    if not 60 <= expires_in_seconds <= 1_800:
        raise BrowserAccountError("invalid_expiry", "session expiry must be between 60 and 1800 seconds", 422)
    if purpose == "execution" and not execution_id:
        raise BrowserAccountError("invalid_request", "execution sessions require execution_id", 422)
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    if account.status in _TERMINAL_SESSION_STATUSES:
        raise BrowserAccountError(BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED, "account is not available")
    if account.paused:
        raise BrowserAccountError(BrowserAccountErrorCode.PERMISSION_DENIED, "account is suspended")
    active = await db.scalar(
        select(BrowserLoginSession)
        .where(
            BrowserLoginSession.workspace_id == workspace_id,
            BrowserLoginSession.account_id == account_id,
            BrowserLoginSession.status.in_(_ACTIVE_SESSION_STATUSES),
        )
        .order_by(BrowserLoginSession.updated_at.desc(), BrowserLoginSession.id.desc())
    )
    if active is not None and not takeover:
        return active
    if active is not None:
        active.status = BrowserAccountStatus.CLOSED.value
        active.closed_at = _now()
    node = await db.get(EdgeNode, account.node_id) if account.node_id else None
    session = BrowserLoginSession(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        account_id=account_id,
        instance_id=None,
        node_id=account.node_id,
        node_boot_id=node.boot_id if node else None,
        epoch=0,
        profile_id=account.profile_id,
        profile_version=account.profile_version,
        profile_state="committed" if account.profile_id and account.profile_version else "new",
        login_rule_id=account.login_rule_id,
        login_rule_version=account.login_rule_version,
        view_generation=0,
        purpose=purpose,
        execution_id=execution_id,
        status=BrowserAccountStatus.OPENING.value,
        expires_at=_now() + timedelta(seconds=expires_in_seconds),
    )
    db.add(session)
    account.status = BrowserAccountStatus.OPENING.value
    _set_revision(account)
    await db.flush()
    command = await _enqueue_command(
        db,
        account,
        BrowserCommandKind.START_LOGIN if purpose == "login" else BrowserCommandKind.EXECUTE_REFERENCE,
        idempotency_scope=f"session:{session.id}",
        idempotency_key="start",
        session_id=session.id,
        execution_id=execution_id,
        payload=(
            EmptyCommandPayloadV1()
            if purpose == "login"
            else ExecuteReferenceCommandPayloadV1(execution_id=execution_id or "")
        ),
    )
    session.command_id = command.id
    await db.flush()
    await db.refresh(session)
    return session


async def list_login_sessions(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    *,
    limit: int = 50,
) -> list[BrowserLoginSession]:
    await _account_or_error(db, workspace_id, account_id)
    if not 1 <= limit <= 200:
        raise BrowserAccountError("invalid_limit", "limit must be between 1 and 200", 422)
    return list(
        (
            await db.execute(
                select(BrowserLoginSession)
                .where(
                    BrowserLoginSession.workspace_id == workspace_id,
                    BrowserLoginSession.account_id == account_id,
                )
                .order_by(BrowserLoginSession.created_at.desc(), BrowserLoginSession.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def get_login_session(db: AsyncSession, workspace_id: str, account_id: str, session_id: str) -> BrowserLoginSession:
    await _account_or_error(db, workspace_id, account_id)
    return await _session_or_error(db, workspace_id, account_id, session_id)


async def takeover_login_session(db: AsyncSession, workspace_id: str, account_id: str, session_id: str) -> BrowserLoginSession:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, session_id, for_update=True)
    if session.status in _TERMINAL_SESSION_STATUSES:
        raise BrowserAccountError(BrowserAccountErrorCode.SESSION_EXPIRED, "login session is closed")
    session.status = BrowserAccountStatus.PRESENTING.value
    account.status = BrowserAccountStatus.PRESENTING.value
    _set_revision(account)
    await db.flush()
    return session


async def refresh_login_session(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    expected_view_generation: int | None = None,
) -> BrowserLoginSession:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, session_id, for_update=True)
    if session.status in _TERMINAL_SESSION_STATUSES or (
        session.expires_at is not None and _as_utc(session.expires_at) <= _now()
    ):
        session.status = BrowserAccountStatus.EXPIRED.value
        raise BrowserAccountError(BrowserAccountErrorCode.SESSION_EXPIRED, "login session is expired", 410)
    if expected_view_generation is not None and expected_view_generation != session.view_generation:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "session view generation is stale")
    session.view_generation += 1
    session.status = BrowserAccountStatus.REFRESHING.value
    account.status = BrowserAccountStatus.REFRESHING.value
    _set_revision(account)
    await db.flush()
    command = await _enqueue_command(
        db,
        account,
        BrowserCommandKind.REFRESH_LOGIN,
        idempotency_scope=f"session:{session.id}:refresh",
        idempotency_key=str(session.view_generation),
        session_id=session.id,
        payload=RefreshLoginCommandPayloadV1(
            trigger="manual",
            expected_view_generation=session.view_generation,
        ),
    )
    session.command_id = command.id
    await db.flush()
    return session


async def confirm_login_session(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    confirmed_by: str,
    expected_revision: int | None = None,
    expected_view_generation: int | None = None,
    platform_identity: ExternalIdentityV1 | Mapping[str, Any] | None = None,
) -> BrowserLoginSession:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, session_id, for_update=True)
    if expected_revision is not None and expected_revision != account.revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "account revision is stale")
    if expected_view_generation is not None and expected_view_generation != session.view_generation:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "session view generation is stale")
    if session.status not in {
        BrowserAccountStatus.CHALLENGE.value,
        BrowserAccountStatus.UNKNOWN.value,
    }:
        raise BrowserAccountError("invalid_transition", "manual confirmation is only valid for an active login challenge")
    account.auth_required = False
    account.auth_evidence = BrowserAuthEvidence.VALID.value
    account.evidence_source = BrowserEvidenceSource.MANUAL_FALLBACK.value
    account.evidence_observed_at = _now()
    account.manual_confirmed_by = confirmed_by
    identity = _identity_dump(platform_identity)
    if identity is not None:
        account.platform_identity = identity
    account.status = BrowserAccountStatus.SAVING.value
    session.status = BrowserAccountStatus.SAVING.value
    session.profile_state = "uncommitted"
    _set_revision(account)
    await db.flush()
    command = await _enqueue_command(
        db,
        account,
        BrowserCommandKind.STOP_AND_SAVE,
        idempotency_scope=f"session:{session.id}:save",
        idempotency_key=str(account.revision),
        session_id=session.id,
        payload=StopAndSaveCommandPayloadV1(
            expected_profile_version=account.profile_version,
        ),
    )
    session.command_id = command.id
    await db.flush()
    return session


async def close_login_session(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    reason: Literal["completed", "cancelled", "expired", "error"] = "cancelled",
) -> BrowserLoginSession:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, session_id, for_update=True)
    if session.status in _TERMINAL_SESSION_STATUSES:
        return session
    session.status = BrowserAccountStatus.EXPIRED.value if reason == "expired" else BrowserAccountStatus.CLOSED.value
    session.closed_at = _now()
    command = await _enqueue_command(
        db,
        account,
        BrowserCommandKind.CLOSE_SESSION,
        idempotency_scope=f"session:{session.id}:close",
        idempotency_key=reason,
        session_id=session.id,
        payload=CloseSessionCommandPayloadV1(reason=reason),
    )
    session.command_id = command.id
    active_other = await db.scalar(
        select(BrowserLoginSession.id).where(
            BrowserLoginSession.workspace_id == workspace_id,
            BrowserLoginSession.account_id == account_id,
            BrowserLoginSession.id != session.id,
            BrowserLoginSession.status.in_(_ACTIVE_SESSION_STATUSES),
        )
    )
    if active_other is None and account.status != BrowserAccountStatus.SAVED.value:
        account.status = BrowserAccountStatus.DORMANT.value
    _set_revision(account)
    await db.flush()
    return session


async def _session_envelope(account: BrowserAccount, session: BrowserLoginSession) -> SessionEnvelopeV1:
    if not session.node_id or not session.node_boot_id or not session.lease_id:
        raise BrowserAccountError("waiting", "session is waiting for a fenced node lease")
    if not session.command_id:
        raise BrowserAccountError("waiting", "session is waiting for a durable command")
    if not account.runtime_bundle_id or not account.runtime_bundle_version:
        raise BrowserAccountError(BrowserAccountErrorCode.CAPABILITY_MISSING, "account runtime bundle is not selected")
    if session.purpose == "execution" and session.profile_state != "committed":
        raise BrowserAccountError("waiting", "execution session is waiting for a committed profile")
    if session.profile_state == "committed" and not (session.profile_id and session.profile_version):
        raise BrowserAccountError(BrowserAccountErrorCode.PROFILE_CORRUPT, "session profile manifest is incomplete")
    target = SessionTargetV1(
        tab_id=session.tab_id,
        frame_id=session.frame_id,
        document_id=session.document_id,
        origin=session.origin,
    )
    return SessionEnvelopeV1(
        workspace_id=account.workspace_id,
        account_id=account.id,
        session_id=session.id,
        profile_id=session.profile_id,
        profile_version=session.profile_version,
        node_id=session.node_id,
        node_boot_id=session.node_boot_id,
        lease_id=session.lease_id,
        epoch=session.epoch,
        lease_expires_at=(await _lease_expiry_for_session(account, session)),
        runtime_bundle_id=account.runtime_bundle_id,
        runtime_bundle_version=account.runtime_bundle_version,
        login_rule_id=session.login_rule_id,
        login_rule_version=session.login_rule_version,
        target=target,
        view_generation=session.view_generation,
        purpose=session.purpose,
        execution_id=session.execution_id,
        command_id=session.command_id,
        profile_state=session.profile_state,
        profile_manifest_id=account.profile_manifest_id,
    )


async def _lease_expiry_for_session(account: BrowserAccount, session: BrowserLoginSession) -> datetime:
    # Session envelopes are only constructed after the caller has verified a
    # lease.  Keep this helper asynchronous for a stable call seam; the lease
    # deadline is attached by resolve_account_session below.
    return _now() + _COMMAND_TTL


async def resolve_account_session(
    db: AsyncSession,
    account_ref: AccountRef | Mapping[str, Any],
    execution_context: Any,
) -> AccountSessionResolution:
    """Resolve one existing execution session without acquiring another slot."""

    ref = account_ref if isinstance(account_ref, AccountRef) else AccountRef.from_wire(account_ref)
    context = execution_context if isinstance(execution_context, Mapping) else getattr(execution_context, "__dict__", {})
    if not isinstance(context, Mapping):
        context = {}
    forbidden = {"endpoint", "browser_endpoint", "shell", "command", "input", "client_endpoint"}
    if forbidden.intersection(context):
        raise BrowserAccountError(BrowserAccountErrorCode.PERMISSION_DENIED, "execution context cannot select an endpoint or carry input")
    account = await _account_or_error(db, ref.workspace_id, ref.account_id)
    if ref.source_binding_revision_id is not None:
        binding_revision = await db.scalar(
            select(SourceBindingRevision).where(
                SourceBindingRevision.id == ref.source_binding_revision_id,
                SourceBindingRevision.account_id == account.id,
            )
        )
        if binding_revision is None:
            return AccountSessionResolution(
                "blocked",
                error_code=BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED,
                reason="source binding revision is not pinned to this account",
            )
    if account.paused or account.auth_required:
        return AccountSessionResolution(
            "blocked",
            error_code=BrowserAccountErrorCode.AUTH_REQUIRED if account.auth_required else BrowserAccountErrorCode.PERMISSION_DENIED,
            reason="account requires authorization" if account.auth_required else "account is suspended",
        )
    if account.status in {BrowserAccountStatus.ERROR.value, BrowserAccountStatus.CLOSED.value, BrowserAccountStatus.EXPIRED.value}:
        return AccountSessionResolution("blocked", error_code=BrowserAccountErrorCode.SESSION_EXPIRED, reason="account is not runnable")
    execution_id = context.get("execution_id")
    statement = select(BrowserLoginSession).where(
        BrowserLoginSession.workspace_id == ref.workspace_id,
        BrowserLoginSession.account_id == ref.account_id,
        BrowserLoginSession.purpose == "execution",
        BrowserLoginSession.status.in_(_ACTIVE_SESSION_STATUSES),
    )
    if execution_id:
        statement = statement.where(BrowserLoginSession.execution_id == execution_id)
    session = await db.scalar(statement.order_by(BrowserLoginSession.updated_at.desc()))
    if session is None:
        return AccountSessionResolution("waiting", reason="no execution session has been claimed")
    lease = await _active_lease(db, ref.workspace_id, ref.account_id)
    if lease is None or lease.node_id != session.node_id or lease.epoch != session.epoch:
        return AccountSessionResolution("waiting", reason="fenced node lease is not available")
    if _as_utc(lease.expires_at) <= _now():
        return AccountSessionResolution("waiting", reason="node lease has expired")
    if not account.profile_manifest_id:
        return AccountSessionResolution(
            "blocked",
            error_code=BrowserAccountErrorCode.PROFILE_CORRUPT,
            reason="account has no committed profile manifest",
        )
    manifest = await db.get(BrowserProfileManifest, account.profile_manifest_id)
    if (
        manifest is None
        or manifest.workspace_id != account.workspace_id
        or manifest.account_id != account.id
        or manifest.profile_id != session.profile_id
        or manifest.version != session.profile_version
        or manifest.state != "committed"
    ):
        return AccountSessionResolution(
            "blocked",
            error_code=BrowserAccountErrorCode.PROFILE_CORRUPT,
            reason="committed profile manifest does not match the account",
        )
    session.node_id = lease.node_id
    session.node_boot_id = lease.node_boot_id
    session.lease_id = lease.lease_id
    try:
        envelope = await _session_envelope(account, session)
    except BrowserAccountError as exc:
        if exc.code == "waiting":
            return AccountSessionResolution("waiting", reason=str(exc))
        return AccountSessionResolution("blocked", error_code=BrowserAccountErrorCode(exc.code), reason=str(exc))
    # Replace the helper's conservative deadline with the authoritative lease.
    envelope = envelope.model_copy(update={"lease_expires_at": lease.expires_at})
    return AccountSessionResolution("ready", session=envelope)


async def get_portal_authorization_facts(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    subject: str,
) -> PortalAuthorizationFactsV1:
    """Read bounded, fresh membership/session facts for every portal replica."""

    now = _now()
    row = await db.execute(
        select(WorkspaceMembership, User, Workspace)
        .join(User, User.id == WorkspaceMembership.user_id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .where(User.subject == subject)
    )
    membership = row.one_or_none()
    session = await _session_or_error(db, workspace_id, account_id, session_id)
    account = await _account_or_error(db, workspace_id, account_id)
    role = membership.WorkspaceMembership.role.value if membership is not None else None
    user_disabled = True if membership is None else bool(membership.User.disabled)
    workspace_active = False if membership is None else bool(membership.Workspace.active)
    session_revoked = session.status in _TERMINAL_SESSION_STATUSES or (
        session.expires_at is not None and _as_utc(session.expires_at) <= now
    )
    return PortalAuthorizationFactsV1(
        workspace_id=workspace_id,
        account_id=account_id,
        session_id=session_id,
        membership_exists=membership is not None,
        role=role,
        user_disabled=user_disabled,
        workspace_active=workspace_active,
        session_revoked=session_revoked,
        session_revision=account.revision,
        session_expires_at=_as_utc(session.expires_at) if session.expires_at is not None else now,
        checked_at=now,
        freshness_deadline=now + timedelta(milliseconds=500),
    )


async def apply_login_observation(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    observation: LoginObservationV1,
) -> BrowserLoginSession:
    """Apply a bounded Script Host observation with generation and identity fencing."""

    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, observation.session_id, for_update=True)
    if observation.epoch != session.epoch or observation.view_generation < session.view_generation:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "login observation is stale")
    session.login_rule_id = observation.rule_id
    session.login_rule_version = observation.rule_version
    session.tab_id = str(observation.target.tab_id) if observation.target.tab_id is not None else None
    session.frame_id = str(observation.target.frame_id) if observation.target.frame_id is not None else None
    session.document_id = str(observation.target.document_id) if observation.target.document_id is not None else None
    session.origin = observation.target.origin
    session.view_generation = observation.view_generation
    session.status = observation.state
    if observation.external_identity is not None:
        identity = _identity_dump(observation.external_identity)
        if account.platform_identity and account.platform_identity != identity:
            account.auth_evidence = BrowserAuthEvidence.INVALID.value
            account.auth_required = True
            account.status = BrowserAccountStatus.ERROR.value
            account.status_reason_code = BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH.value
            _set_revision(account)
            await db.flush()
            raise BrowserAccountError(BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH, "observed identity does not match account")
        account.platform_identity = identity
    if observation.evidence_kind == _SchemaBrowserAuthEvidence.VALID:
        account.auth_evidence = BrowserAuthEvidence.VALID.value
        account.evidence_source = BrowserEvidenceSource.RULE_VERIFIED.value
        account.evidence_observed_at = observation.observed_at
        account.auth_required = False
        account.status = BrowserAccountStatus.SAVING.value
        session.status = BrowserAccountStatus.SAVING.value
        await db.flush()
        command = await _enqueue_command(
            db,
            account,
            BrowserCommandKind.STOP_AND_SAVE,
            idempotency_scope=f"session:{session.id}:save",
            idempotency_key=str(observation.view_generation),
            session_id=session.id,
            payload=StopAndSaveCommandPayloadV1(expected_profile_version=account.profile_version),
        )
        session.command_id = command.id
    elif observation.evidence_kind == _SchemaBrowserAuthEvidence.INVALID:
        account.auth_evidence = BrowserAuthEvidence.INVALID.value
        account.auth_required = True
        account.status = BrowserAccountStatus.ERROR.value
        account.status_reason_code = observation.error_code.value if observation.error_code else BrowserAccountErrorCode.AUTH_REQUIRED.value
    else:
        account.status = observation.state
        account.auth_required = observation.state == BrowserAccountStatus.CHALLENGE.value
    _set_revision(account)
    await db.flush()
    return session


async def apply_node_result(db: AsyncSession, result: NodeResultV1) -> BrowserDurableCommand:
    """Fence one node result before changing account state; stale results never save."""

    command = await db.get(BrowserDurableCommand, result.command_id)
    if command is None or command.node_id != result.node_id:
        raise BrowserAccountError(BrowserAccountErrorCode.LEASE_LOST, "command or node fencing identity is invalid")
    if command.session_id != result.session_id:
        raise BrowserAccountError(BrowserAccountErrorCode.LEASE_LOST, "command/session fencing identity is invalid")
    if command.epoch != result.epoch or command.expected_revision != result.expected_revision:
        raise BrowserAccountError(BrowserAccountErrorCode.LEASE_LOST, "node result is stale")
    account = await _account_or_error(db, command.workspace_id, command.account_id, for_update=True)
    if account.revision != result.expected_revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "account revision is stale")
    command.result = result.evidence.to_wire()
    command.error_code = result.error_code.value if result.error_code else None
    command.status = "succeeded" if result.status == "succeeded" else "failed"
    command.completed_at = _now()
    if result.profile_manifest_ref:
        manifest = await db.get(BrowserProfileManifest, result.profile_manifest_ref)
        if (
            manifest is None
            or manifest.workspace_id != account.workspace_id
            or manifest.account_id != account.id
            or manifest.state != "committed"
        ):
            raise BrowserAccountError(
                BrowserAccountErrorCode.PROFILE_CORRUPT,
                "node result references an uncommitted or foreign profile manifest",
            )
        account.profile_manifest_id = manifest.id
        account.profile_id = manifest.profile_id
        account.profile_version = manifest.version
    if result.status == "succeeded" and result.evidence.auth_evidence == BrowserAuthEvidence.VALID:
        account.auth_required = False
        account.status = BrowserAccountStatus.SAVED.value if account.profile_manifest_id else BrowserAccountStatus.SAVING.value
    elif result.status in {"failed", "blocked"}:
        account.status = BrowserAccountStatus.ERROR.value
        account.status_reason_code = result.error_code.value if result.error_code else BrowserAccountErrorCode.SAVE_FAILED.value
    _set_revision(account)
    await db.flush()
    return command


__all__ = [
    "AccountSessionResolution",
    "BrowserAccountError",
    "apply_login_observation",
    "apply_node_result",
    "close_login_session",
    "confirm_login_session",
    "create_browser_account",
    "create_login_session",
    "get_browser_account",
    "get_login_session",
    "get_portal_authorization_facts",
    "list_browser_accounts",
    "list_login_sessions",
    "mark_auth_required",
    "queue_account_migration",
    "refresh_login_session",
    "resolve_account_session",
    "resume_browser_account",
    "suspend_browser_account",
    "takeover_login_session",
    "update_browser_account",
]
