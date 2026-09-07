"""Workspace-scoped browser account lifecycle and session fencing.

This module owns the mutable account/session facts behind the frozen QRAC2
contracts.  Runtime execution and node scheduling remain consumers: this
service never chooses an endpoint, accepts shell input, or treats a browser
cookie as proof of authentication.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Mapping
import uuid

from pydantic import SecretStr
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from collections.abc import Awaitable, Callable
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

from backend.models.browser_portal import BrowserPortalOwner, BrowserPortalTicket

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
    DurableCommandV1,
    EmptyCommandPayloadV1,
    ExecuteReferenceCommandPayloadV1,
    ExecutionContextV1,
    ExternalIdentityV1,
    LoginObservationV1,
    MigrateCommandPayloadV1,
    PortalAuthorizationFactsV1,
    PortalTicketConsumeCASV1,
    PortalTicketIssueRequestV1,
    PortalTicketIssuedV1,
    PortalTicketRecordV1,
    PortalTicketRedeemRequestV1,
    RefreshLoginCommandPayloadV1,
    SessionEnvelopeV1,
    SessionResolutionBlockedV1,
    SessionResolutionV1,
    SessionResolutionWaitingV1,
    SessionTargetV1,
    StopAndSaveCommandPayloadV1,
)

from backend.services.browser_portal_contract import (
    consume_portal_ticket_cas,
    issue_first_portal_ticket,
    ticket_record_from_issue,
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

_PORTAL_TERMINAL_SESSION_STATUSES = frozenset(
    (*_TERMINAL_SESSION_STATUSES, BrowserAccountStatus.ERROR.value)
)
_COMMAND_TTL = timedelta(minutes=30)

PORTAL_WEBSOCKET_PATH_TEMPLATE = (
    "/api/v1/workspaces/{workspace_id}/browser-accounts/"
    "{account_id}/login-sessions/{session_id}/portal"
)


def portal_websocket_path(workspace_id: str, account_id: str, session_id: str) -> str:
    """Return the only WebSocket route that can serve an account portal grant."""

    return PORTAL_WEBSOCKET_PATH_TEMPLATE.format(
        workspace_id=workspace_id,
        account_id=account_id,
        session_id=session_id,
    )


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

EnqueueCommand = Callable[[AsyncSession, DurableCommandV1], Awaitable[DurableCommandV1]]


async def _scheduler_enqueue(
    db: AsyncSession,
    command: DurableCommandV1,
) -> DurableCommandV1:
    """Use S's transaction-participating enqueue seam; never insert locally."""

    from backend.services.browser_account_scheduler import enqueue

    try:
        return await enqueue(db, command)
    except Exception as exc:
        code = getattr(exc, "code", None) or "scheduler_unavailable"
        raise BrowserAccountError(code, "durable command enqueue was rejected") from exc


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
    enqueue: EnqueueCommand | None = None,
) -> BrowserDurableCommand:
    """Build a typed command and pass it to S inside A's open transaction."""

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
    command_id = str(uuid.uuid4())
    command = DurableCommandV1(
        command_id=command_id,
        workspace_id=account.workspace_id,
        account_id=account.id,
        node_id=lease.node_id if lease else account.node_id,
        kind=kind,
        idempotency_scope=idempotency_scope,
        idempotency_key=idempotency_key,
        execution_id=execution_id,
        binding_revision_id=binding_revision_id,
        epoch=lease.epoch if lease else 0,
        expected_revision=account.revision,
        available_at=now,
        expires_at=expires_at or now + _COMMAND_TTL,
        session_id=session_id,
        payload=payload or EmptyCommandPayloadV1(),
    )
    persisted = await (enqueue or _scheduler_enqueue)(db, command)
    if isinstance(persisted, BrowserDurableCommand):
        return persisted
    row = await db.get(BrowserDurableCommand, persisted.command_id)
    if row is None:
        raise BrowserAccountError("scheduler_unavailable", "scheduler did not persist durable command")
    return row




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


