"""固定账号执行会话 producer 的边界回归测试。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.v1.tasks import router as tasks_router
from backend.database import get_db
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserProfileManifest,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.source import DataSource
from backend.models.source_binding import (
    Source,
    SourceBinding,
    SourceBindingRevision,
    SourceRevision,
)
from backend.models.task import CollectionTask
from backend.models.workflow import Project
from backend.pipeline.pipeline import _resolve_account_execution
from backend.schemas.browser_account import (
    AccountRef,
    BrowserAccountErrorCode,
    ExecutionContextV1,
)
from backend.security.identity import RequestIdentity
from backend.services import browser_account_service
from backend.services.browser_account_service import (
    BrowserAccountError,
    ensure_execution_session,
)

_NOW = datetime.now(UTC)


async def _seed_account(db_session, *, auth_required: bool = False):
    workspace = Workspace(id="producer-w", name="Producer", slug="producer")
    user = User(id="producer-user", subject="producer-subject", disabled=False)
    membership = WorkspaceMembership(
        workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OPERATOR
    )
    node = EdgeNode(
        id="producer-node",
        url="http://producer-agent:19823",
        boot_id="producer-boot",
        account_capable=True,
        status="online",
    )
    bundle = BrowserRuntimeBundle(
        id="producer-bundle",
        name="managed",
        version="1",
        manifest={"opencli": "1.8.7"},
    )
    account = BrowserAccount(
        id="producer-account",
        workspace_id=workspace.id,
        site="fixture.test",
        label="Fixture",
        node_id=node.id,
        profile_id="producer-profile",
        profile_version=7,
        profile_manifest_id="producer-manifest",
        runtime_bundle_id=bundle.id,
        runtime_bundle_version=bundle.version,
        auth_required=auth_required,
        auth_evidence="valid",
        evidence_source="rule_verified",
        status="dormant",
        revision=0,
    )
    old_command = BrowserDurableCommand(
        id="producer-profile-command",
        workspace_id=workspace.id,
        account_id=account.id,
        node_id=node.id,
        kind="start_login",
        idempotency_scope="profile:producer-account",
        idempotency_key="committed",
        expected_revision=0,
        available_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
        status="succeeded",
        payload={},
    )
    manifest = BrowserProfileManifest(
        id="producer-manifest",
        workspace_id=workspace.id,
        account_id=account.id,
        profile_id=account.profile_id,
        version=account.profile_version,
        node_id=node.id,
        command_id=old_command.id,
        writer_epoch=1,
        bundle_name=bundle.name,
        browser_version="stable",
        files_count=1,
        total_bytes=1,
        checksum_manifest_ref="sha256:manifest",
        complete_marker="complete",
        committed_at=_NOW,
        state="committed",
    )
    db_session.add_all([workspace, user, membership, node, bundle, account, old_command, manifest])
    await db_session.flush()
    return workspace, user, node, account


async def _binding(db_session, workspace_id: str, account_id: str, user_id: str):
    project = Project(
        id="producer-project",
        workspace_id=workspace_id,
        name="Producer project",
        slug="producer-project",
        created_by_user_id=user_id,
    )
    source = Source(
        id="producer-source",
        workspace_id=workspace_id,
        name="Producer source",
        slug="producer-source",
        adapter_type="opencli",
        created_by_user_id=user_id,
    )
    source_revision = SourceRevision(
        id="producer-source-revision",
        source_id=source.id,
        revision_number=3,
        adapter_config={"site": "fixture.test"},
        created_by_user_id=user_id,
    )
    binding = SourceBinding(
        id="producer-binding",
        project_id=project.id,
        source_id=source.id,
        name="Producer binding",
        slug="producer-binding",
        created_by_user_id=user_id,
        current_revision_number=1,
    )
    revision = SourceBindingRevision(
        id="producer-binding-revision",
        source_binding_id=binding.id,
        revision_number=1,
        pinned_source_revision_id=source_revision.id,
        scope_config={},
        workspace_id=workspace_id,
        account_id=account_id,
        created_by_user_id=user_id,
    )
    db_session.add_all([project, source, source_revision, binding, revision])
    await db_session.flush()
    return revision


def _context(account, user, binding_revision_id=None, execution_id="execution-1"):
    ref = AccountRef(
        workspace_id=account.workspace_id,
        account_id=account.id,
        source_binding_revision_id=binding_revision_id,
    )
    return ref, ExecutionContextV1(
        account_ref=ref,
        execution_id=execution_id,
        caller_id=user.id,
        source_binding_revision_id=binding_revision_id,
    )


@pytest.mark.asyncio
async def test_producer_is_idempotent_and_pins_binding_revision(db_session):
    workspace, user, _node, account = await _seed_account(db_session)
    revision = await _binding(db_session, workspace.id, account.id, user.id)
    ref, context = _context(account, user, revision.id)

    first = await ensure_execution_session(db_session, ref, context, actor_user_id=user.id)
    await db_session.commit()
    second = await ensure_execution_session(db_session, ref, context, actor_user_id=user.id)
    await db_session.commit()

    assert second.id == first.id
    commands = (
        await db_session.scalars(
            select(BrowserDurableCommand).where(
                BrowserDurableCommand.account_id == account.id,
                BrowserDurableCommand.kind == "execute_reference",
            )
        )
    ).all()
    assert len(commands) == 1
    command = commands[0]
    assert command.binding_revision_id == revision.id
    assert command.payload == {
        "execution_id": context.execution_id,
        "source_binding_revision_id": revision.id,
    }
    assert command.session_id == first.id
    assert first.profile_version == account.profile_version


@pytest.mark.asyncio
async def test_producer_rejects_conflicting_fixed_binding_revision(db_session):
    workspace, user, _node, account = await _seed_account(db_session)
    revision = await _binding(db_session, workspace.id, account.id, user.id)
    ref, context = _context(account, user, revision.id)
    await ensure_execution_session(db_session, ref, context, actor_user_id=user.id)
    await db_session.commit()

    conflicting_revision = SourceBindingRevision(
        id="producer-binding-revision-2",
        source_binding_id=revision.source_binding_id,
        revision_number=2,
        pinned_source_revision_id=revision.pinned_source_revision_id,
        scope_config={},
        workspace_id=workspace.id,
        account_id=account.id,
        created_by_user_id=user.id,
    )
    db_session.add(conflicting_revision)
    await db_session.commit()
    conflicting_ref, conflicting_context = _context(
        account,
        user,
        conflicting_revision.id,
    )

    with pytest.raises(BrowserAccountError) as raised:
        await ensure_execution_session(
            db_session,
            conflicting_ref,
            conflicting_context,
            actor_user_id=user.id,
        )

    assert raised.value.code == BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED.value
    sessions = (
        await db_session.scalars(
            select(BrowserLoginSession).where(
                BrowserLoginSession.execution_id == context.execution_id
            )
        )
    ).all()
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_producer_blocks_auth_required_account_without_queueing(db_session):
    _workspace, user, _node, account = await _seed_account(db_session, auth_required=True)
    ref, context = _context(account, user)

    with pytest.raises(BrowserAccountError) as raised:
        await ensure_execution_session(db_session, ref, context, actor_user_id=user.id)

    assert raised.value.code == BrowserAccountErrorCode.AUTH_REQUIRED.value
    assert (
        await db_session.scalar(
            select(BrowserDurableCommand).where(
                BrowserDurableCommand.account_id == account.id,
                BrowserDurableCommand.kind == "execute_reference",
            )
        )
    ) is None


@pytest.mark.asyncio
async def test_pipeline_commits_queued_producer_before_reporting_waiting(db_engine, db_session):
    _workspace, user, _node, account = await _seed_account(db_session)
    source = DataSource(
        id="producer-data-source",
        name="Producer data source",
        channel_type="opencli",
        channel_config={
            "workspace_id": account.workspace_id,
            "account_id": account.id,
        },
    )
    task = CollectionTask(
        id="pipeline-execution",
        source_id=source.id,
        trigger_type="manual",
        parameters={},
        requested_by_user_id=user.id,
    )
    db_session.add_all([source, task])
    await db_session.commit()
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("backend.database.AsyncSessionLocal", session_factory):
        with pytest.raises(RuntimeError, match="lease_waiting"):
            await _resolve_account_execution(
                task.id,
                source,
                {
                    "execution_id": "forged-execution",
                    "caller_id": "forged-caller",
                },
            )

        async with session_factory() as verification:
            commands = (
                await verification.scalars(
                    select(BrowserDurableCommand)
                    .where(BrowserDurableCommand.account_id == account.id)
                    .order_by(BrowserDurableCommand.id)
                )
            ).all()
            sessions = (
                await verification.scalars(
                    select(BrowserLoginSession).where(BrowserLoginSession.execution_id == task.id)
                )
            ).all()

    # A separate session proves the producer transaction committed before
    # the resolver returned its bounded waiting result.
    assert sorted((command.kind, command.status, command.execution_id) for command in commands) == [
        ("execute_reference", "queued", task.id),
        ("start_login", "succeeded", None),
    ]
    assert len(sessions) == 1
    assert sessions[0].status == "opening"


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def dispatch_collection(self, task_id: str, parameters: dict) -> dict:
        self.calls.append((task_id, dict(parameters)))
        return {"task_id": task_id}


async def _post_trigger(
    db_session,
    *,
    source_id: str,
    parameters: dict,
    identity: RequestIdentity | None,
):
    app = FastAPI()
    app.include_router(tasks_router)
    executor = _RecordingExecutor()

    async def override_db():
        yield db_session

    async def resolve_identity(_request):
        if identity is None:
            raise AssertionError("legacy accountless task unexpectedly requested identity")
        return identity

    app.dependency_overrides[get_db] = override_db
    with (
        patch(
            "backend.api.v1.tasks.get_request_identity",
            new=resolve_identity,
        ),
        patch("backend.executor.get_executor", return_value=executor),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/tasks/trigger",
                json={"source_id": source_id, "parameters": parameters},
            )
    return response, executor


@pytest.mark.asyncio
async def test_manual_account_admission_persists_actor_and_ignores_forged_caller(
    db_engine,
    db_session,
):
    _workspace, user, _node, account = await _seed_account(db_session)
    source = DataSource(
        id="api-account-source",
        name="API account source",
        channel_type="opencli",
        channel_config={
            "workspace_id": account.workspace_id,
            "account_id": account.id,
        },
    )
    db_session.add(source)
    await db_session.commit()

    response, executor = await _post_trigger(
        db_session,
        source_id=source.id,
        parameters={
            "query": "fixture",
            "caller_id": "forged-member",
            "execution_id": "forged-execution",
        },
        identity=RequestIdentity(subject=user.subject),
    )

    assert response.status_code == 202
    task = await db_session.scalar(
        select(CollectionTask).where(CollectionTask.source_id == source.id)
    )
    assert task is not None
    assert task.requested_by_user_id == user.id
    assert task.parameters == {"query": "fixture"}
    assert executor.calls == [(task.id, {"query": "fixture"})]

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.database.AsyncSessionLocal", session_factory):
        for _ in range(2):
            with pytest.raises(RuntimeError, match="lease_waiting"):
                await _resolve_account_execution(
                    task.id,
                    source,
                    {
                        "caller_id": "forged-member",
                        "execution_id": "forged-execution",
                    },
                )

    execute_commands = (
        await db_session.scalars(
            select(BrowserDurableCommand).where(
                BrowserDurableCommand.account_id == account.id,
                BrowserDurableCommand.kind == "execute_reference",
            )
        )
    ).all()
    execution_sessions = (
        await db_session.scalars(
            select(BrowserLoginSession).where(BrowserLoginSession.execution_id == task.id)
        )
    ).all()
    assert len(execute_commands) == 1
    assert execute_commands[0].execution_id == task.id
    assert len(execution_sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [WorkspaceRole.VIEWER, None])
async def test_manual_account_admission_rejects_viewer_and_non_member(
    db_session,
    role,
):
    workspace, _operator, _node, account = await _seed_account(db_session)
    source = DataSource(
        id="denied-account-source",
        name="Denied account source",
        channel_type="opencli",
        channel_config={
            "workspace_id": workspace.id,
            "account_id": account.id,
        },
    )
    requester = User(
        id="denied-requester",
        subject="denied-requester-subject",
        disabled=False,
    )
    rows = [source, requester]
    if role is not None:
        rows.append(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=requester.id,
                role=role,
            )
        )
    db_session.add_all(rows)
    await db_session.commit()

    response, executor = await _post_trigger(
        db_session,
        source_id=source.id,
        parameters={},
        identity=RequestIdentity(subject=requester.subject),
    )

    assert response.status_code == 403
    assert executor.calls == []
    assert (
        await db_session.scalar(select(CollectionTask).where(CollectionTask.source_id == source.id))
    ) is None


@pytest.mark.asyncio
async def test_anonymous_legacy_task_admission_remains_unchanged(db_session, db_engine):
    source = DataSource(
        id="anonymous-source",
        name="Anonymous source",
        channel_type="rss",
        channel_config={"feed_url": "https://fixture.test/rss"},
    )
    db_session.add(source)
    await db_session.commit()

    response, executor = await _post_trigger(
        db_session,
        source_id=source.id,
        parameters={"limit": 5},
        identity=None,
    )

    assert response.status_code == 202
    task = await db_session.scalar(
        select(CollectionTask).where(CollectionTask.source_id == source.id)
    )
    assert task is not None
    assert task.requested_by_user_id is None
    assert executor.calls == [(task.id, {"limit": 5})]
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.database.AsyncSessionLocal", session_factory):
        assert await _resolve_account_execution(task.id, source, {"limit": 5}) is None


@pytest.mark.asyncio
async def test_pipeline_rejects_task_from_another_source(db_session, db_engine):
    workspace, user, _node, account = await _seed_account(db_session)
    admitted = DataSource(id="admitted-source", name="Admitted", channel_type="rss")
    other = DataSource(
        id="other-source",
        name="Other",
        channel_type="opencli",
        channel_config={"workspace_id": workspace.id, "account_id": account.id},
    )
    task = CollectionTask(
        id="source-mismatch-task",
        source_id=admitted.id,
        trigger_type="manual",
        requested_by_user_id=user.id,
        parameters={},
    )
    db_session.add_all([admitted, other, task])
    await db_session.commit()
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.database.AsyncSessionLocal", session_factory):
        with pytest.raises(ValueError, match="does not match the source"):
            await _resolve_account_execution(task.id, other, {})
    assert await db_session.scalar(
        select(BrowserDurableCommand.id).where(
            BrowserDurableCommand.kind == "execute_reference"
        )
    ) is None


@pytest.mark.asyncio
async def test_pipeline_rejects_account_task_without_persisted_actor(
    db_engine,
    db_session,
):
    workspace, _user, _node, account = await _seed_account(db_session)
    source = DataSource(
        id="unproven-source",
        name="Unproven source",
        channel_type="opencli",
        channel_config={
            "workspace_id": workspace.id,
            "account_id": account.id,
        },
    )
    task = CollectionTask(
        id="unproven-task",
        source_id=source.id,
        trigger_type="scheduled",
        parameters={},
    )
    db_session.add_all([source, task])
    await db_session.commit()
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("backend.database.AsyncSessionLocal", session_factory):
        with pytest.raises(ValueError, match="authenticated task actor"):
            await _resolve_account_execution(task.id, source, {})


@pytest.mark.asyncio
async def test_actor_revocation_blocks_preexisting_ready_execution_session(
    db_engine,
    db_session,
):
    workspace, user, node, account = await _seed_account(db_session)
    source = DataSource(
        id="revoked-source",
        name="Revoked source",
        channel_type="opencli",
        channel_config={
            "workspace_id": workspace.id,
            "account_id": account.id,
        },
    )
    task = CollectionTask(
        id="revoked-task",
        source_id=source.id,
        trigger_type="manual",
        parameters={},
        requested_by_user_id=user.id,
    )
    db_session.add_all([source, task])
    await db_session.commit()
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("backend.database.AsyncSessionLocal", session_factory):
        with pytest.raises(RuntimeError, match="lease_waiting"):
            await _resolve_account_execution(task.id, source, {})

    session = await db_session.scalar(
        select(BrowserLoginSession).where(BrowserLoginSession.execution_id == task.id)
    )
    assert session is not None
    session.status = "presenting"
    session.lease_id = "revoked-lease"
    session.epoch = 2
    session.node_id = node.id
    session.node_boot_id = node.boot_id
    db_session.add(
        BrowserAccountLease(
            id="revoked-lease-row",
            workspace_id=workspace.id,
            account_id=account.id,
            node_id=node.id,
            node_boot_id=node.boot_id,
            lease_id=session.lease_id,
            epoch=session.epoch,
            owner_id="scheduler",
            status="active",
            acquired_at=_NOW,
            renewed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=20),
        )
    )
    forged_actor = User(
        id="forged-actor",
        subject="forged-actor-subject",
        disabled=False,
    )
    db_session.add_all(
        [
            forged_actor,
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=forged_actor.id,
                role=WorkspaceRole.OPERATOR,
            ),
        ]
    )
    membership = await db_session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    assert membership is not None
    await db_session.delete(membership)
    await db_session.commit()

    with patch("backend.database.AsyncSessionLocal", session_factory):
        with pytest.raises(BrowserAccountError) as raised:
            await _resolve_account_execution(
                task.id,
                source,
                {"caller_id": forged_actor.id},
            )

    assert raised.value.code == BrowserAccountErrorCode.PERMISSION_DENIED.value
    assert (
        await db_session.scalar(
            select(BrowserLoginSession).where(
                BrowserLoginSession.id == session.id,
                BrowserLoginSession.status == "presenting",
            )
        )
    ) is not None


def test_account_ref_rejects_conflicting_fixed_source_revision():
    with pytest.raises(BrowserAccountError) as raised:
        browser_account_service.execution_account_ref(
            {
                "workspace_id": "workspace",
                "account_id": "account",
                "source_binding_revision_id": "request-revision",
            },
            {
                "workspace_id": "workspace",
                "account_id": "account",
                "source_binding_revision_id": "configured-revision",
            },
        )

    assert raised.value.code == BrowserAccountErrorCode.ACCOUNT_MIGRATION_REQUIRED.value
