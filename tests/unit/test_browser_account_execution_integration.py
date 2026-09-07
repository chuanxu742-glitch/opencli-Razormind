"""Regression coverage for browser-account execution integration seams."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.channels.base import ChannelResult
from backend.channels.opencli_channel import OpenCLIChannel
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserBinding,
    BrowserCommandStatus,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserProfileManifest,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.source import DataSource
from backend.models.task import CollectionTask
from backend.pipeline.pipeline import _resolve_account_execution
from backend.schemas.browser_account import SessionEnvelopeV1
from backend.services.browser_service import inspect_legacy_binding_migration


@pytest.mark.asyncio
async def test_legacy_mapping_pages_preserve_known_and_unknown_profiles(db_session):
    workspace = Workspace(id="migration-workspace", name="Migration", slug="migration")
    account = BrowserAccount(
        id="known-account", workspace_id=workspace.id, site="fixture.test",
        label="Known", profile_id="known-profile",
    )
    bindings = [
        BrowserBinding(id=f"binding-{index}", site=f"site-{index}",
                       browser_endpoint="known-profile" if index == 0 else f"unknown-{index}")
        for index in range(3)
    ]
    db_session.add_all([workspace, account, *bindings])
    await db_session.commit()
    first = await inspect_legacy_binding_migration(db_session, limit=2)
    second = await inspect_legacy_binding_migration(
        db_session, limit=2, after_id=first["next_cursor"]
    )
    rows = first["items"] + second["items"]
    assert [row["binding_id"] for row in rows] == [binding.id for binding in bindings]
    assert rows[0]["account_ids"] == [account.id]
    assert rows[0]["status"] == "ready"
    assert all(row["status"] == "account_migration_required" for row in rows[1:])
    assert all(row["preserved"] for row in rows)
    assert second["next_cursor"] is None
    assert (await db_session.scalars(select(BrowserBinding))).all() == bindings


def _sessionmaker(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_boot", ["boot-current", "boot-stale"])
async def test_pipeline_account_resolution_preserves_node_generation(
    db_engine, db_session, session_boot
):
    """Only the original current generation resolves; reads never rewrite it."""
    now = datetime.now(timezone.utc)
    workspace = Workspace(id="workspace-1", name="Workspace", slug="workspace-1")
    node = EdgeNode(
        id="node-1",
        url="http://agent:19823",
        boot_id="boot-current",
        account_capable=True,
        status="online",
    )
    bundle = BrowserRuntimeBundle(
        id="bundle-1",
        name="managed",
        version="1",
        manifest={"opencli": "1.8.7"},
    )
    account = BrowserAccount(
        id="account-1",
        workspace_id=workspace.id,
        site="example.com",
        label="Example",
        node_id=node.id,
        profile_id="profile-1",
        profile_version=1,
        profile_manifest_id="manifest-1",
        runtime_bundle_id=bundle.id,
        runtime_bundle_version=bundle.version,
        status="presenting",
        auth_evidence="valid",
        evidence_source="rule_verified",
    )
    session = BrowserLoginSession(
        id="session-1",
        workspace_id=workspace.id,
        account_id=account.id,
        node_id=node.id,
        node_boot_id=session_boot,
        lease_id="lease-1",
        epoch=2,
        profile_id=account.profile_id,
        profile_version=account.profile_version,
        profile_state="committed",
        purpose="execution",
        execution_id="execution-1",
        command_id="command-1",
        status="presenting",
        expires_at=now + timedelta(minutes=20),
    )
    command = BrowserDurableCommand(
        id="command-1",
        workspace_id=workspace.id,
        account_id=account.id,
        node_id=node.id,
        kind="execute_reference",
        idempotency_scope="session:session-1",
        idempotency_key="execute",
        execution_id="execution-1",
        epoch=2,
        expected_revision=account.revision,
        available_at=now,
        expires_at=now + timedelta(minutes=20),
        status=BrowserCommandStatus.QUEUED.value,
        session_id=session.id,
        payload={},
    )
    manifest = BrowserProfileManifest(
        id="manifest-1",
        workspace_id=workspace.id,
        account_id=account.id,
        profile_id=account.profile_id,
        version=1,
        node_id=node.id,
        command_id=command.id,
        writer_epoch=2,
        bundle_name=bundle.name,
        browser_version="stable",
        files_count=1,
        total_bytes=1,
        checksum_manifest_ref="sha256:manifest",
        complete_marker="complete",
        committed_at=now,
        state="committed",
    )
    lease = BrowserAccountLease(
        id="lease-row-1",
        workspace_id=workspace.id,
        account_id=account.id,
        node_id=node.id,
        node_boot_id=node.boot_id,
        lease_id="lease-1",
        epoch=2,
        owner_id="scheduler",
        status="active",
        acquired_at=now,
        renewed_at=now,
        expires_at=now + timedelta(minutes=20),
    )
    db_session.add_all([workspace, node, bundle, account, session, command, manifest, lease])
    await db_session.commit()

    source = DataSource(
        id="source-1",
        name="Account source",
        channel_type="opencli",
        channel_config={"workspace_id": workspace.id, "account_id": account.id},
    )
    actor = User(id="caller-1", subject="execution-fixture-actor", disabled=False)
    task = CollectionTask(
        id="execution-1",
        source_id=source.id,
        requested_by_user_id=actor.id,
        trigger_type="manual",
        parameters={},
    )
    db_session.add_all([
        actor, source, task,
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=actor.id, role=WorkspaceRole.OPERATOR
        ),
    ])
    await db_session.commit()
    with patch("backend.database.AsyncSessionLocal", _sessionmaker(db_engine)):
        if session_boot == "boot-stale":
            with pytest.raises(RuntimeError, match="lease_waiting"):
                await _resolve_account_execution(
                    task.id, source, {}
                )
        else:
            ref, envelope = await _resolve_account_execution(
                task.id, source, {}
            )
            assert ref.account_id == account.id
            assert envelope.session_id == session.id
    async with _sessionmaker(db_engine)() as verification_session:
        persisted = await verification_session.get(BrowserLoginSession, session.id)
    assert persisted is not None
    assert persisted.node_boot_id == session_boot


@pytest.mark.asyncio
async def test_opencli_account_session_separates_internal_controls_and_acquires_once(
    db_engine, db_session
):
    """Account routing controls never become CLI args and use one pool lease."""
    node = EdgeNode(
        id="node-2",
        url="http://account-agent:19823",
        boot_id="boot-account",
        account_capable=True,
        status="online",
    )
    db_session.add(node)
    await db_session.commit()

    envelope = SessionEnvelopeV1(
        workspace_id="workspace-1",
        account_id="account-1",
        session_id="session-1",
        profile_id="profile-1",
        profile_version=1,
        node_id=node.id,
        node_boot_id=node.boot_id,
        lease_id="lease-1",
        epoch=2,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
        runtime_bundle_id="bundle-1",
        runtime_bundle_version="1",
        target={"tab_id": None, "frame_id": None, "document_id": None, "origin": None},
        view_generation=0,
        purpose="execution",
        execution_id="execution-1",
        command_id="command-1",
        profile_state="committed",
        profile_manifest_id="manifest-1",
    )

    pool = MagicMock()
    pool.endpoints = [node.url]
    pool.get_mode.return_value = "cdp"
    pool.get_agent_protocol.return_value = "http"
    pool.get_agent_url.return_value = "http://account-agent:8000"
    acquire_context = AsyncMock()
    acquire_context.__aenter__ = AsyncMock(return_value=node.url)
    acquire_context.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acquire_context
    settings = MagicMock(collection_mode="agent")
    result = ChannelResult.ok([{"id": "item-1"}])

    with (
        patch("backend.browser_pool.get_pool", return_value=pool),
        patch("backend.config.get_settings", return_value=settings),
        patch("backend.database.AsyncSessionLocal", _sessionmaker(db_engine)),
        patch(
            "backend.channels.opencli_channel._get_named_options",
            new=AsyncMock(return_value=frozenset({"query", "account_id"})),
        ),
        patch(
            "backend.channels.opencli_channel._command_requires_browser",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "backend.channels.opencli_channel._collect_via_agent",
            new=AsyncMock(return_value=result),
        ) as collect_agent,
    ):
        collected = await OpenCLIChannel().collect(
            {
                "site": "example.com",
                "command": "search",
                "args": {
                    "query": "from-config",
                    "account_session": {"must_not": "leak"},
                    "account_id": "must-not-leak",
                    "chrome_endpoint": "must-not-leak",
                    "required_profile_kind": "must-not-leak",
                },
            },
            {
                "account_session": envelope,
                "query": "from-parameters",
                "execution_id": "execution-1",
            },
        )

    assert collected.success is True
    pool.acquire.assert_called_once_with(endpoint=node.url)
    call = collect_agent.await_args
    assert call.args[3] == {"query": "from-parameters"}
    assert call.args[4] == []
    assert call.args[8] is envelope
