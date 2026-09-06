"""Durable center-side scheduling for browser accounts.

This module owns only the center's command/lease/capacity transaction.  Account
state transitions and browser execution remain the responsibility of the A and R
slices.  In particular, this code never picks a replacement account when a
fixed account is unavailable and never treats an expired database lease as
proof that a browser writer stopped.

The public methods accept the versioned contracts from
``backend.schemas.browser_account``.  Persistent command payloads are validated
before they are written, and node claims/results are fenced by node id, boot
id, epoch, session, and the account revision captured by the command.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal, commit_session
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserAccountStatus,
    BrowserCommandKind,
    BrowserCommandStatus,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserLeaseStatus,
)
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.schemas.browser_account import (
    BrowserAccountErrorCode,
    DurableCommandV1,
    NodeClaimV1,
    NodeResultV1,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 30
MAX_BATCH_SIZE = 100
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|cookie|otp|qr|credential|authorization|"
    r"input|html|screenshot|raw[_-]?page)",
    re.IGNORECASE,
)


class BrowserAccountSchedulerError(ValueError):
    """Base class for safe, structured scheduler rejections."""

    code = "scheduler_error"


class SchedulerValidationError(BrowserAccountSchedulerError):
    code = "invalid_scheduler_input"


class SchedulerConflict(BrowserAccountSchedulerError):
    code = "scheduler_conflict"


class NodeUnavailable(BrowserAccountSchedulerError):
    code = BrowserAccountErrorCode.NODE_UNAVAILABLE.value


class LeaseLost(BrowserAccountSchedulerError):
    code = BrowserAccountErrorCode.LEASE_LOST.value


class ResultConflict(BrowserAccountSchedulerError):
    code = "result_conflict"


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    """Authenticated node identity used by every durable node operation.

    ``node_id`` is the center-assigned persistent identity.  ``boot_id`` is a
    process/deployment generation and is deliberately not inferred from the
    node URL.  ``owner_id`` distinguishes concurrent agents using the same
    node identity; the credential itself is checked by the R HTTP/WS envelope
    before this service is called.
    """

    node_id: str
    boot_id: str
    owner_id: str
    credential_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryStats:
    expired_commands: int = 0
    quarantined_leases: int = 0

    @property
    def processed(self) -> int:
        return self.expired_commands + self.quarantined_leases


_T = TypeVar("_T")
StateTransition = Callable[
    [AsyncSession, BrowserDurableCommand, NodeResultV1],
    Awaitable[None] | None,
]


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    """Normalize SQLite's sometimes-naive timestamps for comparisons."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_node_identity(value: NodeIdentity | Mapping[str, Any] | Any) -> NodeIdentity:
    if isinstance(value, NodeIdentity):
        identity = value
    elif isinstance(value, Mapping):
        identity = NodeIdentity(
            node_id=str(value.get("node_id") or ""),
            boot_id=str(value.get("boot_id") or ""),
            # Older callers used only a node identity.  Falling back to the
            # center-assigned node id still yields a stable owner partition;
            # authenticated R callers should always provide owner_id.
            owner_id=str(value.get("owner_id") or value.get("node_id") or ""),
            credential_id=(
                str(value["credential_id"])
                if value.get("credential_id") is not None
                else None
            ),
        )
    else:
        identity = NodeIdentity(
            node_id=str(getattr(value, "node_id", "") or ""),
            boot_id=str(getattr(value, "boot_id", "") or ""),
            owner_id=str(
                getattr(value, "owner_id", None)
                or getattr(value, "node_id", "")
                or ""
            ),
            credential_id=(
                str(getattr(value, "credential_id"))
                if getattr(value, "credential_id", None) is not None
                else None
            ),
        )
    if not identity.node_id or not identity.boot_id or not identity.owner_id:
        raise SchedulerValidationError("node identity requires node_id, boot_id, and owner_id")
    if len(identity.node_id) > 36 or len(identity.boot_id) > 128 or len(identity.owner_id) > 255:
        raise SchedulerValidationError("node identity exceeds the contract boundary")
    return identity