async def takeover_login_session(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    expected_revision: int | None = None,
) -> BrowserLoginSession:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, session_id, for_update=True)
    if expected_revision is not None and expected_revision != account.revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "account revision is stale")
    if session.status in _TERMINAL_SESSION_STATUSES:
        raise BrowserAccountError(BrowserAccountErrorCode.SESSION_EXPIRED, "login session is closed")
    session.status = BrowserAccountStatus.PRESENTING.value
    account.status = BrowserAccountStatus.PRESENTING.value
    _set_revision(account)
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
        raise BrowserAccountError(
            "invalid_transition",
            "manual confirmation is only valid for an active login challenge",
        )
    # Manual confirmation is an explicit exception, never trusted platform
    # identity evidence and never an automatic save trigger.
    account.auth_required = False
    account.auth_evidence = BrowserAuthEvidence.UNKNOWN.value
    account.evidence_source = BrowserEvidenceSource.MANUAL_FALLBACK.value
    account.evidence_observed_at = _now()
    account.manual_confirmed_by = confirmed_by
    identity = _identity_dump(platform_identity)
    if identity is not None and account.platform_identity and account.platform_identity != identity:
        raise BrowserAccountError(
            BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH,
            "confirmed identity does not match account",
        )
    account.status = BrowserAccountStatus.UNKNOWN.value
    session.status = BrowserAccountStatus.UNKNOWN.value
    _set_revision(account)
    await db.flush()
    return session


async def close_login_session(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    reason: Literal["completed", "cancelled", "expired", "error"] = "cancelled",
    expected_revision: int | None = None,
) -> BrowserLoginSession:
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(db, workspace_id, account_id, session_id, for_update=True)
    if expected_revision is not None and expected_revision != account.revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "account revision is stale")
    if session.status in _TERMINAL_SESSION_STATUSES:
        return session
    session.status = (
        BrowserAccountStatus.EXPIRED.value
        if reason == "expired"
        else BrowserAccountStatus.CLOSED.value
    )
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
    execution_context: ExecutionContextV1 | Mapping[str, Any],
) -> SessionResolutionV1:
    """Resolve one fixed account session without acquiring another slot."""

    try:
        ref = account_ref if isinstance(account_ref, AccountRef) else AccountRef.from_wire(account_ref)
        context = (
            execution_context
            if isinstance(execution_context, ExecutionContextV1)
            else ExecutionContextV1.from_wire(execution_context)
        )
    except Exception as exc:
        raise BrowserAccountError(
            BrowserAccountErrorCode.PERMISSION_DENIED,
            "execution context is not a valid account execution contract",
            403,
        ) from exc
    if context.account_ref != ref:
        raise BrowserAccountError(
            BrowserAccountErrorCode.PERMISSION_DENIED,
            "execution context account does not match the requested account",
            403,
        )
    account = await _account_or_error(db, ref.workspace_id, ref.account_id)

    def waiting(reason: str) -> SessionResolutionWaitingV1:
        normalized = (
            "capacity_missing"
            if "no execution session" in reason
            else "lease_waiting"
        )
        return SessionResolutionWaitingV1(
            account_ref=ref,
            account_revision=account.revision,
            reason=normalized,
        )

    def blocked(code: BrowserAccountErrorCode) -> SessionResolutionBlockedV1:
        return SessionResolutionBlockedV1(
            account_ref=ref,
            account_revision=account.revision,
            error_code=code,
        )
    binding_revision_id = ref.source_binding_revision_id or context.source_binding_revision_id
    if context.source_binding_revision_id not in (None, binding_revision_id):
        return blocked(BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED)
    if binding_revision_id is not None:
        binding_revision = await db.scalar(
            select(SourceBindingRevision).where(
                SourceBindingRevision.id == binding_revision_id,
                SourceBindingRevision.workspace_id == ref.workspace_id,
                SourceBindingRevision.account_id == account.id,
            )
        )
        if binding_revision is None:
            return blocked(BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED)
    if account.paused or account.auth_required:
        return blocked(
            BrowserAccountErrorCode.AUTH_REQUIRED
            if account.auth_required
            else BrowserAccountErrorCode.PERMISSION_DENIED
        )
    if account.status in {
        BrowserAccountStatus.ERROR.value,
        BrowserAccountStatus.CLOSED.value,
        BrowserAccountStatus.EXPIRED.value,
    }:
        return blocked(BrowserAccountErrorCode.SESSION_EXPIRED)
    statement = (
        select(BrowserLoginSession)
        .where(
            BrowserLoginSession.workspace_id == ref.workspace_id,
            BrowserLoginSession.account_id == ref.account_id,
            BrowserLoginSession.purpose == "execution",
            BrowserLoginSession.status.in_(_ACTIVE_SESSION_STATUSES),
        )
        .where(BrowserLoginSession.execution_id == context.execution_id)
    )
    session = await db.scalar(statement.order_by(BrowserLoginSession.updated_at.desc()))
    if session is None:
        return waiting("no execution session has been claimed")
    lease = await _active_lease(db, ref.workspace_id, ref.account_id)
    if (
        lease is None
        or session.lease_id != lease.lease_id
        or lease.node_id != session.node_id
        or lease.epoch != session.epoch
    ):
        return waiting("fenced node lease is not available")
    if _as_utc(lease.expires_at) <= _now():
        return waiting("node lease has expired")
    if not account.profile_manifest_id:
        return blocked(BrowserAccountErrorCode.PROFILE_CORRUPT)
    manifest = await db.get(BrowserProfileManifest, account.profile_manifest_id)
    if (
        manifest is None
        or manifest.workspace_id != account.workspace_id
        or manifest.account_id != account.id
        or manifest.profile_id != session.profile_id
        or manifest.version != session.profile_version
        or manifest.state != "committed"
    ):
        return blocked(BrowserAccountErrorCode.PROFILE_CORRUPT)
    session.node_id = lease.node_id
    session.node_boot_id = lease.node_boot_id
    session.lease_id = lease.lease_id
    try:
        envelope = await _session_envelope(account, session)
    except BrowserAccountError as exc:
        if exc.code == "waiting":
            return waiting(str(exc))
        return blocked(BrowserAccountErrorCode(exc.code))
    return envelope.model_copy(update={"lease_expires_at": lease.expires_at})


