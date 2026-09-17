from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from backend.main import app
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserLoginSession,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services.browser_desktop_service import (
    BrowserDesktopGrantStore,
    authorize_browser_desktop,
    grant_matches_authorization,
)
from backend.services.browser_native_window import (
    NativeWindowOpenResult,
    NativeWindowSupport,
    native_window_manager,
)

_WORKSPACE_ID = "desktop-workspace"
_ACCOUNT_ID = "desktop-account"
_SESSION_ID = "desktop-session"
_SUBJECT = "desktop-subject"


async def _seed_desktop_authorization(db_session, *, role=WorkspaceRole.OPERATOR):
    now = datetime.now(UTC)
    user = User(id="desktop-user", subject=_SUBJECT)
    workspace = Workspace(id=_WORKSPACE_ID, name="Desktop", slug="desktop")
    membership = WorkspaceMembership(
        id="desktop-membership",
        workspace_id=_WORKSPACE_ID,
        user_id=user.id,
        role=role,
    )
    node = EdgeNode(
        id="desktop-node",
        url="https://desktop-node.test",
        status="online",
        boot_id="desktop-boot",
        account_capable=True,
        quarantined=False,
    )
    bundle = BrowserRuntimeBundle(
        id="desktop-bundle",
        name="desktop-bundle",
        version="1",
        manifest={},
    )
    account = BrowserAccount(
        id=_ACCOUNT_ID,
        workspace_id=_WORKSPACE_ID,
        site="example.test",
        label="Desktop account",
        node_id=node.id,
        runtime_bundle_id=bundle.id,
        runtime_bundle_version=bundle.version,
        status="opening",
    )
    session = BrowserLoginSession(
        id=_SESSION_ID,
        workspace_id=_WORKSPACE_ID,
        account_id=account.id,
        node_id=node.id,
        node_boot_id=node.boot_id,
        lease_id="desktop-lease",
        epoch=7,
        profile_id="desktop-profile",
        profile_state="uncommitted",
        purpose="browser",
        command_id="desktop-command",
        status="opening",
        expires_at=now + timedelta(minutes=30),
    )
    lease = BrowserAccountLease(
        id="desktop-lease-row",
        workspace_id=_WORKSPACE_ID,
        account_id=account.id,
        node_id=node.id,
        node_boot_id=node.boot_id,
        lease_id=session.lease_id,
        epoch=session.epoch,
        owner_id="desktop-owner",
        status="active",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    db_session.add_all([user, workspace, membership, node, bundle, account, session, lease])
    await db_session.commit()
    return membership, account, session, lease


@pytest.mark.asyncio
async def test_desktop_authorization_rechecks_role_workspace_and_fenced_lease(db_session):
    membership, _account, session, lease = await _seed_desktop_authorization(db_session)
    authorization = await authorize_browser_desktop(
        db_session,
        _WORKSPACE_ID,
        _ACCOUNT_ID,
        _SESSION_ID,
        subject=_SUBJECT,
    )
    assert authorization is not None
    assert authorization.role == "operator"
    assert authorization.envelope.profile_id == "desktop-profile"

    store = BrowserDesktopGrantStore()
    _token, grant = store.issue(authorization, subject=_SUBJECT)
    assert grant_matches_authorization(
        grant,
        replace(authorization, session_revision=authorization.session_revision + 1),
    )
    assert (
        await authorize_browser_desktop(
            db_session,
            "another-workspace",
            _ACCOUNT_ID,
            _SESSION_ID,
            subject=_SUBJECT,
        )
        is None
    )

    membership.role = WorkspaceRole.VIEWER
    await db_session.commit()
    assert (
        await authorize_browser_desktop(
            db_session,
            _WORKSPACE_ID,
            _ACCOUNT_ID,
            _SESSION_ID,
            subject=_SUBJECT,
        )
        is None
    )

    membership.role = WorkspaceRole.OPERATOR
    lease.epoch += 1
    await db_session.commit()
    assert (
        await authorize_browser_desktop(
            db_session,
            _WORKSPACE_ID,
            _ACCOUNT_ID,
            _SESSION_ID,
            subject=_SUBJECT,
        )
        is None
    )


@pytest.mark.asyncio
async def test_desktop_grant_http_sets_session_scoped_secure_cookie_without_token(
    client, db_session
):
    await _seed_desktop_authorization(db_session)

    async def identity_override() -> RequestIdentity:
        return RequestIdentity(subject=_SUBJECT, auth_method="test")

    app.dependency_overrides[get_request_identity] = identity_override
    try:
        path = (
            f"/api/v1/workspaces/{_WORKSPACE_ID}/browser-accounts/{_ACCOUNT_ID}"
            f"/login-sessions/{_SESSION_ID}/browser-grant"
        )
        response = await client.post(path, headers={"Origin": "http://test"}, json={})
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        websocket_path = path.removesuffix("-grant")
        assert payload["websocket_path"] == websocket_path
        assert "token" not in response.text.lower()
        cookie = response.headers["set-cookie"]
        assert "opencli_browser_desktop=" in cookie
        assert f"Path={websocket_path}" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Secure" in cookie
    finally:
        app.dependency_overrides.pop(get_request_identity, None)


@pytest.mark.asyncio
async def test_native_window_support_is_static_operator_only_and_same_origin(
    client, db_session, monkeypatch
):
    await _seed_desktop_authorization(db_session)

    async def identity_override() -> RequestIdentity:
        return RequestIdentity(subject=_SUBJECT, auth_method="test")

    app.dependency_overrides[get_request_identity] = identity_override
    monkeypatch.setattr(
        native_window_manager,
        "support",
        lambda: NativeWindowSupport(True, "available"),
    )
    path = (
        f"/api/v1/workspaces/{_WORKSPACE_ID}/browser-accounts/native-window-support"
    )
    try:
        denied = await client.get(path, headers={"Origin": "https://attacker.test"})
        assert denied.status_code == 403
        response = await client.get(path, headers={"Origin": "http://test"})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "available": True,
            "message": "available",
        }
        # Browsers omit Origin on a same-origin GET.
        same_origin_get = await client.get(path)
        assert same_origin_get.status_code == 200
    finally:
        app.dependency_overrides.pop(get_request_identity, None)


