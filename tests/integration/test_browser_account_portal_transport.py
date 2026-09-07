"""ASGI portal relay coverage; the fake transport isolates only the remote node socket."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from backend.api.v1 import browser_accounts
from backend.main import app
from backend.models.browser import (
    BrowserAccount, BrowserAccountLease, BrowserLoginSession, BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.browser_portal import BrowserPortalOwner
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.schemas.browser_account import (
    AccountRef,
    NodeIdentityV1,
    PortalClipV1,
    PortalControlMessageV1,
    PortalOuterBindingV1,
    PortalOwnerRouteV1,
    PortalPixelFrameV1,
    PortalRegionFocusV1,
    PortalSensitivePayloadV1,
    PortalTransientV1,
    PortalWireFrameV1,
    PortalWireLayoutV1,
    SensitiveSessionBindingV1,
    SessionTargetV1,
)
from backend.services.browser_portal_contract import (
    decode_portal_wire_frame,
    encode_portal_wire_frame,
    portal_wire_metadata_length,
)

_WORKSPACE = "portal-transport-workspace"
_ACCOUNT = "portal-transport-account"
_SESSION = "portal-transport-session"
_SUBJECT = "portal-transport-subject"
_OWNER_TOKEN = "portal-transport-owner-token"


def _wire(frame: PortalWireFrameV1, payload_bytes: int) -> PortalWireFrameV1:
    return frame.model_copy(
        update={
            "layout": PortalWireLayoutV1(
                metadata_bytes=portal_wire_metadata_length(frame), payload_bytes=payload_bytes
            )
        }
    )


class _NodeTransportDouble:
    """Network-edge double; ASGI relay, DB authorization, and wire codec stay real."""

    def __init__(self, pixel: PortalWireFrameV1) -> None:
        self._pixel = pixel
        self._emitted = False
        self.sent: list[PortalWireFrameV1] = []
        self.closed_reason: str | None = None

    async def receive(self, *, timeout: float | None = None) -> PortalWireFrameV1 | None:
        if not self._emitted:
            self._emitted = True
            return self._pixel
        await asyncio.sleep(timeout or 0.01)
        return None

    async def send(self, frame: PortalWireFrameV1) -> None:
        self.sent.append(frame)

    async def close(self, *, reason: str) -> None:
        self.closed_reason = reason


@pytest.mark.asyncio
async def test_asgi_portal_websocket_relays_canonical_pixels_and_input_with_db_owner_facts(
    db_engine, db_session: AsyncSession, monkeypatch
) -> None:
    """The center ASGI WebSocket accepts only the DB-backed owner and canonical wire frames."""
    now = datetime.now(UTC)
    db_session.add_all(
        (
            User(id="portal-transport-user", subject=_SUBJECT),
            Workspace(id=_WORKSPACE, name="Portal transport", slug="portal-transport"),
            WorkspaceMembership(
                id="portal-transport-membership",
                workspace_id=_WORKSPACE,
                user_id="portal-transport-user",
                role=WorkspaceRole.OPERATOR,
            ),
            EdgeNode(
                id="portal-transport-node",
                url="https://node.test",
                boot_id="boot",
                account_capable=True,
                status="online",
            ),
            BrowserRuntimeBundle(id="bundle", name="fixture", version="1", manifest={}),
            BrowserAccount(
                id=_ACCOUNT,
                workspace_id=_WORKSPACE,
                site="fixture.test",
                label="Fixture",
                node_id="portal-transport-node",
                runtime_bundle_id="bundle",
                runtime_bundle_version="1",
            ),
            BrowserLoginSession(
                id=_SESSION,
                workspace_id=_WORKSPACE,
                account_id=_ACCOUNT,
                node_id="portal-transport-node",
                node_boot_id="boot",
                lease_id="portal-transport-lease",
                epoch=7,
                command_id="command",
                tab_id="tab",
                frame_id="frame",
                document_id="document",
                origin="https://fixture.test",
                view_generation=2,
                revision=3,
                purpose="login",
                status="presenting",
                expires_at=now + timedelta(minutes=5),
            ),
            BrowserAccountLease(
                workspace_id=_WORKSPACE,
                account_id=_ACCOUNT,
                node_id="portal-transport-node",
                node_boot_id="boot",
                lease_id="portal-transport-lease",
                epoch=7,
                owner_id="portal-transport-owner",
                acquired_at=now,
                expires_at=now + timedelta(minutes=1),
            ),
            BrowserPortalOwner(
                owner_digest=hashlib.sha256(_OWNER_TOKEN.encode()).hexdigest(),
                subject=_SUBJECT,
                workspace_id=_WORKSPACE,
                account_id=_ACCOUNT,
                session_id=_SESSION,
                ticket_id="portal-transport-ticket",
                session_revision=3,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
                hard_expires_at=now + timedelta(minutes=10),
                active=True,
            ),
        )
    )
    await db_session.commit()
    endpoint, actual_envelope, revision = (
        await browser_accounts.browser_account_service.get_portal_session_envelope(
            db_session, _WORKSPACE, _ACCOUNT, _SESSION
        )
    )
    assert endpoint == "https://node.test"
    assert actual_envelope.node_id == "portal-transport-node"
    assert revision == 3

    target = SessionTargetV1(tab_id="tab", frame_id="frame", document_id="document", origin="https://fixture.test")
    binding = SensitiveSessionBindingV1(
        account_ref=AccountRef(workspace_id=_WORKSPACE, account_id=_ACCOUNT),
        session_id=_SESSION,
        epoch=7,
        target=target,
        view_generation=2,
        record_session_id="record-session",
    )
    clip = PortalClipV1(x=0, y=0, width=8, height=8)
    route = PortalOwnerRouteV1(
        binding=binding,
        node_identity=NodeIdentityV1(node_id="portal-transport-node", boot_id="boot"),
        owner_endpoint="https://node.test",
        tunnel_handle="portal-tunnel",
        tunnel_auth_digest="a" * 64,
        region_focus=PortalRegionFocusV1(
            target=target, view_generation=2, region_kind="form", approved_regions=[clip], focused_field_ref="password"
        ),
        session_revision=3,
        route_expires_at=now + timedelta(minutes=1),
        max_frame_bytes=4_000_000,
        max_input_bytes=4096,
    )
    raw_pixels = b"\x89PNG\r\n\x1a\n"
    pixel = PortalPixelFrameV1(
        workspace_id=_WORKSPACE, account_id=_ACCOUNT, session_id=_SESSION, epoch=7, target=target,
        view_generation=2, sequence=1, region_kind="form", mime_type="image/png",
        expires_at=now + timedelta(seconds=30), clip=clip, byte_length=len(raw_pixels), frame_bytes=raw_pixels,
    )
    pixel_wire = _wire(PortalWireFrameV1(
        sequence=1, encoding="pixel-binary", content_type="application/octet-stream", mime_type="image/png",
        byte_length=len(raw_pixels), layout=PortalWireLayoutV1(metadata_bytes=1, payload_bytes=len(raw_pixels)),
        transient=PortalTransientV1(binding=PortalOuterBindingV1(
            workspace_id=_WORKSPACE, account_id=_ACCOUNT, session_id=_SESSION, epoch=7, target=target, view_generation=2
        ), pixel=pixel),
    ), len(raw_pixels))
    transport = _NodeTransportDouble(pixel_wire)
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async def prepare_route(*_args, **_kwargs):
        return route

    async def open_route(*_args, **_kwargs):
        return transport

    monkeypatch.setattr(browser_accounts, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr("backend.ws_agent_manager.prepare_portal_route", prepare_route)
    monkeypatch.setattr("backend.ws_agent_manager.open_portal_route", open_route)

    websocket_path = f"/api/v1/workspaces/{_WORKSPACE}/browser-accounts/{_ACCOUNT}/login-sessions/{_SESSION}/portal"
    with TestClient(app) as asgi:
        with asgi.websocket_connect(websocket_path, headers={"origin": "http://testserver", "cookie": f"qrac2_portal={_OWNER_TOKEN}"}) as socket:
            observed = decode_portal_wire_frame(socket.receive_bytes())
            assert observed.encoding == "pixel-binary"
            assert observed.transient.pixel is not None
            assert observed.transient.pixel.frame_bytes.get_secret_value() == raw_pixels

            secret = "transient-password"
            control = PortalControlMessageV1(
                workspace_id=_WORKSPACE, account_id=_ACCOUNT, session_id=_SESSION, epoch=7, target=target,
                view_generation=2, sequence=1, kind="field_input", field_ref="password",
                sensitive_payload=PortalSensitivePayloadV1(value=SecretStr(secret)),
            )
            control_wire = _wire(PortalWireFrameV1(
                sequence=1, encoding="control-json", content_type="application/json", mime_type="application/json",
                layout=PortalWireLayoutV1(metadata_bytes=1, payload_bytes=len(secret)),
                transient=PortalTransientV1(binding=PortalOuterBindingV1(
                    workspace_id=_WORKSPACE, account_id=_ACCOUNT, session_id=_SESSION, epoch=7, target=target, view_generation=2
                ), control=control),
            ), len(secret))
            socket.send_bytes(encode_portal_wire_frame(control_wire))
            for _ in range(50):
                if transport.sent:
                    break
                await asyncio.sleep(0.01)

    assert len(transport.sent) == 1
    assert transport.sent[0].transient.control is not None
    assert transport.sent[0].transient.control.sensitive_payload.value.get_secret_value() == "transient-password"
    assert transport.closed_reason == "portal_closed"
