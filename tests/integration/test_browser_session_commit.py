"""Observe response emission directly; HTTPX buffers request dependency cleanup."""

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend import database
from backend.main import app
from backend.models.browser import BrowserAccount, BrowserDurableCommand, BrowserLoginSession
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.schemas.browser_account import BrowserAccountCreate
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import browser_account_service as service
from tests.fixtures.browser_account_login import seed_login_resources


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "commit", "callback"])
async def test_session_creation_is_committed_before_success_response(tmp_path, monkeypatch, failure):
    # File-backed SQLite and NullPool guarantee reader/writer use independent transactions.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'commit.db'}", poolclass=NullPool)
    readers = async_sessionmaker(engine, expire_on_commit=False)
    entered_commit = asyncio.Event()
    allow_commit = asyncio.Event()
    response_started = asyncio.Event()
    callback_counts = []
    sent = []
    visible_at_response = []

    class RequestSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            database.queue_after_commit(self, publish)

        async def commit(self):
            entered_commit.set()
            await allow_commit.wait()
            if failure == "commit":
                raise RuntimeError("private_commit_secret")
            return await super().commit()

    async def publish():
        async with readers() as db:
            callback_counts.append(len(list(await db.scalars(select(BrowserLoginSession)))))
        if failure == "callback":
            raise RuntimeError("private_callback_secret")

    async def identity():
        return RequestIdentity(subject="commit-subject", auth_method="test")

    async def post(account_id, revision):
        body = json.dumps({"purpose": "browser", "expected_revision": revision}).encode()
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                response_started.set()
                if 200 <= message["status"] < 300:
                    async with readers() as db:
                        sessions = list(await db.scalars(select(BrowserLoginSession)))
                        commands = list(await db.scalars(select(BrowserDurableCommand)))
                        account = await db.get(BrowserAccount, account_id)
                        visible_at_response.append((len(sessions), len(commands), account.revision))
                        assert len(sessions) == len(commands) == 1
                        assert commands[0].id == sessions[0].command_id
                        assert callback_counts and callback_counts[-1] == 1

        path = f"/api/v1/workspaces/commit-w/browser-accounts/{account_id}/login-sessions"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345), "server": ("test", 80),
        }
        try:
            await app(scope, receive, send)
        except RuntimeError as exc:
            # ServerErrorMiddleware sends a safe 500, then re-raises to its server.
            assert failure == "commit" and str(exc) == "private_commit_secret"

    previous_overrides = dict(app.dependency_overrides)
    request = None
    waits = []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
        async with readers() as db:
            db.add_all([
                User(id="commit-u", subject="commit-subject"),
                Workspace(id="commit-w", name="Commit", slug="commit-w"),
                WorkspaceMembership(id="commit-m", workspace_id="commit-w", user_id="commit-u", role=WorkspaceRole.ADMIN),
            ])
            resources = await seed_login_resources(db)
            account = await service.create_browser_account(db, "commit-w", BrowserAccountCreate(workspace_id="commit-w", label="Commit", **resources))
            await db.commit()
            account_id, revision = account.id, account.revision

        monkeypatch.setattr(database, "AsyncSessionLocal", async_sessionmaker(engine, class_=RequestSession, expire_on_commit=False))
        # Use the real get_db lifecycle, including its post-response second commit.
        app.dependency_overrides.pop(database.get_db, None)
        app.dependency_overrides[get_request_identity] = identity
        request = asyncio.create_task(post(account_id, revision))
        waits = [asyncio.create_task(entered_commit.wait()), asyncio.create_task(response_started.wait())]
        await asyncio.wait_for(asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED), timeout=10)
        assert not response_started.is_set(), "A response escaped before the transaction committed"
        assert entered_commit.is_set()
        async with readers() as db:
            assert list(await db.scalars(select(BrowserLoginSession))) == []
            assert list(await db.scalars(select(BrowserDurableCommand))) == []
        assert callback_counts == []
        allow_commit.set()
        await asyncio.wait_for(request, timeout=10)
        statuses = [message["status"] for message in sent if message["type"] == "http.response.start"]
        response_body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        assert b"private_" not in response_body
        async with readers() as db:
            stored = list(await db.scalars(select(BrowserLoginSession)))
            current_account = await db.get(BrowserAccount, account_id)
        if failure == "commit":
            assert statuses == [500]
            assert json.loads(response_body)["success"] is False
            assert not stored and current_account.revision == revision
            assert callback_counts == [] and visible_at_response == []
        else:
            assert statuses == [201]
            assert json.loads(response_body)["data"]["id"] == stored[0].id
            assert stored[0].status == "opening" and stored[0].lease_id is None
            assert callback_counts == [1]  # Dependency cleanup cannot publish twice.
            # Reusing the queued session commits normally without another start command.
            sent.clear()
            await asyncio.wait_for(post(account_id, current_account.revision), timeout=10)
            response_body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
            assert json.loads(response_body)["data"]["id"] == stored[0].id
            assert callback_counts == [1, 1]
            assert len(visible_at_response) == 2
    finally:
        allow_commit.set()
        if request is not None:
            # Retrieve task errors without replacing the original assertion failure.
            await asyncio.gather(asyncio.wait_for(request, timeout=10), return_exceptions=True)
        for waiter in waits:
            waiter.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()
