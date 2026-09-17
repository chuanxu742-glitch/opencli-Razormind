from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from backend.api.v1.project_source_bindings import _account_for_binding
from backend.main import app
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserDurableCommand,
    BrowserInstance,
    BrowserLoginSession,
)
from backend.models.browser_space import BrowserSpace
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.schemas.browser_account import DurableCommandV1
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import browser_account_service as service
from backend.services.browser_account_scheduler import BrowserAccountScheduler, SchedulerConflict

BASE = "/api/v1/workspaces/delete-workspace/browser-accounts"
NOW = datetime.now(UTC)


async def seed(db, role=WorkspaceRole.ADMIN):
    account = BrowserAccount(
        id="delete-account",
        workspace_id="delete-workspace",
        site="github.com",
        label="GitHub",
        revision=7,
        status="dormant",
        profile_id="retained-profile",
        profile_version=3,
    )
    db.add_all(
        [
            User(id="delete-user", subject="delete-subject"),
            Workspace(id="delete-workspace", name="Delete", slug="delete"),
            Workspace(id="foreign-workspace", name="Foreign", slug="foreign"),
            WorkspaceMembership(
                id="delete-member",
                workspace_id="delete-workspace",
                user_id="delete-user",
                role=role,
            ),
            WorkspaceMembership(
                id="foreign-member",
                workspace_id="foreign-workspace",
                user_id="delete-user",
                role=role,
            ),
            account,
            BrowserAccount(
                id="other-account",
                workspace_id="delete-workspace",
                site="xiaohongshu.com",
                label="保留",
                revision=4,
                status="dormant",
            ),
            EdgeNode(id="delete-node", label="Delete node", url="http://fixture.invalid"),
            BrowserInstance(id="delete-instance", endpoint="http://fixture.invalid"),
        ]
    )
    await db.commit()

    async def identity():
        return RequestIdentity(subject="delete-subject", auth_method="test")

    app.dependency_overrides[get_request_identity] = identity
    return account


def command(status="succeeded"):
    return BrowserDurableCommand(
        id="delete-command",
        workspace_id="delete-workspace",
        account_id="delete-account",
        kind="close_session",
        idempotency_scope="delete",
        idempotency_key="delete",
        epoch=0,
        expected_revision=7,
        available_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
        status=status,
        payload={"reason": "completed"},
        session_id="delete-session",
    )


def session(closed=True, status="closed"):
    return BrowserLoginSession(
        id="delete-session",
        workspace_id="delete-workspace",
        account_id="delete-account",
        status=status,
        closed_at=NOW if closed else None,
    )


@pytest.mark.asyncio
async def test_delete_tombstone_blocks_all_entrypoints_and_preserves_history(client, db_session):
    account = await seed(db_session)
    history = session()
    historical_command = command()
    space = BrowserSpace(
        id="delete-space",
        workspace_id="delete-workspace",
        browser_instance_id="delete-instance",
        account_id=account.id,
        owner_type="operator",
        owner_id="delete-user",
        status="closed",
    )
    db_session.add_all([history, historical_command, space])
    await db_session.commit()
    other_before = (await client.get(f"{BASE}/other-account")).json()["data"]
    response = await client.delete(f"{BASE}/{account.id}", headers={"If-Match": '"7"'})
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert not db_session.in_transaction()
    await db_session.refresh(account)
    assert (account.status, account.paused, account.status_reason_code, account.revision) == (
        "closed",
        True,
        "account_deleted",
        8,
    )
    assert (account.profile_id, account.profile_version) == ("retained-profile", 3)
    assert await db_session.get(BrowserLoginSession, history.id) is history
    assert history.closed_at is not None and history.status == "closed"
    assert (
        await db_session.get(BrowserDurableCommand, historical_command.id)
    ).status == "succeeded"
    assert (await db_session.get(BrowserSpace, space.id)).status == "closed"
    assert (await client.get(f"{BASE}/other-account")).json()["data"] == other_before
    assert [row["id"] for row in (await client.get(BASE)).json()["data"]["items"]] == [
        "other-account"
    ]
    for suffix in ("", "/login-sessions", "/login-sessions/delete-session", "/login-readiness"):
        assert (await client.get(f"{BASE}/{account.id}{suffix}")).status_code == 404
    assert (
        await client.patch(f"{BASE}/{account.id}", json={"paused": False, "expected_revision": 8})
    ).status_code == 404
    assert (
        await client.delete(f"{BASE}/{account.id}", headers={"If-Match": "8"})
    ).status_code == 404
    assert (
        await client.post(
            f"{BASE}/{account.id}/resume",
            json={
                "account_ref": {"workspace_id": account.workspace_id, "account_id": account.id},
                "expected_revision": 8,
                "operation": "resume",
            },
        )
    ).status_code == 404
    assert (
        await client.post(
            f"{BASE}/{account.id}/login-sessions",
            headers={"Idempotency-Key": "deleted"},
            json={"expected_revision": 8, "purpose": "login"},
        )
    ).status_code == 404
    with pytest.raises(service.BrowserAccountError) as error:
        await service._session_or_error(db_session, account.workspace_id, account.id, history.id)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        await _account_for_binding(db_session, account.workspace_id, account.id)
    assert error.value.status_code == 404
    scheduler = BrowserAccountScheduler()
    assert not scheduler._command_can_run(account, historical_command)
    request = DurableCommandV1(
        command_id=historical_command.id,
        workspace_id=account.workspace_id,
        account_id=account.id,
        kind="close_session",
        idempotency_scope="delete",
        idempotency_key="delete",
        epoch=0,
        expected_revision=7,
        available_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
        status="succeeded",
        payload={"reason": "completed"},
        session_id=history.id,
    )
    with pytest.raises(SchedulerConflict, match="deleted"):
        await scheduler.enqueue(db_session, request)
    with pytest.raises(SchedulerConflict, match="deleted"):
        await scheduler.enqueue(
            db_session,
            request.model_copy(update={"command_id": "new-command", "expected_revision": 8}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [None, "6", "wrong", "-1"])
async def test_delete_requires_current_revision(client, db_session, revision):
    account = await seed(db_session)
    response = await client.delete(
        f"{BASE}/{account.id}", headers={"If-Match": revision} if revision else {}
    )
    assert response.status_code == 409
    await db_session.refresh(account)
    assert account.revision == 7 and account.status_reason_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [WorkspaceRole.OPERATOR, WorkspaceRole.VIEWER])
