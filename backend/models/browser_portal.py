from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TimestampMixin


class BrowserPortalTicket(TimestampMixin):
    """Digest-only, one-use portal ticket bound to one account session."""

    __tablename__ = "browser_portal_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_id", name="uq_browser_portal_tickets_ticket_id"),
        Index("ix_browser_portal_tickets_scope", "workspace_id", "account_id", "session_id"),
        CheckConstraint("session_revision >= 0", name="ck_browser_portal_tickets_session_revision"),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ticket_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("browser_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_login_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hard_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BrowserPortalOwner(TimestampMixin):
    """Short-lived digest-backed owner credential used by the portal WebSocket."""

    __tablename__ = "browser_portal_owners"
    __table_args__ = (
        UniqueConstraint("owner_digest", name="uq_browser_portal_owners_owner_digest"),
        Index("ix_browser_portal_owners_scope", "workspace_id", "account_id", "session_id"),
        CheckConstraint("session_revision >= 0", name="ck_browser_portal_owners_session_revision"),
    )

    owner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("browser_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("browser_login_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hard_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