@dataclass(frozen=True)
class PortalAuthorizationSnapshot:
    """One fresh DB-backed authorization row used by an active portal."""

    facts: PortalAuthorizationFactsV1
    account_paused: bool
    account_auth_required: bool
    session_purpose: str


async def get_portal_authorization_batch(
    db: AsyncSession,
    requests: list[tuple[str, str, str, str]],
) -> dict[tuple[str, str, str, str], PortalAuthorizationSnapshot]:
    """Read all active portal permissions from one bounded fresh DB snapshot.

    ``requests`` contains ``(workspace_id, account_id, session_id, subject)``.
    An empty request does no I/O.  Callers must discard the snapshot at its
    freshness deadline; it is not a cache or a long-lived transaction.
    """

    if not requests:
        return {}
    now = _now()
    unique_requests = list(dict.fromkeys(requests))
    conditions = [
        and_(
            WorkspaceMembership.workspace_id == workspace_id,
            User.subject == subject,
            BrowserLoginSession.workspace_id == workspace_id,
            BrowserLoginSession.account_id == account_id,
            BrowserLoginSession.id == session_id,
            BrowserAccount.workspace_id == workspace_id,
            BrowserAccount.id == account_id,
        )
        for workspace_id, account_id, session_id, subject in unique_requests
    ]
    try:
        async with asyncio.timeout(0.5):
            result = await db.execute(
                select(
                    WorkspaceMembership,
                    User,
                    Workspace,
                    BrowserLoginSession,
                    BrowserAccount,
                )
                .join(User, User.id == WorkspaceMembership.user_id)
                .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
                .join(
                    BrowserAccount,
                    and_(
                        BrowserAccount.workspace_id == Workspace.id,
                        BrowserAccount.id.in_(
                            [account_id for _, account_id, _, _ in unique_requests]
                        ),
                    ),
                )
                .join(
                    BrowserLoginSession,
                    and_(
                        BrowserLoginSession.workspace_id == Workspace.id,
                        BrowserLoginSession.account_id == BrowserAccount.id,
                    ),
                )
                .where(or_(*conditions))
            )
            rows = result.all()
    except TimeoutError as exc:
        raise BrowserAccountError(
            BrowserAccountErrorCode.PERMISSION_DENIED,
            "portal authorization could not be refreshed",
            503,
        ) from exc

    snapshots: dict[tuple[str, str, str, str], PortalAuthorizationSnapshot] = {}
    for membership, user, workspace, session, account in rows:
        key = (workspace.id, account.id, session.id, user.subject)
        if key not in unique_requests:
            continue
        session_revoked = session.status in _PORTAL_TERMINAL_SESSION_STATUSES or (
            session.expires_at is not None and _as_utc(session.expires_at) <= now
        )
        facts = PortalAuthorizationFactsV1(
            workspace_id=workspace.id,
            account_id=account.id,
            session_id=session.id,
            membership_exists=True,
            role=membership.role.value,
            user_disabled=bool(user.disabled),
            workspace_active=bool(workspace.active),
            account_revision=account.revision,
            session_revoked=session_revoked,
            session_revision=session.revision,
            session_expires_at=(
                _as_utc(session.expires_at) if session.expires_at is not None else now
            ),
            checked_at=now,
            freshness_deadline=now + timedelta(milliseconds=500),
        )
        snapshots[key] = PortalAuthorizationSnapshot(
            facts=facts,
            account_paused=bool(account.paused),
            account_auth_required=bool(account.auth_required),
            session_purpose=session.purpose,
        )
    return snapshots


