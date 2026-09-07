"""Execution-session refactor boundary regression coverage."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.models.browser import (
    BrowserAccount,
    BrowserDurableCommand,
    BrowserProfileManifest,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.schemas.browser_account import (
    AccountRef,
    BrowserAccountErrorCode,
    ExecutionContextV1,
)
from backend.services.browser_account_service import (
    BrowserAccountError,
    ensure_execution_session,
)


@pytest.mark.asyncio
async def test_manifest_mismatch_does_not_create_execution_command(db_session):
    """Admission rejects a mismatched manifest before creating execution state."""
    now = datetime.now(UTC)
    workspace = Workspace(id="refactor-w", name="Refactor", slug="refactor")
    user = User(id="refactor-user", subject="refactor-subject", disabled=False)
    node = EdgeNode(
        id="refactor-node",
        url="http://refactor-agent:19823",
        boot_id="refactor-boot",
        account_capable=True,
        status="online",
    )
    bundle = BrowserRuntimeBundle(
        id="refactor-bundle",
        name="managed",
        version="1",
        manifest={"opencli": "1.8.7"},
    )
    account = BrowserAccount(
        id="refactor-account",
        workspace_id=workspace.id,
        site="fixture.test",
        label="Refactor fixture",
        node_id=node.id,
        profile_id="refactor-profile",
        profile_version=1,
        profile_manifest_id="refactor-manifest",
        runtime_bundle_id=bundle.id,
        runtime_bundle_version=bundle.version,
        auth_evidence="valid",
        evidence_source="rule_verified",
        status="dormant",
        revision=0,
    )
    profile_command = BrowserDurableCommand(
        id="refactor-profile-command",
        workspace_id=workspace.id,
        account_id=account.id,
        node_id=node.id,
        kind="start_login",
        idempotency_scope="profile:refactor-account",
        idempotency_key="committed",
        expected_revision=0,
        available_at=now,
        expires_at=now + timedelta(minutes=30),
        status="succeeded",
        payload={},
    )
    manifest = BrowserProfileManifest(
        id=account.profile_manifest_id,
        workspace_id=workspace.id,
        account_id=account.id,
        profile_id="different-profile",
        version=account.profile_version,
        node_id=node.id,
        command_id=profile_command.id,
        writer_epoch=1,
        bundle_name=bundle.name,
        browser_version="stable",
        files_count=1,
        total_bytes=1,
        checksum_manifest_ref="sha256:manifest",
        complete_marker="complete",
        committed_at=now,
        state="committed",
    )
    db_session.add_all(
        [
            workspace,
            user,
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role=WorkspaceRole.OPERATOR,
            ),
            node,
            bundle,
            account,
            profile_command,
            manifest,
        ]
    )
    await db_session.flush()
    ref = AccountRef(workspace_id=workspace.id, account_id=account.id)
    context = ExecutionContextV1(
        account_ref=ref,
        execution_id="refactor-execution",
        caller_id=user.id,
    )

    with pytest.raises(BrowserAccountError) as raised:
        await ensure_execution_session(db_session, ref, context, actor_user_id=user.id)

    assert raised.value.code == BrowserAccountErrorCode.PROFILE_CORRUPT.value
    assert (
        await db_session.scalar(
            select(BrowserDurableCommand).where(
                BrowserDurableCommand.account_id == account.id,
                BrowserDurableCommand.kind == "execute_reference",
            )
        )
    ) is None
