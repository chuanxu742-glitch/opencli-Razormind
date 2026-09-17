from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.v1.automations import router
from backend.database import get_db
from backend.models.automation import Automation
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.security.identity import RequestIdentity, get_request_identity


async def test_admin_can_create_list_and_update_disabled_automation_draft(db_session):
    user = User(subject="automation-admin")
    workspace = Workspace(name="Automation", slug="automation")
    db_session.add_all((user, workspace))
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.ADMIN)
    )
    await db_session.commit()

    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db_session

    async def override_identity():
        return RequestIdentity(subject=user.subject)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_request_identity] = override_identity
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/workspaces/{workspace.id}/automations",
            json={
                "name": "Daily review",
                "prompt": "Review the workspace",
                "executor": "codex",
                "schedule": "daily@09:00",
                "timezone": "Asia/Shanghai",
                "enabled": False,
            },
        )
        automation_id = created.json()["data"]["id"]
        listed = await client.get(f"/workspaces/{workspace.id}/automations")
        updated = await client.patch(
            f"/workspaces/{workspace.id}/automations/{automation_id}",
            json={"name": "Updated review"},
        )

    assert created.status_code == 201
    assert listed.json()["data"][0]["name"] == "Daily review"
    assert updated.json()["data"]["name"] == "Updated review"
    assert updated.json()["data"]["enabled"] is False
    assert updated.json()["data"]["revision"] == 2


async def test_enabled_automation_rejects_an_unbound_agent_contract(db_session):
    user = User(subject="automation-enabled")
    workspace = Workspace(name="Enabled automation", slug="automation-enabled")
    db_session.add_all((user, workspace))
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.ADMIN)
    )
    await db_session.commit()

    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db_session

    async def override_identity():
        return RequestIdentity(subject=user.subject)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_request_identity] = override_identity
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/workspaces/{workspace.id}/automations",
            json={
                "operations_agent_id": "missing-agent",
                "operations_agent_version": 1,
                "name": "Enabled review",
                "prompt": "Review the workspace",
                "executor": "codex",
                "schedule": "daily@09:00",
                "timezone": "Asia/Shanghai",
                "enabled": True,
            },
        )
        draft = await client.post(
            f"/workspaces/{workspace.id}/automations",
            json={
                "operations_agent_id": "missing-agent",
                "operations_agent_version": 1,
                "name": "Disabled review",
                "prompt": "Review the workspace",
                "executor": "codex",
                "schedule": "daily@09:00",
                "timezone": "Asia/Shanghai",
                "enabled": False,
            },
        )
        automation_id = draft.json()["data"]["id"]
        updated = await client.patch(
            f"/workspaces/{workspace.id}/automations/{automation_id}",
            json={"enabled": True},
        )
        row = await db_session.get(Automation, automation_id)
        assert row is not None
        row.enabled = True
        await db_session.commit()
        started = await client.post(
            f"/workspaces/{workspace.id}/automations/{automation_id}/runs",
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Bound Operations Agent must belong to Automation Workspace"
    assert draft.status_code == 201
    assert updated.status_code == 422
    assert updated.json()["detail"] == "Bound Operations Agent must belong to Automation Workspace"
    assert started.status_code == 409
    assert started.json()["detail"] == "Bound Operations Agent must belong to Automation Workspace"


def test_starter_routes_are_registered_once():
    methods_and_paths = [
        (method, route.path)
        for route in router.routes
        for method in route.methods or ()
        if route.path
        in {
            "/workspaces/{workspace_id}/automations/starters/preview",
            "/workspaces/{workspace_id}/automations/starters/install",
        }
    ]

    assert (
        methods_and_paths.count(("GET", "/workspaces/{workspace_id}/automations/starters/preview"))
        == 1
    )
    assert (
        methods_and_paths.count(("POST", "/workspaces/{workspace_id}/automations/starters/install"))
        == 1
    )


async def test_pinned_automation_can_create_update_and_start(db_session, monkeypatch):
    from backend import ws_agent_manager
    from backend.services import automation_schedule_service
    from tests.unit.test_automation_schedule_service import _seed_bound_automation

    existing, agent, profile = await _seed_bound_automation(db_session)
    user = await db_session.get(User, existing.created_by_user_id)
    workspace_id = existing.workspace_id
    db_session.add(
        WorkspaceMembership(workspace_id=workspace_id, user_id=user.id, role=WorkspaceRole.ADMIN)
    )
    await db_session.commit()
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda _url: True)
    monkeypatch.setattr(
        automation_schedule_service, "schedule_operations_agent_run", lambda _id: None
    )

    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db_session

    async def override_identity():
        return RequestIdentity(subject=user.subject)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_request_identity] = override_identity
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/workspaces/{workspace_id}/automations",
            json={
                "operations_agent_id": agent.id,
                "operations_agent_version": 1,
                "name": "Bound review",
                "prompt": "Review the workspace",
                "executor": "operations-agent",
                "schedule": "daily@09:00",
                "approval_mode": "observe_only",
                "enabled": True,
            },
        )
        assert created.status_code == 201
        automation_id = created.json()["data"]["id"]
        updated = await client.patch(
            f"/workspaces/{workspace_id}/automations/{automation_id}",
            json={"name": "Updated bound review"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["revision"] == 2
        started = await client.post(f"/workspaces/{workspace_id}/automations/{automation_id}/runs")

    assert started.status_code == 201
    run = started.json()["data"]
    assert run["operations_agent_id"] == agent.id
    assert run["published_version"] == 1
    assert run["profile_version"] == profile.version
    assert run["automation_revision"] == 2
    assert run["automation_snapshot"]["name"] == "Updated bound review"
    assert run["trigger_type"] == "manual"
    assert run["status"] == "queued"