def _reject_secret_keys(value: Any) -> None:
    """Defend the persistence boundary even if a contract grows later."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise SchedulerValidationError("persistent command payload contains a sensitive field")
            _reject_secret_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_secret_keys(child)


def _coerce_command(command: DurableCommandV1 | Mapping[str, Any]) -> DurableCommandV1:
    try:
        value = command if isinstance(command, DurableCommandV1) else DurableCommandV1.from_wire(command)
        # Round-trip through the canonical wire representation so union payload
        # validation happens before any database operation.
        canonical = DurableCommandV1.from_wire(value.to_wire())
    except Exception as exc:
        if isinstance(exc, BrowserAccountSchedulerError):
            raise
        raise SchedulerValidationError("invalid durable command contract") from exc
    _reject_secret_keys(canonical.to_wire().get("payload", {}))
    return canonical


def _command_wire(command: BrowserDurableCommand) -> dict[str, Any]:
    """Build a safe contract projection from a durable ORM row."""

    return {
        "command_id": command.id,
        "workspace_id": command.workspace_id,
        "account_id": command.account_id,
        "node_id": command.node_id,
        "kind": command.kind,
        "idempotency_scope": command.idempotency_scope,
        "idempotency_key": command.idempotency_key,
        "execution_id": command.execution_id,
        "binding_revision_id": command.binding_revision_id,
        "epoch": command.epoch,
        "expected_revision": command.expected_revision,
        "available_at": _utc(command.available_at),
        "expires_at": _utc(command.expires_at),
        "status": command.status,
        "session_id": command.session_id,
        "payload": command.payload or {},
    }


def _command_contract(command: BrowserDurableCommand) -> DurableCommandV1:
    try:
        return DurableCommandV1.from_wire(_command_wire(command))
    except Exception as exc:
        # Database rows are trusted only after the same contract validation as
        # new input.  Do not echo JSON payloads in errors.
        raise SchedulerConflict("persisted durable command is not a valid contract") from exc


def _canonical_command_fields(
    command: DurableCommandV1,
    *,
    include_command_id: bool = True,
) -> dict[str, Any]:
    wire = command.to_wire()
    # status, node assignment, and epoch are mutable scheduler fields.  An
    # idempotent retry must match the immutable request, not its current claim.
    wire.pop("status", None)
    wire.pop("node_id", None)
    wire.pop("epoch", None)
    if not include_command_id:
        wire.pop("command_id", None)
    return wire


def _result_wire(result: NodeResultV1) -> dict[str, Any]:
    wire = result.to_wire()
    _reject_secret_keys(wire)
    return wire


def _terminal_status(result_status: str) -> BrowserCommandStatus:
    return (
        BrowserCommandStatus.SUCCEEDED
        if result_status == "succeeded"
        else BrowserCommandStatus.FAILED
    )


class BrowserAccountScheduler:
    """PostgreSQL-backed scheduler for one account command stream.

    Methods taking a caller-provided ``AsyncSession`` join that transaction and
    flush their changes without committing.  Methods without ``db`` acquire a
    short-lived session and commit exactly one scheduler transaction.
    """

    def __init__(
        self,
        *,
        session_factory: Any = AsyncSessionLocal,
        lease_ttl: timedelta = timedelta(seconds=DEFAULT_LEASE_SECONDS),
        clock: Callable[[], datetime] = _now,
        state_transition: StateTransition | None = None,
    ) -> None:
        if lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")
        self.session_factory = session_factory
        self.lease_ttl = lease_ttl
        self.clock = clock
        self.state_transition = state_transition

    async def _owned(self, operation: Callable[[AsyncSession], Awaitable[_T]]) -> _T:
        async with self.session_factory() as db:
            result = await operation(db)
            await commit_session(db)
            return result

    async def enqueue(
        self,
        db: AsyncSession,
        command: DurableCommandV1 | Mapping[str, Any],
    ) -> DurableCommandV1:
        """Persist one fully validated command without committing the caller's transaction."""

        value = _coerce_command(command)
        await db.flush()

        existing = await db.get(BrowserDurableCommand, value.command_id, with_for_update=True)
        if existing is not None:
            if _canonical_command_fields(_command_contract(existing)) != _canonical_command_fields(value):
                raise SchedulerConflict("command_id is already bound to a different request")
            return _command_contract(existing)

        account = await db.scalar(
            select(BrowserAccount)
            .where(
                BrowserAccount.workspace_id == value.workspace_id,
                BrowserAccount.id == value.account_id,
            )
            .with_for_update()
        )
        if account is None:
            raise SchedulerValidationError("account does not belong to workspace")
        if value.node_id is not None and account.node_id not in (None, value.node_id):
            raise SchedulerConflict("command node does not match account ownership")
        if value.expected_revision != account.revision:
            raise SchedulerConflict("command revision is stale")

        duplicate = await db.scalar(
            select(BrowserDurableCommand)
            .where(
                BrowserDurableCommand.workspace_id == value.workspace_id,
                BrowserDurableCommand.idempotency_scope == value.idempotency_scope,
                BrowserDurableCommand.idempotency_key == value.idempotency_key,
            )
            .with_for_update()
        )
        if duplicate is not None:
            if _canonical_command_fields(
                _command_contract(duplicate), include_command_id=False
            ) != _canonical_command_fields(value, include_command_id=False):
                raise SchedulerConflict("idempotency key is already bound to a different request")
            return _command_contract(duplicate)

        row = BrowserDurableCommand(
            id=value.command_id,
            workspace_id=value.workspace_id,
            account_id=value.account_id,
            node_id=value.node_id,
            kind=value.kind.value,
            idempotency_scope=value.idempotency_scope,
            idempotency_key=value.idempotency_key,
            execution_id=value.execution_id,
            binding_revision_id=value.binding_revision_id,
            epoch=value.epoch,
            expected_revision=value.expected_revision,
            available_at=_utc(value.available_at),
            expires_at=_utc(value.expires_at),
            status=value.status.value,
            session_id=value.session_id,
            payload=value.to_wire().get("payload", {}),
        )
        db.add(row)
        await db.flush()
        return _command_contract(row)

    async def _lock_node_for_work(self, db: AsyncSession, identity: NodeIdentity) -> tuple[EdgeNode, EdgeNodeBoot, EdgeNodeCapacity] | None:
        node = await db.scalar(
            select(EdgeNode).where(EdgeNode.id == identity.node_id).with_for_update()
        )
        if node is None:
            return None
        if (
            node.boot_id != identity.boot_id
            or node.quarantined
            or not node.account_capable
            or node.status != "online"
            or (node.credential_id is not None and identity.credential_id != node.credential_id)
        ):
            return None
        boot = await db.scalar(
            select(EdgeNodeBoot)
            .where(
                EdgeNodeBoot.node_id == identity.node_id,
                EdgeNodeBoot.boot_id == identity.boot_id,
                EdgeNodeBoot.status == "active",
            )
            .with_for_update()
        )
        capacity = await db.scalar(
            select(EdgeNodeCapacity)
            .where(
                EdgeNodeCapacity.node_id == identity.node_id,
                EdgeNodeCapacity.boot_id == identity.boot_id,
                EdgeNodeCapacity.valid.is_(True),
            )
            .with_for_update()
        )
        now = self.clock()
        if boot is None or capacity is None or _utc(capacity.expires_at) <= _utc(now):
            return None
        return node, boot, capacity

    @staticmethod
    def _command_can_run(account: BrowserAccount, command: BrowserDurableCommand) -> bool:
        if account.paused or account.status in {
            BrowserAccountStatus.CLOSED.value,
            BrowserAccountStatus.EXPIRED.value,
        }:
            return False
        if command.kind == BrowserCommandKind.EXECUTE_REFERENCE.value:
            if account.auth_required or account.status not in {
                BrowserAccountStatus.SAVED.value,
                BrowserAccountStatus.DORMANT.value,
            }:
                return False
        return command.session_id is not None

    async def _expire_command(self, command: BrowserDurableCommand, now: datetime) -> None:
        command.status = BrowserCommandStatus.EXPIRED.value
        command.completed_at = now
        command.error_code = BrowserAccountErrorCode.SESSION_EXPIRED.value

    async def _quarantine_lease(self, lease: BrowserAccountLease, now: datetime) -> None:
        lease.status = BrowserLeaseStatus.QUARANTINED.value
        lease.updated_at = now

    async def _claim_in_session(
        self,
        db: AsyncSession,
        identity: NodeIdentity,
        limit: int,
    ) -> list[NodeClaimV1]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_BATCH_SIZE:
            raise SchedulerValidationError("claim limit must be between 1 and 100")
        locked = await self._lock_node_for_work(db, identity)
        if locked is None:
            return []
        _node, boot, capacity = locked
        now = _utc(self.clock())
        if capacity.occupied_slots >= capacity.slot_limit:
            return []

        commands = list(
            (
                await db.scalars(
                    select(BrowserDurableCommand)
                    .where(
                        BrowserDurableCommand.status == BrowserCommandStatus.QUEUED.value,
                        BrowserDurableCommand.available_at <= now,
                        BrowserDurableCommand.expires_at > now,
                        or_(
                            BrowserDurableCommand.node_id.is_(None),
                            BrowserDurableCommand.node_id == identity.node_id,
                        ),
                    )
                    .order_by(
                        BrowserDurableCommand.available_at,
                        BrowserDurableCommand.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

        claims: list[NodeClaimV1] = []
        for command in commands:
            if _utc(command.expires_at) <= now:
                await self._expire_command(command, now)
                continue
            account = await db.scalar(
                select(BrowserAccount)
                .where(
                    BrowserAccount.workspace_id == command.workspace_id,
                    BrowserAccount.id == command.account_id,
                )
                .with_for_update()
            )
            if account is None or (
                account.node_id is not None and account.node_id != identity.node_id
            ):
                # A fixed account remains queued while its owner is unavailable;
                # it is never silently moved to another node.
                continue
            if not self._command_can_run(account, command):
                continue
            if command.expected_revision != account.revision:
                command.status = BrowserCommandStatus.FAILED.value
                command.completed_at = now
                command.error_code = BrowserAccountErrorCode.STALE_GENERATION.value
                continue
            if capacity.occupied_slots >= capacity.slot_limit:
                break

            current_lease = await db.scalar(
                select(BrowserAccountLease)
                .where(
                    BrowserAccountLease.workspace_id == command.workspace_id,
                    BrowserAccountLease.account_id == command.account_id,
                    BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if current_lease is not None:
                if _utc(current_lease.expires_at) <= now:
                    await self._quarantine_lease(current_lease, now)
                    continue
                # Commands for the same account/session are serialized under
                # one durable lease.  A different node/owner must wait rather
                # than stealing the account; a same-owner follow-up command
                # reuses the epoch and capacity reservation.
                if (
                    current_lease.node_id != identity.node_id
                    or current_lease.node_boot_id != identity.boot_id
                    or current_lease.owner_id != identity.owner_id
                ):
                    continue
                session = None
                if command.session_id is not None:
                    session = await db.scalar(
                        select(BrowserLoginSession).where(
                            BrowserLoginSession.workspace_id == command.workspace_id,
                            BrowserLoginSession.account_id == command.account_id,
                            BrowserLoginSession.id == command.session_id,
                        )
                    )
                if session is not None and session.lease_id not in (None, current_lease.lease_id):
                    continue
                current_lease.renewed_at = now
                current_lease.expires_at = now + self.lease_ttl
                command.node_id = identity.node_id
                command.epoch = current_lease.epoch
                command.status = BrowserCommandStatus.CLAIMED.value
                command.claimed_at = now
                claims.append(
                    NodeClaimV1(
                        command_id=command.id,
                        session_id=command.session_id,
                        node_id=identity.node_id,
                        boot_id=identity.boot_id,
                        epoch=current_lease.epoch,
                        expected_revision=command.expected_revision,
                        claimed_at=now,
                        expires_at=current_lease.expires_at,
                    )
                )
                continue
            if boot.max_epoch < 0:
                raise SchedulerConflict("node epoch watermark is invalid")
            boot.max_epoch += 1
            epoch = boot.max_epoch
            lease_id = str(uuid4())
            expires_at = now + self.lease_ttl
            lease = BrowserAccountLease(
                workspace_id=command.workspace_id,
                account_id=command.account_id,
                node_id=identity.node_id,
                node_boot_id=identity.boot_id,
                lease_id=lease_id,
                epoch=epoch,
                owner_id=identity.owner_id,
                status=BrowserLeaseStatus.ACTIVE.value,
                acquired_at=now,
                renewed_at=now,
                expires_at=expires_at,
            )
            db.add(lease)
            command.node_id = identity.node_id
            command.epoch = epoch
            command.status = BrowserCommandStatus.CLAIMED.value
            command.claimed_at = now
            capacity.occupied_slots += 1
            claims.append(
                NodeClaimV1(
                    command_id=command.id,
                    session_id=command.session_id,
                    node_id=identity.node_id,
                    boot_id=identity.boot_id,
                    epoch=epoch,
                    expected_revision=command.expected_revision,
                    claimed_at=now,
                    expires_at=expires_at,
                )
            )
        await db.flush()
        return claims

    async def claim(
        self,
        node_identity: NodeIdentity | Mapping[str, Any] | Any,
        limit: int = MAX_BATCH_SIZE,
        *,
        db: AsyncSession | None = None,
    ) -> list[NodeClaimV1]:
        identity = _coerce_node_identity(node_identity)
        if db is not None:
            return await self._claim_in_session(db, identity, limit)
        return await self._owned(lambda session: self._claim_in_session(session, identity, limit))

    async def _renew_in_session(
        self,
        db: AsyncSession,
        identity: NodeIdentity,
        claim: NodeClaimV1,
    ) -> NodeClaimV1:
        now = _utc(self.clock())
        if claim.node_id != identity.node_id or claim.boot_id != identity.boot_id:
            raise LeaseLost("claim node identity mismatch")
        lease = await db.scalar(
            select(BrowserAccountLease)
            .where(BrowserAccountLease.lease_id == claim.command_id)
            .with_for_update()
        )
        # The durable lease id is not present in NodeClaimV1.  Commands use the
        # command id as the lookup key below; this fallback keeps renew usable
        # for contracts that identify the lease by command id in the outer RPC.
        if lease is None:
            lease = await db.scalar(
                select(BrowserAccountLease)
                .join(
                    BrowserDurableCommand,
                    and_(
                        BrowserDurableCommand.workspace_id == BrowserAccountLease.workspace_id,
                        BrowserDurableCommand.account_id == BrowserAccountLease.account_id,
                        BrowserDurableCommand.epoch == BrowserAccountLease.epoch,
                        BrowserDurableCommand.node_id == BrowserAccountLease.node_id,
                    ),
                )
                .where(
                    BrowserDurableCommand.id == claim.command_id,
                    BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
                )
                .with_for_update()
            )
        if lease is None:
            raise LeaseLost("claim lease does not exist")
        if (
            lease.node_id != identity.node_id
            or lease.node_boot_id != identity.boot_id
            or lease.owner_id != identity.owner_id
            or lease.epoch != claim.epoch
            or lease.status != BrowserLeaseStatus.ACTIVE.value
        ):
            raise LeaseLost("claim lease fencing mismatch")
        if _utc(lease.expires_at) <= now:
            await self._quarantine_lease(lease, now)
            raise LeaseLost("claim lease expired")
        node = await db.scalar(select(EdgeNode).where(EdgeNode.id == identity.node_id))
        if node is None or node.boot_id != identity.boot_id or node.quarantined:
            await self._quarantine_lease(lease, now)
            raise NodeUnavailable("node boot is no longer current")
        command = await db.scalar(
            select(BrowserDurableCommand)
            .where(BrowserDurableCommand.id == claim.command_id)
            .with_for_update()
        )
        if command is None or command.session_id != claim.session_id:
            raise LeaseLost("claim command mismatch")
        if command.status not in {
            BrowserCommandStatus.CLAIMED.value,
            BrowserCommandStatus.RUNNING.value,
        }:
            raise LeaseLost("claim command is no longer active")
        command.status = BrowserCommandStatus.RUNNING.value
        lease.renewed_at = now
        lease.expires_at = now + self.lease_ttl
        await db.flush()
        return NodeClaimV1(
            command_id=claim.command_id,
            session_id=claim.session_id,
            node_id=claim.node_id,
            boot_id=claim.boot_id,
            epoch=claim.epoch,
            expected_revision=claim.expected_revision,
            claimed_at=claim.claimed_at,
            expires_at=lease.expires_at,
        )

    async def renew(
        self,
        node_identity: NodeIdentity | Mapping[str, Any] | Any,
        claim: NodeClaimV1 | Mapping[str, Any],
        *,
        db: AsyncSession | None = None,
    ) -> NodeClaimV1:
        identity = _coerce_node_identity(node_identity)
        parsed_claim = claim if isinstance(claim, NodeClaimV1) else NodeClaimV1.from_wire(claim)
        if db is not None:
            return await self._renew_in_session(db, identity, parsed_claim)
        return await self._owned(
            lambda session: self._renew_in_session(session, identity, parsed_claim)
        )

    async def _release_capacity(
        self,
        db: AsyncSession,
        *,
        node_id: str,
        boot_id: str,
    ) -> None:
        capacity = await db.scalar(
            select(EdgeNodeCapacity)
            .where(
                EdgeNodeCapacity.node_id == node_id,
                EdgeNodeCapacity.boot_id == boot_id,
            )
            .with_for_update()
        )
        if capacity is not None:
            capacity.occupied_slots = max(0, capacity.occupied_slots - 1)

    async def _commit_result_in_session(
        self,
        db: AsyncSession,
        identity: NodeIdentity,
        claim: NodeClaimV1,
        result: NodeResultV1,
    ) -> NodeResultV1:
        now = _utc(self.clock())
        if (
            result.command_id != claim.command_id
            or result.session_id != claim.session_id
            or result.node_id != claim.node_id
            or result.boot_id != claim.boot_id
            or result.epoch != claim.epoch
            or result.expected_revision != claim.expected_revision
        ):
            raise ResultConflict("result does not match claim fencing fields")
        if result.node_id != identity.node_id or result.boot_id != identity.boot_id:
            raise ResultConflict("result node identity mismatch")
        result_wire = _result_wire(result)
        command = await db.scalar(
            select(BrowserDurableCommand)
            .where(BrowserDurableCommand.id == claim.command_id)
            .with_for_update()
        )
        if command is None:
            raise ResultConflict("result command does not exist")
        if command.status in {
            BrowserCommandStatus.SUCCEEDED.value,
            BrowserCommandStatus.FAILED.value,
            BrowserCommandStatus.EXPIRED.value,
            BrowserCommandStatus.CANCELLED.value,
        }:
            if command.result == result_wire:
                return result
            raise ResultConflict("command already has a different result")
        if (
            command.node_id != claim.node_id
            or command.epoch != claim.epoch
            or command.session_id != claim.session_id
            or command.expected_revision != claim.expected_revision
            or command.status not in {
                BrowserCommandStatus.CLAIMED.value,
                BrowserCommandStatus.RUNNING.value,
            }
        ):
            raise ResultConflict("result command fencing mismatch")

        lease = await db.scalar(
            select(BrowserAccountLease)
            .where(
                BrowserAccountLease.workspace_id == command.workspace_id,
                BrowserAccountLease.account_id == command.account_id,
                BrowserAccountLease.node_id == claim.node_id,
                BrowserAccountLease.node_boot_id == claim.boot_id,
                BrowserAccountLease.epoch == claim.epoch,
            )
            .with_for_update()
        )
        if lease is None or lease.owner_id != identity.owner_id:
            raise LeaseLost("result lease does not match node owner")
        if lease.status != BrowserLeaseStatus.ACTIVE.value:
            raise LeaseLost("result lease is not active")
        if _utc(lease.expires_at) <= now:
            await self._quarantine_lease(lease, now)
            command.status = BrowserCommandStatus.FAILED.value
            command.error_code = BrowserAccountErrorCode.LEASE_LOST.value
            command.completed_at = now
            await db.flush()
            raise LeaseLost("result arrived after lease expiry")

        account = await db.scalar(
            select(BrowserAccount)
            .where(
                BrowserAccount.workspace_id == command.workspace_id,
                BrowserAccount.id == command.account_id,
            )
            .with_for_update()
        )
        if account is None or account.revision != claim.expected_revision:
            raise ResultConflict("result account revision is stale")

        command.status = _terminal_status(result.status).value
        command.result = result_wire
        command.error_code = result.error_code.value if result.error_code else None
        command.completed_at = now
        lease.renewed_at = now

        # Keep the account lease for the session's next command.  Release the
        # capacity only after a stop/isolation result proves the browser writer
        # is no longer active.  A blocked result remains quarantined.
        runtime_stopped = result.evidence.runtime_status == "stopped"
        isolation_proven = bool(result.isolation_evidence_ref or result.evidence.isolation_evidence_ref)
        must_stop = command.kind in {
            BrowserCommandKind.STOP_AND_SAVE.value,
            BrowserCommandKind.CLOSE_SESSION.value,
            BrowserCommandKind.ISOLATE.value,
            BrowserCommandKind.MIGRATE.value,
        }
        if result.status == "blocked" or (must_stop and not (runtime_stopped or isolation_proven)):
            await self._quarantine_lease(lease, now)
        elif runtime_stopped or isolation_proven:
            lease.status = BrowserLeaseStatus.RELEASED.value
            lease.released_at = now
            await self._release_capacity(db, node_id=lease.node_id, boot_id=lease.node_boot_id)
        if self.state_transition is not None:
            transition = self.state_transition(db, command, result)
            if inspect.isawaitable(transition):
                await transition
        await db.flush()
        return result

    async def commit_result(
        self,
        node_identity: NodeIdentity | Mapping[str, Any] | Any,
        claim: NodeClaimV1 | Mapping[str, Any],
        result: NodeResultV1 | Mapping[str, Any],
        *,
        db: AsyncSession | None = None,
    ) -> NodeResultV1:
        identity = _coerce_node_identity(node_identity)
        parsed_claim = claim if isinstance(claim, NodeClaimV1) else NodeClaimV1.from_wire(claim)
        try:
            parsed_result = result if isinstance(result, NodeResultV1) else NodeResultV1.from_wire(result)
        except Exception as exc:
            raise SchedulerValidationError("invalid node result contract") from exc
        if db is not None:
            return await self._commit_result_in_session(db, identity, parsed_claim, parsed_result)
        return await self._owned(
            lambda session: self._commit_result_in_session(
                session, identity, parsed_claim, parsed_result
            )
        )

    async def ack(
        self,
        node_identity: NodeIdentity | Mapping[str, Any] | Any,
        claim: NodeClaimV1 | Mapping[str, Any],
        result: NodeResultV1 | Mapping[str, Any],
        *,
        db: AsyncSession | None = None,
    ) -> NodeResultV1:
        """C1's ack name; result commit remains the single implementation."""

        return await self.commit_result(node_identity, claim, result, db=db)

    async def _recover_in_session(self, db: AsyncSession, batch_limit: int) -> RecoveryStats:
        if not isinstance(batch_limit, int) or isinstance(batch_limit, bool) or batch_limit < 1 or batch_limit > MAX_BATCH_SIZE:
            raise SchedulerValidationError("recovery batch_limit must be between 1 and 100")
        now = _utc(self.clock())
        expired = list(
            (
                await db.scalars(
                    select(BrowserDurableCommand)
                    .where(
                        BrowserDurableCommand.status == BrowserCommandStatus.QUEUED.value,
                        BrowserDurableCommand.expires_at <= now,
                    )
                    .order_by(BrowserDurableCommand.expires_at, BrowserDurableCommand.id)
                    .limit(batch_limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for command in expired:
            await self._expire_command(command, now)

        remaining = max(0, batch_limit - len(expired))
        quarantined = 0
        if remaining:
            leases = list(
                (
                    await db.scalars(
                        select(BrowserAccountLease)
                        .where(
                            BrowserAccountLease.status == BrowserLeaseStatus.ACTIVE.value,
                            BrowserAccountLease.expires_at <= now,
                        )
                        .order_by(BrowserAccountLease.expires_at, BrowserAccountLease.id)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for lease in leases:
                await self._quarantine_lease(lease, now)
                in_flight = list(
                    (
                        await db.scalars(
                            select(BrowserDurableCommand)
                            .where(
                                BrowserDurableCommand.workspace_id == lease.workspace_id,
                                BrowserDurableCommand.account_id == lease.account_id,
                                BrowserDurableCommand.node_id == lease.node_id,
                                BrowserDurableCommand.epoch == lease.epoch,
                                BrowserDurableCommand.status.in_(
                                    [
                                        BrowserCommandStatus.CLAIMED.value,
                                        BrowserCommandStatus.RUNNING.value,
                                    ]
                                ),
                            )
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                for command in in_flight:
                    command.status = BrowserCommandStatus.FAILED.value
                    command.error_code = BrowserAccountErrorCode.LEASE_LOST.value
                    command.completed_at = now
                quarantined += 1
        await db.flush()
        return RecoveryStats(expired_commands=len(expired), quarantined_leases=quarantined)

    async def recover(
        self,
        batch_limit: int = MAX_BATCH_SIZE,
        *,
        db: AsyncSession | None = None,
    ) -> RecoveryStats:
        if db is not None:
            return await self._recover_in_session(db, batch_limit)
        return await self._owned(lambda session: self._recover_in_session(session, batch_limit))

    async def expire(
        self,
        batch_limit: int = MAX_BATCH_SIZE,
        *,
        db: AsyncSession | None = None,
    ) -> RecoveryStats:
        """C1's expire name; expired writers are quarantined, never released."""

        return await self.recover(db=db, batch_limit=batch_limit)


_default_scheduler = BrowserAccountScheduler()


async def enqueue(db: AsyncSession, command: DurableCommandV1 | Mapping[str, Any]) -> DurableCommandV1:
    return await _default_scheduler.enqueue(db, command)


async def claim(
    node_identity: NodeIdentity | Mapping[str, Any] | Any,
    limit: int = MAX_BATCH_SIZE,
    *,
    db: AsyncSession | None = None,
) -> list[NodeClaimV1]:
    return await _default_scheduler.claim(node_identity, limit, db=db)


async def renew(
    node_identity: NodeIdentity | Mapping[str, Any] | Any,
    claim_value: NodeClaimV1 | Mapping[str, Any],
    *,
    db: AsyncSession | None = None,
) -> NodeClaimV1:
    return await _default_scheduler.renew(node_identity, claim_value, db=db)


async def ack(
    node_identity: NodeIdentity | Mapping[str, Any] | Any,
    claim_value: NodeClaimV1 | Mapping[str, Any],
    result: NodeResultV1 | Mapping[str, Any],
    *,
    db: AsyncSession | None = None,
) -> NodeResultV1:
    return await _default_scheduler.ack(node_identity, claim_value, result, db=db)


async def commit_result(
    node_identity: NodeIdentity | Mapping[str, Any] | Any,
    claim_value: NodeClaimV1 | Mapping[str, Any],
    result: NodeResultV1 | Mapping[str, Any],
    *,
    db: AsyncSession | None = None,
) -> NodeResultV1:
    return await _default_scheduler.commit_result(node_identity, claim_value, result, db=db)


async def recover(batch_limit: int = MAX_BATCH_SIZE, *, db: AsyncSession | None = None) -> RecoveryStats:
    return await _default_scheduler.recover(db=db, batch_limit=batch_limit)


async def expire(batch_limit: int = MAX_BATCH_SIZE, *, db: AsyncSession | None = None) -> RecoveryStats:
    return await _default_scheduler.expire(db=db, batch_limit=batch_limit)


__all__ = [
    "BrowserAccountScheduler",
    "BrowserAccountSchedulerError",
    "LeaseLost",
    "NodeIdentity",
    "NodeUnavailable",
    "RecoveryStats",
    "ResultConflict",
    "SchedulerConflict",
    "SchedulerValidationError",
    "ack",
    "claim",
    "commit_result",
    "enqueue",
    "expire",
    "recover",
    "renew",
]