async def get_portal_authorization_facts(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    subject: str,
) -> PortalAuthorizationFactsV1:
    """Read one portal authorization fact through the same batch path."""

    key = (workspace_id, account_id, session_id, subject)
    snapshot = (await get_portal_authorization_batch(db, [key])).get(key)
    if snapshot is None:
        # Preserve the historical 404/403-neutral behavior for missing scope.
        raise BrowserAccountError(
            BrowserAccountErrorCode.PERMISSION_DENIED,
            "portal authorization could not be confirmed",
            403,
        )
    return snapshot.facts


async def apply_login_observation(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    observation: LoginObservationV1,
) -> BrowserLoginSession:
    """Apply one trusted observation and queue saving through S."""

    if (
        observation.account_ref.workspace_id != workspace_id
        or observation.account_ref.account_id != account_id
        or observation.claim.workspace_id != workspace_id
        or observation.claim.account_id != account_id
    ):
        raise BrowserAccountError(BrowserAccountErrorCode.PERMISSION_DENIED, "observation account scope is invalid", 403)
    account = await _account_or_error(db, workspace_id, account_id, for_update=True)
    session = await _session_or_error(
        db, workspace_id, account_id, observation.session_id, for_update=True
    )
    if (
        observation.epoch != session.epoch
        or observation.view_generation < session.view_generation
        or session.command_id != observation.claim.command_id
        or session.node_id != observation.node_identity.node_id
        or session.node_boot_id != observation.node_identity.boot_id
    ):
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "login observation is stale")
    session.login_rule_id = observation.rule_id
    session.login_rule_version = observation.rule_version
    session.tab_id = str(observation.target.tab_id)
    session.frame_id = str(observation.target.frame_id)
    session.document_id = str(observation.target.document_id)
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
            raise BrowserAccountError(
                BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH,
                "observed identity does not match account",
            )
        account.platform_identity = identity
    revision_bumped = False
    if observation.evidence_kind == _SchemaBrowserAuthEvidence.VALID:
        account.auth_evidence = BrowserAuthEvidence.VALID.value
        account.evidence_source = BrowserEvidenceSource.RULE_VERIFIED.value
        account.evidence_observed_at = observation.observed_at
        account.auth_required = False
        account.status = BrowserAccountStatus.SAVING.value
        session.status = BrowserAccountStatus.SAVING.value
        _set_revision(account)
        revision_bumped = True
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
        account.status_reason_code = (
            observation.error_code.value
            if observation.error_code
            else BrowserAccountErrorCode.AUTH_REQUIRED.value
        )
    else:
        account.status = observation.state
        account.auth_required = observation.state == BrowserAccountStatus.CHALLENGE.value
    if not revision_bumped:
        _set_revision(account)
    await db.flush()
    return session


