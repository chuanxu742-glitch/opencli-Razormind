"""Short-lived browser desktop grants and fresh DB authorization snapshots."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.browser import BrowserAccount, BrowserAccountLease, BrowserLoginSession
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership
from backend.schemas.browser_account import SessionEnvelopeV1
from backend.services.browser_account_service import session_envelope

DESKTOP_COOKIE_NAME = "opencli_browser_desktop"
DESKTOP_GRANT_TTL = timedelta(minutes=10)
DESKTOP_HARD_TTL = timedelta(minutes=30)
_MAX_GRANTS = 256


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class BrowserDesktopGrant:
    digest: str
    subject: str
    workspace_id: str
    account_id: str
    session_id: str
    profile_id: str
    node_id: str
    boot_id: str
    lease_id: str
    epoch: int
    issued_at: datetime
    expires_at: datetime
    hard_expires_at: datetime


@dataclass(frozen=True)
class BrowserDesktopAuthorization:
    endpoint: str
    envelope: SessionEnvelopeV1
    role: str
    session_revision: int
    session_expires_at: datetime


class BrowserDesktopGrantStore:
    """Process-local digest-only grants; restart and eviction fail closed."""

    def __init__(self) -> None:
        self._grants: dict[str, BrowserDesktopGrant] = {}

    def issue(
        self,
        authorization: BrowserDesktopAuthorization,
        *,
        subject: str,
        now: datetime | None = None,
    ) -> tuple[str, BrowserDesktopGrant]:
        current = now or datetime.now(UTC)
        envelope = authorization.envelope
        if envelope.profile_id is None:
            raise ValueError("desktop session has no runtime profile identity")
        hard_expires_at = min(
            _utc(authorization.session_expires_at), current + DESKTOP_HARD_TTL
        )
        if hard_expires_at <= current:
            raise ValueError("desktop session lease has expired")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        grant = BrowserDesktopGrant(
            digest=digest,
            subject=subject,
            workspace_id=envelope.workspace_id,
            account_id=envelope.account_id,
            session_id=envelope.session_id,
            profile_id=envelope.profile_id,
            node_id=envelope.node_id,
            boot_id=envelope.node_boot_id,
            lease_id=envelope.lease_id,
            epoch=envelope.epoch,
            issued_at=current,
            expires_at=min(current + DESKTOP_GRANT_TTL, hard_expires_at),
            hard_expires_at=hard_expires_at,
        )
        self._purge(current)
        while len(self._grants) >= _MAX_GRANTS:
            self._grants.pop(next(iter(self._grants)))
        self._grants[digest] = grant
        return token, grant

    def resolve(self, token: str, *, now: datetime | None = None) -> BrowserDesktopGrant | None:
        current = now or datetime.now(UTC)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        grant = self._grants.get(digest)
        if grant is None or current >= grant.expires_at or current >= grant.hard_expires_at:
            self._grants.pop(digest, None)
            return None
        return grant

    def revoke(self, digest: str) -> None:
        self._grants.pop(digest, None)

    def _purge(self, now: datetime) -> None:
        for digest, grant in tuple(self._grants.items()):
            if now >= grant.expires_at or now >= grant.hard_expires_at:
                self._grants.pop(digest, None)


grant_store = BrowserDesktopGrantStore()


async def authorize_browser_desktop(
    db: AsyncSession,
    workspace_id: str,
    account_id: str,
    session_id: str,
    *,
    subject: str,
) -> BrowserDesktopAuthorization | None:
    """Read the exact member/session/lease/node tuple without portal-owner joins."""

    now = datetime.now(UTC)
    result = await db.execute(
        select(
            WorkspaceMembership,
            User,
            Workspace,
            BrowserAccount,
            BrowserLoginSession,
            BrowserAccountLease,
            EdgeNode,
        )
        .join(User, User.id == WorkspaceMembership.user_id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .join(
            BrowserAccount,
            and_(
                BrowserAccount.workspace_id == Workspace.id,
                BrowserAccount.id == account_id,
            ),
        )
        .join(
            BrowserLoginSession,
            and_(
                BrowserLoginSession.workspace_id == Workspace.id,
                BrowserLoginSession.account_id == BrowserAccount.id,
                BrowserLoginSession.id == session_id,
            ),
        )
        .join(
            BrowserAccountLease,
            and_(
                BrowserAccountLease.lease_id == BrowserLoginSession.lease_id,
                BrowserAccountLease.workspace_id == Workspace.id,
                BrowserAccountLease.account_id == BrowserAccount.id,
            ),
        )
        .join(EdgeNode, EdgeNode.id == BrowserLoginSession.node_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            User.subject == subject,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    membership, user, workspace, account, session, lease, node = row
    if (
        user.disabled
        or not workspace.active
        or membership.role.value not in {"admin", "maintainer", "operator"}
        or account.paused
        or session.purpose != "browser"
        or session.status
        not in {"opening", "presenting", "refreshing", "verifying", "challenge", "unknown"}
        or session.expires_at is None
        or _utc(session.expires_at) <= now
        or not session.profile_id
        or not session.command_id
        or lease.status != "active"
        or _utc(lease.expires_at) <= now
        or lease.node_id != session.node_id
        or lease.node_boot_id != session.node_boot_id
        or lease.epoch != session.epoch
        or node.status != "online"
        or node.quarantined
        or not node.account_capable
        or node.boot_id != session.node_boot_id
        or account.node_id != session.node_id
    ):
        return None
    envelope = await session_envelope(account, session)
    envelope.lease_expires_at = min(_utc(lease.expires_at), _utc(session.expires_at))
    return BrowserDesktopAuthorization(
        endpoint=node.url.rstrip("/"),
        envelope=envelope,
        role=membership.role.value,
        session_revision=session.revision,
        session_expires_at=_utc(session.expires_at),
    )


def grant_matches_authorization(
    grant: BrowserDesktopGrant, authorization: BrowserDesktopAuthorization
) -> bool:
    envelope = authorization.envelope
    return (
        grant.workspace_id == envelope.workspace_id
        and grant.account_id == envelope.account_id
        and grant.session_id == envelope.session_id
        and grant.profile_id == envelope.profile_id
        and grant.node_id == envelope.node_id
        and grant.boot_id == envelope.node_boot_id
        and grant.lease_id == envelope.lease_id
        and grant.epoch == envelope.epoch
    )
