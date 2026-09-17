from __future__ import annotations

import pytest

from backend.main import app
from backend.models.browser import BrowserAccount
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.security.identity import RequestIdentity, get_request_identity


async def _seed(db_session, role=WorkspaceRole.ADMIN):
    db_session.add_all([
        User(id="rename-user", subject="rename-subject"),
        Workspace(id="rename-workspace", name="Rename", slug="rename"),
        WorkspaceMembership(
            id="rename-member", workspace_id="rename-workspace",
            user_id="rename-user", role=role,
        ),
        BrowserAccount(
            id="rename-account", workspace_id="rename-workspace", site="github.com",
            label="Original", status="presenting", revision=7,
        ),
    ])
    await db_session.commit()

    async def identity_override():
        return RequestIdentity(subject="rename-subject", auth_method="test")

    app.dependency_overrides[get_request_identity] = identity_override
    return "/api/v1/workspaces/rename-workspace/browser-accounts/rename-account"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [WorkspaceRole.ADMIN, WorkspaceRole.MAINTAINER])
async def test_rename_preserves_login_state_and_uses_revision(client, db_session, role):
    path = await _seed(db_session, role)
    before = (await client.get(path)).json()["data"]
    response = await client.patch(path, headers={"If-Match": "7"}, json={
        "label": "  GitHub 工作号  ", "expected_revision": 7,
    })
    assert response.status_code == 200, response.text
    renamed = response.json()["data"]
    assert renamed["label"] == "GitHub 工作号"
    assert renamed["revision"] == 8
    for key in before.keys() - {"label", "revision", "updated_at"}:
        assert renamed[key] == before[key], key
    assert (await client.get(path)).json()["data"]["label"] == "GitHub 工作号"
    stale = await client.patch(path, json={"label": "Old writer", "expected_revision": 7})
    assert stale.status_code == 409
    unchanged = await client.patch(path, json={"label": "GitHub 工作号", "expected_revision": 8})
    assert unchanged.status_code == 200
    assert unchanged.json()["data"]["revision"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [WorkspaceRole.OPERATOR, WorkspaceRole.VIEWER])
async def test_non_managers_cannot_rename(client, db_session, role):
    path = await _seed(db_session, role)
    response = await client.patch(path, json={"label": "Forbidden", "expected_revision": 7})
    assert response.status_code == 403
    assert (await client.get(path)).json()["data"]["label"] == "Original"


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["", " \t\n ", "x" * 256, None])
async def test_rename_rejects_invalid_names(client, db_session, label):
    path = await _seed(db_session)
    response = await client.patch(path, json={"label": label, "expected_revision": 7})
    assert response.status_code == 422
    assert (await client.get(path)).json()["data"]["revision"] == 7


@pytest.mark.asyncio
async def test_rename_cannot_cross_workspace(client, db_session):
    path = await _seed(db_session)
    response = await client.patch(path.replace("rename-workspace", "foreign-workspace"), json={
        "label": "Forbidden", "expected_revision": 7,
    })
    assert response.status_code in {403, 404}
    assert (await client.get(path)).json()["data"]["label"] == "Original"