@pytest.mark.asyncio
async def test_native_window_endpoint_opens_authorized_generation_without_secrets(
    client, db_session, monkeypatch
):
    await _seed_desktop_authorization(db_session)

    async def identity_override() -> RequestIdentity:
        return RequestIdentity(subject=_SUBJECT, auth_method="test")

    app.dependency_overrides[get_request_identity] = identity_override
    monkeypatch.setattr(
        native_window_manager,
        "support",
        lambda: NativeWindowSupport(True, "available"),
    )
    opened = AsyncMock(
        return_value=NativeWindowOpenResult(
            status="opened",
            session_id=_SESSION_ID,
            message="opened",
        )
    )
    monkeypatch.setattr(native_window_manager, "open", opened)
    path = (
        f"/api/v1/workspaces/{_WORKSPACE_ID}/browser-accounts/{_ACCOUNT_ID}"
        f"/login-sessions/{_SESSION_ID}/native-window"
    )
    try:
        denied = await client.post(path, headers={"Origin": "https://attacker.test"})
        assert denied.status_code == 403
        response = await client.post(path, headers={"Origin": "http://test"})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "status": "opened",
            "session_id": _SESSION_ID,
            "message": "opened",
        }
        assert "password" not in response.text.lower()
        assert "127.0.0.1" not in response.text
        opened.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(get_request_identity, None)


@pytest.mark.asyncio
async def test_native_window_support_rejects_viewer_role(client, db_session, monkeypatch):
    await _seed_desktop_authorization(db_session, role=WorkspaceRole.VIEWER)

    async def identity_override() -> RequestIdentity:
        return RequestIdentity(subject=_SUBJECT, auth_method="test")

    app.dependency_overrides[get_request_identity] = identity_override
    monkeypatch.setattr(
        native_window_manager,
        "support",
        lambda: NativeWindowSupport(True, "available"),
    )
    try:
        response = await client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/browser-accounts/native-window-support",
            headers={"Origin": "http://test"},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_request_identity, None)