async def apply_node_result(
    db: AsyncSession,
    command: BrowserDurableCommand | DurableCommandV1,
    result: NodeResultV1,
) -> BrowserDurableCommand:
    """Apply one fenced result; this transition performs no browser I/O."""

    if isinstance(command, DurableCommandV1):
        command_row = await db.get(BrowserDurableCommand, command.command_id)
    else:
        command_row = command
    if command_row is None:
        raise BrowserAccountError(BrowserAccountErrorCode.LEASE_LOST, "durable command is not found")
    if (
        command_row.workspace_id != result.workspace_id
        or command_row.account_id != result.account_id
        or command_row.id != result.command_id
        or command_row.session_id != result.session_id
        or command_row.node_id != result.node_id
        or command_row.epoch != result.epoch
        or command_row.expected_revision != result.expected_revision
    ):
        raise BrowserAccountError(
            BrowserAccountErrorCode.LEASE_LOST,
            "node result fencing identity is invalid",
        )
    account = await _account_or_error(
        db, command_row.workspace_id, command_row.account_id, for_update=True
    )
    session = await _session_or_error(
        db,
        command_row.workspace_id,
        command_row.account_id,
        result.session_id,
        for_update=True,
    )
    lease = await db.scalar(
        select(BrowserAccountLease).where(
            BrowserAccountLease.workspace_id == command_row.workspace_id,
            BrowserAccountLease.account_id == command_row.account_id,
            BrowserAccountLease.node_id == result.node_id,
            BrowserAccountLease.node_boot_id == result.boot_id,
            BrowserAccountLease.epoch == result.epoch,
            BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
        )
    )
    if lease is None or session.lease_id != lease.lease_id:
        raise BrowserAccountError(BrowserAccountErrorCode.LEASE_LOST, "node lease is not active")
    if (
        session.epoch != result.epoch
        or session.node_id != result.node_id
        or session.node_boot_id != result.boot_id
        or account.revision != result.expected_revision
    ):
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "node result is stale")

    command_row.result = result.evidence.to_wire()
    command_row.error_code = result.error_code.value if result.error_code else None
    command_row.status = "succeeded" if result.status == "succeeded" else "failed"
    command_row.completed_at = _now()

    observed_identity = _identity_dump(result.external_identity)
    identity_mismatch = bool(
        observed_identity is not None
        and account.platform_identity is not None
        and account.platform_identity != observed_identity
    )
    if identity_mismatch:
        account.auth_evidence = BrowserAuthEvidence.INVALID.value
        account.auth_required = True
        account.status = BrowserAccountStatus.ERROR.value
        account.status_reason_code = BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH.value
        _set_revision(account)
        await db.flush()
        return command_row
    if observed_identity is not None:
        account.platform_identity = observed_identity

    manifest = None
    if result.profile_manifest_ref:
        manifest = await db.get(BrowserProfileManifest, result.profile_manifest_ref)
        if (
            manifest is None
            or manifest.workspace_id != account.workspace_id
            or manifest.account_id != account.id
            or manifest.command_id != command_row.id
            or manifest.node_id != result.node_id
            or manifest.writer_epoch != result.epoch
            or manifest.state != "committed"
        ):
            raise BrowserAccountError(
                BrowserAccountErrorCode.PROFILE_CORRUPT,
                "node result references an uncommitted or foreign profile manifest",
            )
        account.profile_manifest_id = manifest.id
        account.profile_id = manifest.profile_id
        account.profile_version = manifest.version
        session.profile_id = manifest.profile_id

        session.profile_version = manifest.version
        session.profile_state = "committed"
    if result.status == "succeeded" and result.evidence.auth_evidence == BrowserAuthEvidence.VALID:
        account.auth_required = False
        account.status = (
            BrowserAccountStatus.SAVED.value
            if manifest is not None or account.profile_manifest_id
            else BrowserAccountStatus.SAVING.value
        )
        session.status = account.status
    elif result.status in {"failed", "blocked"}:
        account.status = BrowserAccountStatus.ERROR.value
        account.status_reason_code = (
            result.error_code.value
            if result.error_code
            else BrowserAccountErrorCode.SAVE_FAILED.value
        )
        session.status = BrowserAccountStatus.ERROR.value
