"""固定账号执行会话 producer 的边界回归测试。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models.browser import (
    BrowserAccount,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserProfileManifest,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.source_binding import (
    Source,
    SourceBinding,
    SourceBindingRevision,
    SourceRevision,
)
from backend.models.workflow import Project
from backend.pipeline.pipeline import _resolve_account_execution
from backend.schemas.browser_account import (
    AccountRef,
    BrowserAccountErrorCode,
    ExecutionContextV1,
)
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
        caller_id=user.subject,
        source_binding_revision_id=binding_revision_id,
    )


@pytest.mark.asyncio
async def test_producer_is_idempotent_and_pins_binding_revision(db_session):
    workspace, user, _node, account = await _seed_account(db_session)
    revision = await _binding(db_session, workspace.id, account.id, user.id)
    ref, context = _context(account, user, revision.id)

    first = await ensure_execution_session(db_session, ref, context)
    await db_session.commit()
    second = await ensure_execution_session(db_session, ref, context)
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
async def test_producer_blocks_auth_required_account_without_queueing(db_session):
    _workspace, user, _node, account = await _seed_account(db_session, auth_required=True)
    ref, context = _context(account, user)

    with pytest.raises(BrowserAccountError) as raised:
        await ensure_execution_session(db_session, ref, context)

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
    await db_session.commit()
    source = type(
        "Source",
        (),
        {
            "id": "producer-source",
            "channel_config": {
                "workspace_id": account.workspace_id,
                "account_id": account.id,
            },
        },
    )()
    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    with patch("backend.database.AsyncSessionLocal", session_factory):
        with pytest.raises(RuntimeError, match="lease_waiting"):
            await _resolve_account_execution(
                source,
                {
                    "execution_id": "pipeline-execution",
                    "caller_id": user.subject,
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
                    select(BrowserLoginSession).where(
                        BrowserLoginSession.execution_id == "pipeline-execution"
                    )
                )
            ).all()

    # A separate session proves the producer transaction committed before
    # the resolver returned its bounded waiting result.
    assert sorted(
        (command.kind, command.status, command.execution_id) for command in commands
    ) == [
        ("execute_reference", "queued", "pipeline-execution"),
        ("start_login", "succeeded", None),
    ]
    assert len(sessions) == 1
    assert sessions[0].status == "opening"
