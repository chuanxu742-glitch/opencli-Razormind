from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient

from backend.main import app
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.security.identity import RequestIdentity, get_request_identity

_WORKSPACE_ID = "portal-api-workspace"
_USER_ID = "portal-api-user"
_SUBJECT = "portal-api-subject"


def _route(account_id: str | None = None, suffix: str = "") -> str:
    base = f"/api/v1/workspaces/{_WORKSPACE_ID}/browser-accounts"
    if account_id is not None:
        base += f"/{account_id}"
    return base + suffix


def _data(response):
    assert response.is_success, response.text
    body = response.json()
    assert body["success"] is True
    assert body["error"] is None
    return body["data"]


@pytest.mark.asyncio
async def test_account_crud_session_portal_ticket_and_close_use_real_http_contract(
    client: AsyncClient, db_session
):
    db_session.add_all(
        [
            User(id=_USER_ID, subject=_SUBJECT),
            Workspace(id=_WORKSPACE_ID, name="Portal API", slug="portal-api"),
            WorkspaceMembership(
                id="portal-api-membership",
                workspace_id=_WORKSPACE_ID,
                user_id=_USER_ID,
                role=WorkspaceRole.ADMIN,
            ),
        ]
    )
    await db_session.commit()

    async def identity_override() -> RequestIdentity:
        return RequestIdentity(subject=_SUBJECT, auth_method="test")

    app.dependency_overrides[get_request_identity] = identity_override
    try:
        account = _data(
            await client.post(
                _route(),
                json={
                    "workspace_id": _WORKSPACE_ID,
                    "site": "fixture.test",
                    "label": "Fixture account",
                },
            )
        )
        account_id = account["id"]
        assert account["workspace_id"] == _WORKSPACE_ID
        assert account["status"] == "dormant"
        assert account["revision"] == 0

        listed = _data(await client.get(_route()))
        assert listed["next_cursor"] is None
        assert [row["id"] for row in listed["items"]] == [account_id]
        fetched = _data(await client.get(_route(account_id)))
        assert fetched["id"] == account_id

        paused = _data(
            await client.patch(
                _route(account_id),
                headers={"If-Match": "0"},
                json={"paused": True, "expected_revision": 0},
            )
        )
        assert paused["paused"] is True
        assert paused["revision"] == 1
        resumed = _data(
            await client.post(
                _route(account_id, "/resume"),
                headers={"If-Match": "1"},
                json={"expected_revision": 1},
            )
        )
        assert resumed["paused"] is False
        assert resumed["revision"] == 2

        stale = await client.patch(
            _route(account_id),
            headers={"If-Match": "0"},
            json={"paused": True, "expected_revision": 0},
        )
        assert stale.status_code == 409

        session = _data(
            await client.post(
                _route(account_id, "/login-sessions"),
                json={"purpose": "login", "expires_in_seconds": 600},
            )
        )
        session_id = session["id"]
        session_revision = session["revision"]
        session_route = _route(account_id, f"/login-sessions/{session_id}")
        assert session["account_id"] == account_id
        assert session["status"] == "opening"
        assert session_revision == 0

        sessions = _data(await client.get(_route(account_id, "/login-sessions")))
        assert [row["id"] for row in sessions] == [session_id]
        assert _data(await client.get(session_route))["id"] == session_id
        assert _data(await client.post(f"{session_route}/view"))["id"] == session_id

        csrf_token = secrets.token_urlsafe(24)
        issue = _data(
            await client.post(
                f"{session_route}/portal-ticket/issue",
                headers={"Origin": "http://test"},
                json={
                    "contract_version": 1,
                    "first_entry": "initial",
                    "account_ref": {
                        "workspace_id": _WORKSPACE_ID,
                        "account_id": account_id,
                    },
                    "session_id": session_id,
                    "expected_session_revision": session_revision,
                    "csrf_token": csrf_token,
                },
            )
        )
        ticket = issue["ticket"]
        issued_csrf = issue["csrf_token"]
        assert issue["status"] == "issued"
        assert issue["http_status"] == 200
        assert len(ticket) >= 16
        assert issued_csrf == csrf_token
        assert issue["ticket_id"] != ticket

        grant_response = await client.post(
            f"{session_route}/portal-ticket/redeem",
            headers={"Origin": "http://test"},
            json={
                "contract_version": 1,
                "first_entry": "initial",
                "account_ref": {
                    "workspace_id": _WORKSPACE_ID,
                    "account_id": account_id,
                },
                "session_id": session_id,
                "expected_session_revision": session_revision,
                "ticket_id": issue["ticket_id"],
                "ticket": ticket,
                "csrf_token": issued_csrf,
            },
        )
        grant = _data(grant_response)
        assert grant["status"] == "granted"
        assert grant["account_id"] == account_id
        assert grant["session_id"] == session_id
        assert grant["cookie_name"] == "qrac2_portal"
        assert "ticket" not in grant
        assert "csrf_token" not in grant
        assert "qrac2_portal=" in grant_response.headers.get("set-cookie", "")

        closed = _data(
            await client.post(
                f"{session_route}/close",
                headers={"If-Match": str(session_revision)},
                json={"reason": "completed"},
            )
        )
        assert closed["id"] == session_id
        assert closed["status"] == "closed"
    finally:
        app.dependency_overrides.pop(get_request_identity, None)