PortalTicketStore = Callable[[AsyncSession, PortalTicketRecordV1], Awaitable[None]]
PortalTicketLoader = Callable[
    [AsyncSession, str], Awaitable[PortalTicketRecordV1 | None]
]
PortalTicketCAS = Callable[
    [AsyncSession, PortalTicketConsumeCASV1, datetime], Awaitable[bool]
]


def _portal_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _portal_record(row: BrowserPortalTicket) -> PortalTicketRecordV1:
    return PortalTicketRecordV1(
        ticket_id=row.ticket_id,
        ticket_digest=row.ticket_digest,
        csrf_digest=row.csrf_digest,
        subject=row.subject,
        account_ref=AccountRef(workspace_id=row.workspace_id, account_id=row.account_id),
        session_id=row.session_id,
        session_revision=row.session_revision,
        issued_at=_as_utc(row.issued_at),
        expires_at=_as_utc(row.expires_at),
        hard_expires_at=_as_utc(row.hard_expires_at),
        consumed_at=_as_utc(row.consumed_at) if row.consumed_at else None,
    )


async def _store_portal_ticket(
    db: AsyncSession,
    record: PortalTicketRecordV1,
) -> None:
    db.add(
        BrowserPortalTicket(
            id=record.ticket_id,
            ticket_id=record.ticket_id,
            ticket_digest=record.ticket_digest,
            csrf_digest=record.csrf_digest,
            subject=record.subject,
            workspace_id=record.account_ref.workspace_id,
            account_id=record.account_ref.account_id,
            session_id=record.session_id,
            session_revision=record.session_revision,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            hard_expires_at=record.hard_expires_at,
        )
    )
    await db.flush()


async def _load_portal_ticket(
    db: AsyncSession,
    ticket_id: str,
) -> PortalTicketRecordV1 | None:
    row = await db.scalar(
        select(BrowserPortalTicket).where(BrowserPortalTicket.ticket_id == ticket_id)
    )
    return _portal_record(row) if row is not None else None


async def _consume_portal_ticket(
    db: AsyncSession,
    cas: PortalTicketConsumeCASV1,
    now: datetime,
    *,
    owner_token: str,
    subject: str,
) -> bool:
    row = await db.scalar(
        select(BrowserPortalTicket)
        .where(
            BrowserPortalTicket.ticket_id == cas.ticket_id,
            BrowserPortalTicket.workspace_id == cas.account_ref.workspace_id,
            BrowserPortalTicket.account_id == cas.account_ref.account_id,
            BrowserPortalTicket.session_id == cas.session_id,
            BrowserPortalTicket.session_revision == cas.expected_session_revision,
            BrowserPortalTicket.consumed_at.is_(None),
            BrowserPortalTicket.expires_at > now,
            BrowserPortalTicket.hard_expires_at > now,
        )
        .with_for_update()
    )
    if row is None:
        return False
    session = await db.scalar(
        select(BrowserLoginSession).where(
            BrowserLoginSession.workspace_id == row.workspace_id,
            BrowserLoginSession.account_id == row.account_id,
            BrowserLoginSession.id == row.session_id,
        )
    )
    account = await db.scalar(
        select(BrowserAccount).where(
            BrowserAccount.workspace_id == row.workspace_id,
            BrowserAccount.id == row.account_id,
        )
    )
    if (
        session is None
        or account is None
        or session.revision != row.session_revision
        or session.status in _PORTAL_TERMINAL_SESSION_STATUSES
        or (session.expires_at is not None and _as_utc(session.expires_at) <= now)
        or account.paused
        or (account.auth_required and session.purpose != "login")
    ):
        return False
    row.consumed_at = now
    db.add(
        BrowserPortalOwner(
            owner_digest=_portal_digest(owner_token),
            subject=subject,
            workspace_id=row.workspace_id,
            account_id=row.account_id,
            session_id=row.session_id,
            ticket_id=row.ticket_id,
            session_revision=row.session_revision,
            issued_at=now,
            expires_at=min(_as_utc(row.expires_at), now + timedelta(minutes=10)),
            hard_expires_at=_as_utc(row.hard_expires_at),
            active=True,
        )
    )
    await db.flush()
    return True


