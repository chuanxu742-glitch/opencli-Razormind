from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TimestampMixin


class EdgeNode(TimestampMixin):
    """Remote agent node that has registered with the center.

    Tracks lifecycle (online / offline) and metadata for each edge agent.
    """

    __tablename__ = "edge_nodes"

    # Canonical URL the center uses to identify / reach this agent
    # (e.g. http://192.168.1.100:19823).  Acts as a unique logical key.
    url: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # "http" — center HTTP-POSTs to agent_url (LAN)
    # "ws"   — agent opened a reverse WS channel (NAT/firewall)
    protocol: Mapped[str] = mapped_column(String(10), nullable=False, default="http")
    # Chrome connection mode: "bridge" | "cdp" — how opencli connects to Chrome during collection
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="bridge")
    # Node startup/deployment type: "docker" | "shell"
    # Orthogonal to mode — both docker and shell nodes need Chrome (via bridge or cdp).
    node_type: Mapped[str] = mapped_column(String(20), nullable=False, default="docker")
    # "online" | "offline"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="offline")
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Detected outbound IP at last registration
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    # Agent-runtime types advertised at last WS register handshake (e.g. ["pi"]),
    # from backend.agent_runtimes.registry.available_runtimes(). NULL when the
    # node hasn't registered since this field was added, or registered over the
    # HTTP (non-WS) path, which doesn't carry runtime advertisement.
    runtimes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Per-runtime capability names measured by the edge adapter registry.
    runtime_capabilities: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Persistent node fencing identity and current supervised process generation.
    boot_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    credential_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capacity_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_capable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class EdgeNodeCapacity(TimestampMixin):
    """Expiring capacity fact; stale facts never create schedulable slots."""

    __tablename__ = "edge_node_capacities"
    __table_args__ = (
        UniqueConstraint("node_id", "boot_id", name="uq_edge_node_capacity_generation"),
        CheckConstraint("slot_limit >= 0", name="ck_edge_node_capacity_slot_limit"),
        CheckConstraint("occupied_slots >= 0", name="ck_edge_node_capacity_occupied_slots"),
        CheckConstraint("occupied_slots <= slot_limit", name="ck_edge_node_capacity_occupied_lte_limit"),
        CheckConstraint("disk_available >= 0", name="ck_edge_node_capacity_disk_available"),
    )

    node_id: Mapped[str] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    slot_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    occupied_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_available: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EdgeNodeBoot(TimestampMixin):
    """Durable boot generation and fencing watermark for one node."""

    __tablename__ = "edge_node_boots"
    __table_args__ = (
        UniqueConstraint("node_id", "boot_id", name="uq_edge_node_boot_generation"),
        CheckConstraint("max_epoch >= 0", name="ck_edge_node_boot_max_epoch"),
    )

    node_id: Mapped[str] = mapped_column(
        ForeignKey("edge_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    max_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EdgeNodeEvent(TimestampMixin):
    """Append-only event log for each edge node (registered / online / offline)."""

    __tablename__ = "edge_node_events"

    node_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("edge_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "registered" | "online" | "offline"
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    event_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