async def test_delete_requires_management_permission(client, db_session, role):
    account = await seed(db_session, role)
    assert (
        await client.delete(f"{BASE}/{account.id}", headers={"If-Match": "7"})
    ).status_code == 403
    assert account.revision == 7 and account.status_reason_code is None


@pytest.mark.asyncio
async def test_delete_cannot_cross_workspace(client, db_session):
    account = await seed(db_session)
    path = f"{BASE}/{account.id}".replace("delete-workspace", "foreign-workspace")
    assert (await client.delete(path, headers={"If-Match": "7"})).status_code == 404
    assert account.revision == 7 and account.status_reason_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,state",
    [
        ("lease", "active"),
        ("lease", "quarantined"),
        ("session", "opening"),
        ("session", "error"),
        ("session", "saved"),
        ("session", "expired"),
        ("command", "queued"),
        ("command", "claimed"),
        ("command", "running"),
        ("space", "idle"),
        ("space", "running"),
        ("space", "error"),
    ],
)
async def test_delete_refuses_unfinished_resources_even_when_expired(
    client, db_session, kind, state
):
    account = await seed(db_session)
    if kind == "lease":
        blocker = BrowserAccountLease(
            id="delete-lease",
            workspace_id=account.workspace_id,
            account_id=account.id,
            node_id="delete-node",
            node_boot_id="boot",
            lease_id="lease",
            epoch=1,
            owner_id="owner",
            status=state,
            acquired_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(hours=1),
        )
    elif kind == "session":
        blocker = session(closed=False, status=state)
    elif kind == "command":
        db_session.add(session())
        blocker = command(state)
    else:
        blocker = BrowserSpace(
            id="delete-space",
            workspace_id=account.workspace_id,
            account_id=account.id,
            browser_instance_id="delete-instance",
            owner_type="operator",
            owner_id="delete-user",
            status=state,
        )
    db_session.add(blocker)
    await db_session.commit()
    response = await client.delete(f"{BASE}/{account.id}", headers={"If-Match": "7"})
    assert response.status_code == 409, response.text
    await db_session.refresh(account)
    assert (account.status, account.revision, account.paused, account.status_reason_code) == (
        "dormant",
        7,
        False,
        None,
    )
    await db_session.refresh(blocker)
    assert blocker.status == state


@pytest.mark.asyncio
async def test_delete_commit_failure_does_not_report_success(client, db_session, monkeypatch):
    await seed(db_session)

    async def fail_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        await client.delete(f"{BASE}/delete-account", headers={"If-Match": "7"})
    account = await db_session.get(BrowserAccount, "delete-account")
    assert (account.status, account.revision, account.status_reason_code) == ("dormant", 7, None)


@pytest.mark.asyncio
async def test_scheduler_reloads_cached_account_before_admission(db_session):
    account = await seed(db_session)
    await db_session.execute(
        update(BrowserAccount)
        .where(BrowserAccount.id == account.id)
        .values(
            status="closed",
            paused=True,
            status_reason_code="account_deleted",
            revision=8,
        )
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert account.status_reason_code is None
    request = DurableCommandV1(
        command_id="cached-command",
        workspace_id="delete-workspace",
        account_id="delete-account",
        kind="close_session",
        idempotency_scope="cached",
        idempotency_key="cached",
        epoch=0,
        expected_revision=7,
        available_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        payload={"reason": "completed"},
        session_id="delete-session",
    )
    with pytest.raises(SchedulerConflict, match="deleted"):
        await BrowserAccountScheduler().enqueue(db_session, request)
    assert account.status_reason_code == "account_deleted"


@pytest.mark.asyncio
async def test_session_guard_preserves_unflushed_account_changes(db_session):
    account = await seed(db_session)
    db_session.add(session())
    await db_session.commit()
    with db_session.no_autoflush:
        account.label = "尚未提交的新名称"
        await service._session_or_error(
            db_session, account.workspace_id, account.id, "delete-session", for_update=True
        )
        assert account.label == "尚未提交的新名称"