async def issue_portal_ticket(
    db: AsyncSession,
    request: PortalTicketIssueRequestV1,
    *,
    subject: str,
    store: PortalTicketStore | None = None,
    cookie_lifetime_seconds: int = 600,
) -> PortalTicketIssuedV1:
    account = await _account_or_error(
        db, request.account_ref.workspace_id, request.account_ref.account_id, for_update=True
    )
    session = await _session_or_error(
        db,
        request.account_ref.workspace_id,
        request.account_ref.account_id,
        request.session_id,
        for_update=True,
    )
    if session.revision != request.expected_session_revision:
        raise BrowserAccountError(BrowserAccountErrorCode.STALE_GENERATION, "session revision is stale")
    if session.status in _PORTAL_TERMINAL_SESSION_STATUSES or (
        session.expires_at is not None and _as_utc(session.expires_at) <= _now()
    ):
        raise BrowserAccountError(BrowserAccountErrorCode.SESSION_EXPIRED, "login session is closed", 410)
    if account.paused or (account.auth_required and session.purpose != "login"):
        raise BrowserAccountError(BrowserAccountErrorCode.PERMISSION_DENIED, "account portal is not authorized", 403)
    now = _now()
    expires_at = now + timedelta(seconds=min(max(cookie_lifetime_seconds, 60), 600))
    issued = issue_first_portal_ticket(
        request,
        ticket=SecretStr(secrets.token_urlsafe(32)),
        now=now,
        expires_at=expires_at,
        hard_expires_at=now + timedelta(minutes=30),
        session_revision=session.revision,
    )
    await (store or _store_portal_ticket)(db, ticket_record_from_issue(issued, subject=subject))
    return issued


async def redeem_portal_ticket(
    db: AsyncSession,
    request: PortalTicketRedeemRequestV1,
    *,
    subject: str,
    load: PortalTicketLoader | None = None,
    consume: PortalTicketCAS | None = None,
    cookie_name: str = "qrac2_portal",
    websocket_path: str | None = None,
    owner_token: str | None = None,
) -> PortalEntryResponseV1:
    owner_token = owner_token or secrets.token_urlsafe(32)
    record = await (load or _load_portal_ticket)(db, request.ticket_id)
    if record is None:
        raise BrowserAccountError(BrowserAccountErrorCode.SESSION_EXPIRED, "portal ticket is expired", 410)
    now = _now()
    if now >= record.expires_at or now >= record.hard_expires_at:
        raise BrowserAccountError(BrowserAccountErrorCode.SESSION_EXPIRED, "portal ticket is expired", 410)
    if consume is None:
        account = await _account_or_error(
            db, request.account_ref.workspace_id, request.account_ref.account_id
        )
        session = await _session_or_error(
            db,
            request.account_ref.workspace_id,
            request.account_ref.account_id,
            request.session_id,
        )
        if (
            account.paused
            or (account.auth_required and session.purpose != "login")
            or session.status in _PORTAL_TERMINAL_SESSION_STATUSES
            or (session.expires_at is not None and _as_utc(session.expires_at) <= now)
            or session.revision != request.expected_session_revision
        ):
            raise BrowserAccountError(
                BrowserAccountErrorCode.SESSION_EXPIRED,
                "login session is no longer authorized",
                410,
            )
    consume_fn = consume or (
        lambda session, cas, consumed_at: _consume_portal_ticket(
            session, cas, consumed_at, owner_token=owner_token, subject=subject
        )
    )
    return await consume_portal_ticket_cas(
        request,
        record=record,
        authenticated_subject=subject,
        now=now,
        cookie_name=cookie_name,
        websocket_path=websocket_path
        or portal_websocket_path(
            request.account_ref.workspace_id,
            request.account_ref.account_id,
            request.session_id,
        ),
        cas_update=lambda cas, consumed_at: consume_fn(db, cas, consumed_at),
    )


__all__ = [
    "AccountSessionResolution",
    "BrowserAccountError",
    "PortalAuthorizationSnapshot",
    "apply_login_observation",
    "apply_node_result",
    "close_login_session",
    "confirm_login_session",
    "create_browser_account",
    "create_login_session",
    "get_browser_account",
    "get_login_session",
    "get_portal_authorization_batch",
    "get_portal_authorization_facts",
    "issue_portal_ticket",
    "portal_websocket_path",
    "redeem_portal_ticket",
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
