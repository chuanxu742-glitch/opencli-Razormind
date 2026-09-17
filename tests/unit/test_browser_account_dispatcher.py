from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend import ws_agent_manager
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserCommandKind,
    BrowserDurableCommand,
    BrowserLoginSession,
)
from backend.models.edge_node import EdgeNode, EdgeNodeCapacity
from backend.models.identity import Workspace
from backend.schemas.browser_account import (
    BrowserAccountCreate,
    LoginObservationV1,
    NodeCapacityFactV1,
    NodeClaimV1,
    NodeIdentityV1,
)
from backend.services import browser_account_service as service
from backend.services.browser_account_dispatcher import (
    BrowserAccountDispatcher,
    _refresh_browser_auth_from_pre_save_observation,
    record_capacity,
)
from backend.services.browser_account_scheduler import BrowserAccountScheduler, NodeIdentity
from tests.fixtures.browser_account_login import seed_login_resources


@pytest.mark.asyncio
async def test_xhs_observed_login_hint_is_revocable_and_never_authenticates(db_engine, monkeypatch):
    import asyncio
    import json
    from pathlib import Path
    from backend.models.browser import BrowserRuntimeBundle

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="xhs-hint-w", name="XHS hint", slug="xhs-hint"))
        await db.flush()
        resources = await seed_login_resources(db)
        bundle = await db.get(BrowserRuntimeBundle, "login-bundle")
        bundle.version = "4"
        bundle.manifest = json.loads((Path(__file__).resolve().parents[2] / "chrome/runtime-bundles/opencli-default/4/manifest.json").read_text(encoding="utf-8"))
        resources.update(site="xiaohongshu.com", login_rule_id="xiaohongshu-qr", login_rule_version="0.2.0")
        account = await service.create_browser_account(db, "xhs-hint-w", BrowserAccountCreate(workspace_id="xhs-hint-w", label="XHS hint", **resources))
        session = await service.create_login_session(db, "xhs-hint-w", account.id, purpose="browser", expected_revision=account.revision)
        await db.commit()
    identity = NodeIdentityV1(node_id="login-node", boot_id="login-boot")
    monkeypatch.setattr(ws_agent_manager, "list_connected", lambda: ["https://node"])
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: True)
    monkeypatch.setattr(ws_agent_manager, "account_connection_identity", lambda url: identity)
    send = AsyncMock(return_value={"type": "done", "result": {"runtime_status": "healthy", "profile_id": "xhs-profile"}})
    monkeypatch.setattr(ws_agent_manager, "send_agent_task", send)
    dispatcher = BrowserAccountDispatcher(factory)
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    claim = send.call_args.args[1]["claim"]
    async with factory() as db:
        for generation, (signal, state, reason) in enumerate([
            ("signed_in_visible", "presenting", "browser_login_observed"),
            ("unknown", "presenting", None),
            ("signed_in_visible", "presenting", "browser_login_observed"),
            ("signed_in_visible", "challenge", None),
            ("session_authenticated", "presenting", "browser_session_verified"),
            ("unknown", "presenting", None),
            ("session_authenticated", "challenge", None),
            ("signed_in_visible", "presenting", "browser_login_observed"),
        ], 1):
            observation = LoginObservationV1(
                claim=claim, node_identity=identity,
                account_ref={"workspace_id": "xhs-hint-w", "account_id": account.id},
                session_id=session.id, epoch=claim["epoch"], rule_id="xiaohongshu-qr", rule_version="0.2.0",
                target={"tab_id": 1, "frame_id": 0, "document_id": "xhs-doc", "origin": "https://www.xiaohongshu.com"},
                view_generation=generation, state=state, evidence_kind="unknown",
                browser_session_state=signal, observed_at=datetime.now(UTC),
            )
            await service.apply_login_observation(db, "xhs-hint-w", account.id, observation)
            current = await db.get(BrowserAccount, account.id)
            assert current.status_reason_code == reason
            assert current.auth_evidence == "unknown"
            assert (current.evidence_observed_at is not None) == (reason == "browser_session_verified")
            assert current.evidence_source is None and current.platform_identity is None
            assert (await db.get(BrowserDurableCommand, claim["command_id"])).status == "running"
            assert len(list(await db.scalars(select(BrowserDurableCommand)))) == 1
            assert (await db.scalar(select(BrowserAccountLease))).status == "active"
        verified = LoginObservationV1.model_validate(observation.model_dump() | {
            "view_generation": 9, "state": "verifying", "evidence_kind": "valid",
            "external_identity": {"provider": "xiaohongshu", "subject": "0123456789abcdef01234567"},
        })
        await service.apply_login_observation(db, "xhs-hint-w", account.id, verified)
        assert current.status_reason_code is None
        assert current.auth_evidence == "valid"
        invalid = LoginObservationV1.model_validate(verified.model_dump() | {
            "view_generation": 10, "state": "unknown", "evidence_kind": "invalid",
            "external_identity": None, "error_code": "auth_required",
            "observed_at": datetime.now(UTC),
        })
        await service.apply_login_observation(db, "xhs-hint-w", account.id, invalid)
        assert current.auth_required is True and current.auth_evidence == "invalid"
        assert current.evidence_source is None
        assert current.status_reason_code == "auth_required"
        restored = LoginObservationV1.model_validate(verified.model_dump() | {
            "view_generation": 11, "observed_at": datetime.now(UTC),
        })
        await service.apply_login_observation(db, "xhs-hint-w", account.id, restored)
        assert current.auth_required is False and current.auth_evidence == "valid"
        assert current.evidence_source == "rule_verified"
        assert current.status_reason_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_browser_close_stays_saving_until_fenced_completion(db_engine, monkeypatch, stopped):
    import asyncio

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="close-save-w", name="Close save", slug="close-save"))
        await db.flush()
        resources = await seed_login_resources(db)
        account = await service.create_browser_account(
            db, "close-save-w", BrowserAccountCreate(workspace_id="close-save-w", label="Close save", **resources)
        )
        session = await service.create_login_session(db, "close-save-w", account.id, purpose="browser", expected_revision=account.revision)
        await db.commit()
    identity = NodeIdentityV1(node_id="login-node", boot_id="login-boot")
    monkeypatch.setattr(ws_agent_manager, "list_connected", lambda: ["https://node"])
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: True)
    monkeypatch.setattr(ws_agent_manager, "account_connection_identity", lambda url: identity)

    async def reply(url, payload, **kwargs):
        if payload["command"]["kind"] == "start_login":
            return {"type": "done", "result": {"runtime_status": "healthy", "profile_id": "closed-profile"}}
        if not stopped:
            return {"type": "error", "error_code": "runtime_stop_unconfirmed"}
        return {"type": "done", "result": {"runtime_status": "stopped", "profile_manifest": {
            "workspace_id": "close-save-w", "account_id": account.id, "profile_id": "closed-profile",
            "version": 1, "node_id": identity.node_id, "command_id": payload["command"]["command_id"],
            "writer_epoch": payload["claim"]["epoch"], "bundle": "opencli-default:2", "browser_version": "test",
            "files_count": 1, "total_bytes": 20, "checksum_manifest_ref": "checksums.json",
            "complete_marker": "complete.marker", "committed_at": datetime.now(UTC).isoformat(),
            "password_inventory_status": "not_present",
        }}}

    monkeypatch.setattr(ws_agent_manager, "send_agent_task", AsyncMock(side_effect=reply))
    dispatcher = BrowserAccountDispatcher(factory)
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    async with factory() as db:
        current = await db.get(BrowserLoginSession, session.id)
        closed = await service.close_login_session(db, "close-save-w", account.id, session.id, expected_revision=current.revision)
        saving_revision = closed.revision
        command_id = closed.command_id
        account_row = await db.get(BrowserAccount, account.id)
        account_revision = account_row.revision
        assert closed.status == account_row.status == "saving"
        assert closed.closed_at is None
        for reason in ("completed", "expired", "cancelled"):
            duplicate = await service.close_login_session(db, "close-save-w", account.id, session.id, reason=reason, expected_revision=saving_revision)
            assert duplicate.command_id == command_id
            assert duplicate.revision == saving_revision
            assert account_row.revision == account_revision
        reopened = await service.create_login_session(db, "close-save-w", account.id, purpose="browser", expected_revision=account_revision)
        assert reopened.id == session.id and reopened.status == "saving"
        assert reopened.command_id == command_id
        assert len(list(await db.scalars(select(BrowserDurableCommand)))) == 2
        await db.commit()
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    async with factory() as db:
        completed = await db.get(BrowserLoginSession, session.id)
        account_row = await db.get(BrowserAccount, account.id)
        assert completed.revision > saving_revision
        assert completed.status == ("saved" if stopped else "error")
        lease = await db.scalar(select(BrowserAccountLease))
        assert lease.status == ("released" if stopped else "quarantined")
        # A delayed viewer finalizer cannot enqueue another stop or launch a browser.
        await service.close_login_session(db, "close-save-w", account.id, session.id, reason="completed")
        assert len(list(await db.scalars(select(BrowserDurableCommand)))) == 2
        if stopped:
            assert completed.closed_at is not None
            assert account_row.profile_manifest_id is not None
            reopened = await service.create_login_session(db, "close-save-w", account.id, purpose="browser", expected_revision=account_row.revision)
            assert reopened.id != session.id and reopened.status == "opening"
        else:
            assert completed.closed_at is None
            assert account_row.status == "error"
            with pytest.raises(service.BrowserAccountError) as error:
                await service.create_login_session(db, "close-save-w", account.id, purpose="browser", expected_revision=account_row.revision)
            assert error.value.code == "isolation_required"


@pytest.mark.asyncio
async def test_queued_browser_cancel_still_closes_without_saving(db_session):
    db_session.add(Workspace(id="cancel-save-w", name="Cancel save", slug="cancel-save"))
    await db_session.flush()
    resources = await seed_login_resources(db_session)
    account = await service.create_browser_account(db_session, "cancel-save-w", BrowserAccountCreate(workspace_id="cancel-save-w", label="Cancel save", **resources))
    session = await service.create_login_session(db_session, "cancel-save-w", account.id, purpose="browser", expected_revision=account.revision)
    start_id = session.command_id
    cancelled = await service.close_login_session(db_session, "cancel-save-w", account.id, session.id)
    assert cancelled.status == "closed" and cancelled.closed_at is not None
    assert cancelled.command_id == start_id
    assert account.status == "dormant"
    assert (await db_session.get(BrowserDurableCommand, start_id)).status == "cancelled"
    assert len(list(await db_session.scalars(select(BrowserDurableCommand)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_code", ["login_rule_unknown", "runtime_binary_missing", "stale_lease", "capacity_missing", "SECRET https://user:pass@node/private", {"secret": "SECRET"}])
@pytest.mark.parametrize("cleanup_stopped", [False, True])
async def test_startup_diagnostic_survives_fenced_cleanup(db_engine, monkeypatch, startup_code, cleanup_stopped):
    import asyncio
    import json

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="diagnostic-w", name="Diagnostic", slug="diagnostic"))
        await db.flush()
        resources = await seed_login_resources(db)
        account = await service.create_browser_account(
            db, "diagnostic-w",
            BrowserAccountCreate(workspace_id="diagnostic-w", label="Diagnostic", **resources),
        )
        session = await service.create_login_session(db, "diagnostic-w", account.id, expected_revision=account.revision)
        start_id = session.command_id
        await db.commit()
    identity = NodeIdentityV1(node_id="login-node", boot_id="login-boot")
    monkeypatch.setattr(ws_agent_manager, "list_connected", lambda: ["https://node"])
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: True)
    monkeypatch.setattr(ws_agent_manager, "account_connection_identity", lambda url: identity)
    send = AsyncMock(side_effect=[
        {"type": "error", "error_code": startup_code, "message": "SECRET /private/file https://user:pass@node", "result": {"secret": "SECRET"}},
        {"type": "done", "result": {"runtime_status": "stopped"}} if cleanup_stopped else
        {"type": "error", "error_code": "runtime_stop_unconfirmed", "message": "SECRET cleanup"},
    ])
    monkeypatch.setattr(ws_agent_manager, "send_agent_task", send)
    dispatcher = BrowserAccountDispatcher(factory)
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    safe_code = startup_code if isinstance(startup_code, str) and not startup_code.startswith("SECRET") else "capability_missing"
    async with factory() as db:
        start = await db.get(BrowserDurableCommand, start_id)
        current = await db.get(BrowserLoginSession, session.id)
        cleanup_id = current.command_id
        assert cleanup_id != start_id
        assert start.status == "failed"
        assert start.error_code == ("login_rule_unknown" if safe_code == "login_rule_unknown" else "capability_missing")
        assert start.result == {"diagnostic_code": safe_code}
        assert (await db.get(BrowserAccount, account.id)).status_reason_code == safe_code
        assert (await db.get(BrowserDurableCommand, cleanup_id)).status == "queued"
        assert (await db.scalar(select(BrowserAccountLease))).status == "active"
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    assert send.await_count == 2
    async with factory() as db:
        saved_account = await db.get(BrowserAccount, account.id)
        cleanup = await db.get(BrowserDurableCommand, cleanup_id)
        lease = await db.scalar(select(BrowserAccountLease))
        assert saved_account.status_reason_code == safe_code
        assert lease.status == ("released" if cleanup_stopped else "quarantined")
        assert (await db.get(EdgeNodeCapacity, "login-capacity")).occupied_slots == (0 if cleanup_stopped else 1)
        if not cleanup_stopped:
            assert cleanup.result["diagnostic_code"] == "runtime_stop_unconfirmed"
            assert cleanup.result["startup_error_code"] == safe_code
            assert lease.released_at is None
            # Exercise recovery separately after a later terminal cleanup failure.
            cleanup.status = "failed"
            current = await db.get(BrowserLoginSession, session.id)
            current.status = "saving"
            await db.commit()
        persisted = json.dumps([saved_account.status_reason_code, cleanup.result, (await db.get(BrowserDurableCommand, start_id)).result])
        assert "SECRET" not in persisted
        assert "https://" not in persisted
    await dispatcher._mark_failed_sessions()
    async with factory() as db:
        assert (await db.get(BrowserAccount, account.id)).status_reason_code == safe_code


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["challenge", "saving"])
async def test_failed_active_session_converges_to_account_error(db_engine, status):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="failed-challenge-w", name="Failed challenge", slug="failed-challenge"))
        await db.flush()
        resources = await seed_login_resources(db)
        account = await service.create_browser_account(
            db,
            "failed-challenge-w",
            BrowserAccountCreate(
                workspace_id="failed-challenge-w", label="Failed challenge", **resources
            ),
        )
        session = await service.create_login_session(
            db, "failed-challenge-w", account.id, expected_revision=account.revision
        )
        command = await db.get(BrowserDurableCommand, session.command_id)
        session.status = status
        command.status = "failed"
        command.error_code = "auth_required"
        await db.commit()

    await BrowserAccountDispatcher(factory)._mark_failed_sessions()

    async with factory() as db:
        recovered = await db.get(BrowserAccount, account.id)
        recovered_session = await service.get_login_session(
            db, "failed-challenge-w", account.id, session.id
        )
        assert recovered_session.status == "error"
        assert recovered.status == "error"
        assert recovered.status_reason_code == "auth_required"


@pytest.mark.asyncio
async def test_failed_old_session_cannot_overwrite_a_newer_account_state(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="old-result-w", name="Old result", slug="old-result"))
        await db.flush()
        resources = await seed_login_resources(db)
        account = await service.create_browser_account(
            db,
            "old-result-w",
            BrowserAccountCreate(workspace_id="old-result-w", label="Old result", **resources),
        )
        old = await service.create_login_session(
            db, "old-result-w", account.id, expected_revision=account.revision
        )
        old_command = await db.get(BrowserDurableCommand, old.command_id)
        old.status = "error"
        old_command.status = "failed"
        replacement = await service.create_login_session(
            db, "old-result-w", account.id, expected_revision=account.revision
        )
        # Model an old recovery query racing with a committed newer session.
        old.status = "challenge"
        revision = account.revision
        await db.commit()

    await BrowserAccountDispatcher(factory)._mark_failed_sessions()

    async with factory() as db:
        recovered = await db.get(BrowserAccount, account.id)
        recovered_old = await db.get(BrowserLoginSession, old.id)
        recovered_replacement = await db.get(BrowserLoginSession, replacement.id)
        assert recovered_old.status == "error"
        assert recovered_replacement.status == "opening"
        assert recovered.status == "opening"
        assert recovered.status_reason_code is None
        assert recovered.revision == revision


@pytest.mark.asyncio
async def test_expired_challenge_blocks_without_replacing_the_active_session(db_session):
    resources = await seed_login_resources(db_session)
    db_session.add(
        Workspace(id="retry-challenge-w", name="Retry challenge", slug="retry-challenge")
    )
    await db_session.flush()
    account = await service.create_browser_account(
        db_session,
        "retry-challenge-w",
        BrowserAccountCreate(
            workspace_id="retry-challenge-w", label="Retry challenge", **resources
        ),
    )
    expired = await service.create_login_session(
        db_session, "retry-challenge-w", account.id, expected_revision=account.revision
    )
    expired.status = "challenge"
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    with pytest.raises(service.BrowserAccountError) as exc:
        await service.create_login_session(
            db_session, "retry-challenge-w", account.id, expected_revision=account.revision
        )

    assert exc.value.code == "session_expired"
    assert expired.status == "challenge"


@pytest.mark.asyncio
async def test_expired_saving_session_keeps_its_existing_completion_path(db_session):
    resources = await seed_login_resources(db_session)
    db_session.add(Workspace(id="saving-w", name="Saving", slug="saving"))
    await db_session.flush()
    account = await service.create_browser_account(
        db_session,
        "saving-w",
        BrowserAccountCreate(workspace_id="saving-w", label="Saving", **resources),
    )
    saving = await service.create_login_session(
        db_session, "saving-w", account.id, expected_revision=account.revision
    )
    saving.status = "saving"
    saving.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    reused = await service.create_login_session(
        db_session, "saving-w", account.id, expected_revision=account.revision
    )

    assert reused.id == saving.id


@pytest.mark.asyncio
async def test_failed_saving_session_blocks_a_new_login(db_session):
    resources = await seed_login_resources(db_session)
    db_session.add(Workspace(id="failed-saving-w", name="Failed saving", slug="failed-saving"))
    await db_session.flush()
    account = await service.create_browser_account(
        db_session,
        "failed-saving-w",
        BrowserAccountCreate(workspace_id="failed-saving-w", label="Failed saving", **resources),
    )
    failed = await service.create_login_session(
        db_session, "failed-saving-w", account.id, expected_revision=account.revision
    )
    failed.status = "saving"
    command = await db_session.get(BrowserDurableCommand, failed.command_id)
    command.status = "failed"
    await db_session.flush()

    with pytest.raises(service.BrowserAccountError) as exc:
        await service.create_login_session(
            db_session, "failed-saving-w", account.id, expected_revision=account.revision
        )

    assert exc.value.code == "lease_lost"
    assert failed.status == "saving"


@pytest.mark.asyncio
async def test_old_node_boot_blocks_without_replacing_the_active_session(db_session):
    resources = await seed_login_resources(db_session)
    db_session.add(Workspace(id="old-boot-w", name="Old boot", slug="old-boot"))
    await db_session.flush()
    account = await service.create_browser_account(
        db_session,
        "old-boot-w",
        BrowserAccountCreate(workspace_id="old-boot-w", label="Old boot", **resources),
    )
    stale = await service.create_login_session(
        db_session, "old-boot-w", account.id, expected_revision=account.revision
    )
    node = await db_session.get(EdgeNode, stale.node_id)
    node.boot_id = "replacement-boot"
    await db_session.flush()

    with pytest.raises(service.BrowserAccountError) as exc:
        await service.create_login_session(
            db_session, "old-boot-w", account.id, expected_revision=account.revision
        )

    assert exc.value.code == "lease_lost"
    assert stale.status == "opening"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("expired", "session_expired"),
        ("old_boot", "lease_lost"),
        ("other_node_same_boot", "lease_lost"),
    ],
)
async def test_running_leased_session_blocks_replacement_without_staling_its_command(
    db_session, scenario, code
):
    resources = await seed_login_resources(db_session)
    db_session.add(Workspace(id=f"leased-{scenario}-w", name="Leased", slug=f"leased-{scenario}"))
    await db_session.flush()
    account = await service.create_browser_account(
        db_session,
        f"leased-{scenario}-w",
        BrowserAccountCreate(workspace_id=f"leased-{scenario}-w", label="Leased", **resources),
    )
    session = await service.create_login_session(
        db_session, f"leased-{scenario}-w", account.id, expected_revision=account.revision
    )
    command = await db_session.get(BrowserDurableCommand, session.command_id)
    now = datetime.now(UTC)
    session.status = "challenge"
    session.lease_id = f"lease-{scenario}"
    session.epoch = 1
    command.status = "running"
    db_session.add(
        BrowserAccountLease(
            id=f"lease-row-{scenario}",
            workspace_id=account.workspace_id,
            account_id=account.id,
            node_id=session.node_id,
            node_boot_id=session.node_boot_id,
            lease_id=session.lease_id,
            epoch=session.epoch,
            owner_id="test-owner",
            status="active",
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    if scenario == "expired":
        session.expires_at = now - timedelta(seconds=1)
    elif scenario == "old_boot":
        node = await db_session.get(EdgeNode, session.node_id)
        node.boot_id = "new-running-boot"
    else:
        db_session.add(
            EdgeNode(
                id="same-boot-other-node",
                url="https://same-boot-other-node.test",
                status="online",
                account_capable=True,
                boot_id=session.node_boot_id,
            )
        )
        account.node_id = "same-boot-other-node"
    revision = account.revision
    expected_revision = command.expected_revision
    await db_session.flush()

    with pytest.raises(service.BrowserAccountError) as exc:
        await service.create_login_session(
            db_session, account.workspace_id, account.id, expected_revision=revision
        )

    assert exc.value.code == code
    assert account.revision == revision
    assert command.status == "running"
    assert command.expected_revision == expected_revision == revision
    assert session.status == "challenge"
    commands = await db_session.scalars(
        select(BrowserDurableCommand).where(BrowserDurableCommand.account_id == account.id)
    )
    assert len(list(commands)) == 1


@pytest.mark.asyncio
async def test_close_claim_releases_lease_before_a_new_login_can_start(db_session):
    resources = await seed_login_resources(db_session)
    db_session.add(Workspace(id="close-guard-w", name="Close guard", slug="close-guard"))
    await db_session.flush()
    account = await service.create_browser_account(
        db_session,
        "close-guard-w",
        BrowserAccountCreate(workspace_id="close-guard-w", label="Close guard", **resources),
    )
    session = await service.create_login_session(
        db_session, "close-guard-w", account.id, expected_revision=account.revision
    )
    start = await db_session.get(BrowserDurableCommand, session.command_id)
    now = datetime.now(UTC)
    session.status = "presenting"
    session.lease_id = "close-guard-lease"
    session.epoch = 1
    start.status = "running"
    db_session.add(
        BrowserAccountLease(
            id="close-guard-lease-row",
            workspace_id=account.workspace_id,
            account_id=account.id,
            node_id=session.node_id,
            node_boot_id=session.node_boot_id,
            lease_id=session.lease_id,
            epoch=session.epoch,
            owner_id="test-owner",
            status="active",
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    await db_session.flush()
    closed = await service.close_login_session(
        db_session, account.workspace_id, account.id, session.id
    )
    close_command = await db_session.get(BrowserDurableCommand, closed.command_id)
    revision = account.revision
    await db_session.commit()

    with pytest.raises(service.BrowserAccountError) as exc:
        await service.create_login_session(
            db_session, account.workspace_id, account.id, expected_revision=revision
        )
    assert exc.value.code == "isolation_required"
    assert account.revision == revision
    assert close_command.status == "queued"
    assert close_command.expected_revision == revision

    scheduler = BrowserAccountScheduler()
    identity = NodeIdentity(node_id="login-node", boot_id="login-boot", owner_id="test-owner")
    claims = await scheduler.claim(identity, db=db_session)
    assert len(claims) == 1 and claims[0].command_id == close_command.id
    from backend.schemas.browser_account import NodeResultV1

    await scheduler.commit_result(
        identity,
        claims[0],
        NodeResultV1(
            workspace_id=account.workspace_id,
            account_id=account.id,
            command_id=close_command.id,
            session_id=session.id,
            node_id=identity.node_id,
            boot_id=identity.boot_id,
            epoch=claims[0].epoch,
            expected_revision=revision,
            status="stopped",
            evidence={"runtime_status": "stopped"},
        ),
        db=db_session,
    )
    replacement = await service.create_login_session(
        db_session, account.workspace_id, account.id, expected_revision=revision
    )
    assert replacement.id != session.id


@pytest.mark.asyncio
async def test_authenticated_node_dispatch_and_close_at_full_capacity(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="dispatch-w", name="Dispatch", slug="dispatch"))
        await db.flush()
        resources = await seed_login_resources(db)
        node = await db.get(EdgeNode, "login-node")
        node.credential_id = "real-credential-id"
        account = await service.create_browser_account(
            db,
            "dispatch-w",
            BrowserAccountCreate(workspace_id="dispatch-w", label="Dispatch", **resources),
        )
        session = await service.create_login_session(
            db, "dispatch-w", account.id, expected_revision=account.revision
        )
        await db.commit()
    identity = NodeIdentityV1(node_id="login-node", boot_id="login-boot")
    monkeypatch.setattr(ws_agent_manager, "list_connected", lambda: [node.url])
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: True)
    monkeypatch.setattr(ws_agent_manager, "account_connection_identity", lambda url: identity)
    send = AsyncMock(
        side_effect=[
            {
                "type": "done",
                "result": {
                    "runtime_status": "healthy",
                    "profile_id": "dispatch-profile",
                },
            },
            {"type": "done", "result": {"runtime_status": "stopped"}},
        ]
    )
    monkeypatch.setattr(ws_agent_manager, "send_agent_task", send)
    dispatcher = BrowserAccountDispatcher(factory)
    await dispatcher.tick()
    import asyncio

    await asyncio.gather(*list(dispatcher.tasks.values()))
    assert send.await_count == 1
    payload = send.call_args.args[1]
    observation = LoginObservationV1(
        claim=payload["claim"],
        node_identity=identity,
        account_ref={"workspace_id": "dispatch-w", "account_id": account.id},
        session_id=session.id,
        epoch=payload["claim"]["epoch"],
        rule_id="controlled-login-fixture",
        rule_version="1.0.0",
        target={"tab_id": 1, "frame_id": 0, "document_id": "doc", "origin": resources["site"]},
        view_generation=1,
        state="presenting",
        evidence_kind="unknown",
        observed_at=datetime.now(UTC),
    )
    async with factory() as db:
        capacity = await db.get(EdgeNodeCapacity, "login-capacity")
        assert capacity.occupied_slots == 1
        command = await db.get(BrowserDurableCommand, session.command_id)
        assert command.status == "running"
        node_row = await db.get(EdgeNode, "login-node")
        node_row.boot_id = "retired-test"
        with pytest.raises(service.BrowserAccountError):
            await service.apply_login_observation(db, "dispatch-w", account.id, observation)
        node_row.boot_id = "login-boot"
        await service.apply_login_observation(db, "dispatch-w", account.id, observation)
        await service.close_login_session(db, "dispatch-w", account.id, session.id)
        await db.commit()
        with pytest.raises(service.BrowserAccountError):
            await service.apply_login_observation(db, "dispatch-w", account.id, observation)
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    assert send.await_count == 2
    async with factory() as db:
        capacity = await db.get(EdgeNodeCapacity, "login-capacity")
        assert capacity.occupied_slots == 0
        lease = await db.scalar(select(BrowserAccountLease))
        assert lease.status == "released"


@pytest.mark.asyncio
async def test_capacity_cannot_erase_reserved_slot_or_change_boot(db_session):
    resources = await seed_login_resources(db_session)
    now = datetime.now(UTC)
    db_session.add(Workspace(id="reserve-w", name="Reserve", slug="reserve"))
    await db_session.flush()
    db_session.add(
        BrowserAccount(id="reserve-a", workspace_id="reserve-w", site="test", label="Reserve")
    )
    await db_session.flush()
    db_session.add(
        BrowserAccountLease(
            workspace_id="reserve-w",
            account_id="reserve-a",
            node_id="login-node",
            node_boot_id="login-boot",
            lease_id="reserved",
            epoch=1,
            owner_id="owner",
            status="active",
            acquired_at=now,
            renewed_at=now,
            expires_at=now + timedelta(seconds=30),
        )
    )
    await db_session.flush()
    identity = NodeIdentityV1(node_id=resources["node_id"], boot_id="login-boot")
    fact = NodeCapacityFactV1(
        node_id=identity.node_id,
        boot_id=identity.boot_id,
        slot_limit=1,
        occupied_slots=0,
        disk_available=99999,
        observed_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(seconds=30),
    )
    await record_capacity(db_session, identity, fact)
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    assert capacity.occupied_slots == 1
    with pytest.raises(ValueError, match="generation"):
        await record_capacity(db_session, identity, fact.model_copy(update={"boot_id": "old"}))


@pytest.mark.asyncio
async def test_failed_renewal_does_not_block_other_work(monkeypatch):
    dispatcher = BrowserAccountDispatcher()
    dispatcher.running["expired"] = ("https://node", None, None)
    dispatcher.running["live"] = ("https://node", None, None)
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: True)
    monkeypatch.setattr(ws_agent_manager, "list_connected", lambda: [])
    renew = AsyncMock(side_effect=[ValueError("expired"), None])
    monkeypatch.setattr(dispatcher, "_renew_one", renew)
    await dispatcher.tick()
    assert renew.await_count == 2
    assert "expired" not in dispatcher.running
    assert "live" in dispatcher.running


def test_paused_account_allows_only_fenced_completion_commands():
    account = BrowserAccount(
        id="paused-a",
        workspace_id="paused-w",
        site="example.test",
        label="Paused",
        paused=True,
        status="dormant",
    )

    def command(kind):
        return BrowserDurableCommand(
            id=f"command-{kind}",
            workspace_id="paused-w",
            account_id="paused-a",
            kind=kind,
            idempotency_scope=f"scope-{kind}",
            idempotency_key="key",
            expected_revision=0,
            available_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            status="queued",
            session_id="session-a",
            payload={},
        )

    assert BrowserAccountScheduler._command_can_run(
        account, command(BrowserCommandKind.STOP_AND_SAVE.value)
    )
    assert BrowserAccountScheduler._command_can_run(
        account, command(BrowserCommandKind.CLOSE_SESSION.value)
    )
    assert BrowserAccountScheduler._command_can_run(
        account, command(BrowserCommandKind.ISOLATE.value)
    )
    assert not BrowserAccountScheduler._command_can_run(
        account, command(BrowserCommandKind.START_LOGIN.value)
    )
    assert not BrowserAccountScheduler._command_can_run(
        account, command(BrowserCommandKind.EXECUTE_REFERENCE.value)
    )


@pytest.mark.asyncio
async def test_paused_account_stop_and_save_claims_on_original_lease(db_session):
    now = datetime.now(UTC)
    db_session.add(Workspace(id="paused-save-w", name="Paused save", slug="paused-save"))
    await db_session.flush()
    resources = await seed_login_resources(db_session)
    account = await service.create_browser_account(
        db_session,
        "paused-save-w",
        BrowserAccountCreate(
            workspace_id="paused-save-w", label="Paused save", **resources
        ),
    )
    session = await service.create_login_session(
        db_session,
        "paused-save-w",
        account.id,
        purpose="browser",
        expected_revision=account.revision,
    )
    command = await db_session.get(BrowserDurableCommand, session.command_id)
    account.paused = True
    command.kind = BrowserCommandKind.STOP_AND_SAVE.value
    command.payload = {"expected_profile_version": None}
    session.lease_id = "paused-original-lease"
    session.node_id = "login-node"
    session.node_boot_id = "login-boot"
    session.epoch = 7
    db_session.add(
        BrowserAccountLease(
            workspace_id="paused-save-w",
            account_id=account.id,
            node_id="login-node",
            node_boot_id="login-boot",
            lease_id="paused-original-lease",
            epoch=7,
            owner_id="paused-owner",
            status="active",
            acquired_at=now,
            expires_at=now + timedelta(seconds=30),
        )
    )
    await db_session.flush()

    claims = await BrowserAccountScheduler().claim(
        NodeIdentity(
            node_id="login-node", boot_id="login-boot", owner_id="paused-owner"
        ),
        db=db_session,
    )

    assert len(claims) == 1
    assert claims[0].command_id == command.id
    assert claims[0].epoch == 7


@pytest.mark.asyncio
async def test_expired_browser_session_keeps_stop_and_save_lease_alive(
    db_engine, monkeypatch
):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(Workspace(id="stop-renew-w", name="Stop renew", slug="stop-renew"))
        await db.flush()
        resources = await seed_login_resources(db)
        account = await service.create_browser_account(
            db,
            "stop-renew-w",
            BrowserAccountCreate(
                workspace_id="stop-renew-w", label="Stop renew", **resources
            ),
        )
        session = await service.create_login_session(
            db,
            "stop-renew-w",
            account.id,
            purpose="browser",
            expected_revision=account.revision,
        )
        command = await db.get(BrowserDurableCommand, session.command_id)
        command.kind = BrowserCommandKind.STOP_AND_SAVE.value
        command.status = "running"
        command.node_id = "login-node"
        command.epoch = 1
        command.payload = {"expected_profile_version": None}
        session.lease_id = "stop-renew-lease"
        session.node_id = "login-node"
        session.node_boot_id = "login-boot"
        session.epoch = 1
        session.expires_at = now - timedelta(seconds=1)
        db.add(
            BrowserAccountLease(
                workspace_id="stop-renew-w",
                account_id=account.id,
                node_id="login-node",
                node_boot_id="login-boot",
                lease_id="stop-renew-lease",
                epoch=1,
                owner_id="stop-owner",
                status="active",
                acquired_at=now,
                expires_at=now + timedelta(seconds=5),
            )
        )
        await db.commit()
        claim = NodeClaimV1(
            workspace_id="stop-renew-w",
            account_id=account.id,
            command_id=command.id,
            session_id=session.id,
            node_id="login-node",
            boot_id="login-boot",
            epoch=1,
            expected_revision=command.expected_revision,
            claimed_at=now,
            expires_at=now + timedelta(seconds=5),
        )

    send = AsyncMock()
    monkeypatch.setattr(ws_agent_manager, "send_account_lease_renewal", send)
    dispatcher = BrowserAccountDispatcher(factory)
    identity = NodeIdentity(
        node_id="login-node", boot_id="login-boot", owner_id="stop-owner"
    )
    await dispatcher._renew_one(command.id, "https://node", identity, claim)

    assert send.await_count == 1
    renewed = send.await_args.args[1]
    assert renewed.expires_at > claim.expires_at
    async with factory() as db:
        assert (await db.get(BrowserDurableCommand, command.id)).status == "running"


@pytest.mark.asyncio
async def test_stop_and_save_command_deadline_ends_renewal(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(Workspace(id="stop-timeout-w", name="Stop timeout", slug="stop-timeout"))
        await db.flush()
        resources = await seed_login_resources(db)
        account = await service.create_browser_account(
            db,
            "stop-timeout-w",
            BrowserAccountCreate(
                workspace_id="stop-timeout-w", label="Stop timeout", **resources
            ),
        )
        session = await service.create_login_session(
            db,
            "stop-timeout-w",
            account.id,
            purpose="browser",
            expected_revision=account.revision,
        )
        command = await db.get(BrowserDurableCommand, session.command_id)
        command.kind = BrowserCommandKind.STOP_AND_SAVE.value
        command.status = "running"
        command.expires_at = now - timedelta(seconds=1)
        session.expires_at = now - timedelta(seconds=1)
        await db.commit()
        claim = NodeClaimV1(
            workspace_id="stop-timeout-w",
            account_id=account.id,
            command_id=command.id,
            session_id=session.id,
            node_id="login-node",
            boot_id="login-boot",
            epoch=0,
            expected_revision=command.expected_revision,
            claimed_at=now - timedelta(seconds=2),
            expires_at=now + timedelta(seconds=1),
        )

    dispatcher = BrowserAccountDispatcher(factory)
    dispatcher.scheduler.renew = AsyncMock()
    monkeypatch.setattr(ws_agent_manager, "send_account_lease_renewal", AsyncMock())
    await dispatcher._renew_one(command.id, "https://node", None, claim)

    dispatcher.scheduler.renew.assert_not_awaited()
    async with factory() as db:
        expired = await db.get(BrowserDurableCommand, command.id)
        failed_account = await db.get(BrowserAccount, account.id)
        assert expired.status == "expired"
        assert failed_account.status_reason_code == "save_failed"


def test_browser_pre_save_observation_revokes_logout_and_identity_switch(monkeypatch):
    now = datetime.now(UTC)
    claim = NodeClaimV1(
        workspace_id="save-w",
        account_id="save-a",
        command_id="save-command",
        session_id="save-session",
        node_id="save-node",
        boot_id="save-boot",
        epoch=3,
        expected_revision=4,
        claimed_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=30),
    )
    session = SimpleNamespace(
        purpose="browser",
        id="save-session",
        epoch=3,
        login_rule_id="rule",
        login_rule_version="1",
    )
    bundle = SimpleNamespace(name="bundle", version="1", manifest={})
    account = SimpleNamespace(
        platform_identity={"provider": "test", "subject": "user-a"},
        auth_evidence="valid",
        auth_required=False,
        evidence_source="rule_verified",
        evidence_observed_at=now,
        status_reason_code=None,
    )
    monkeypatch.setattr(
        "backend.browser_login_rules.load_bundle_login_rule",
        lambda *_args, **_kwargs: {"allowed_origins": ["https://login.test"]},
    )

    def observation(subject):
        return LoginObservationV1(
            claim=claim,
            node_identity={"node_id": "save-node", "boot_id": "save-boot"},
            account_ref={"workspace_id": "save-w", "account_id": "save-a"},
            session_id="save-session",
            epoch=3,
            rule_id="rule",
            rule_version="1",
            target={
                "tab_id": 1,
                "frame_id": 0,
                "document_id": "doc",
                "origin": "https://login.test",
            },
            view_generation=1,
            state="verifying",
            evidence_kind="valid",
            external_identity={"provider": "test", "subject": subject},
            observed_at=now,
        ).model_dump(mode="json")

    _refresh_browser_auth_from_pre_save_observation(
        account, session, claim, bundle, observation("user-a")
    )
    assert account.auth_evidence == "valid"
    assert account.auth_required is False

    _refresh_browser_auth_from_pre_save_observation(
        account, session, claim, bundle, None
    )
    assert account.auth_evidence == "unknown"
    assert account.auth_required is True

    _refresh_browser_auth_from_pre_save_observation(
        account, session, claim, bundle, observation("user-b")
    )
    assert account.auth_evidence == "invalid"
    assert account.auth_required is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,origin",
    [
        ("bilibili", "https://www.bilibili.com"),
        ("douyin", "https://creator.douyin.com"),
        ("xiaohongshu", "https://www.xiaohongshu.com"),
    ],
)
@pytest.mark.parametrize("valid_manifest", [True, False])
async def test_bundle4_identity_observation_commits_stopped_profile(
    db_engine, monkeypatch, platform, origin, valid_manifest
):
    import asyncio
    import json
    from pathlib import Path

    from backend.models.browser import BrowserLoginSession, BrowserRuntimeBundle

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Workspace(id="save-w", name="Save", slug="save"))
        await db.flush()
        resources = await seed_login_resources(db)
        bundle = await db.get(BrowserRuntimeBundle, "login-bundle")
        bundle.version = "4"
        bundle.manifest = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "chrome/runtime-bundles/opencli-default/4/manifest.json"
            ).read_text(encoding="utf-8")
        )
        rule_version = "0.2.0" if platform == "xiaohongshu" else "0.1.0"
        resources.update(
            site=f"{platform}.com", login_rule_id=f"{platform}-qr", login_rule_version=rule_version
        )
        account = await service.create_browser_account(
            db,
            "save-w",
            {
                "workspace_id": "save-w",
                "label": "Save",
                **resources,
            },
        )
        session = await service.create_login_session(
            db, "save-w", account.id, expected_revision=account.revision
        )
        await db.commit()
    identity = NodeIdentityV1(node_id="login-node", boot_id="login-boot")
    monkeypatch.setattr(ws_agent_manager, "list_connected", lambda: ["https://node"])
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: True)
    monkeypatch.setattr(ws_agent_manager, "account_connection_identity", lambda url: identity)

    async def reply(url, payload, **kwargs):
        if payload["command"]["kind"] == "start_login":
            return {"type": "done", "result": {"runtime_status": "healthy"}}
        return {
            "type": "done",
            "result": {
                "runtime_status": "stopped",
                "profile_manifest": {
                    "workspace_id": "save-w",
                    "account_id": account.id,
                    "profile_id": "saved-profile",
                    "version": 1 if valid_manifest else 0,
                    "node_id": identity.node_id,
                    "command_id": payload["command"]["command_id"],
                    "writer_epoch": payload["claim"]["epoch"],
                    "bundle": "opencli-default:4",
                    "browser_version": "test",
                    "files_count": 1,
                    "total_bytes": 20,
                    "checksum_manifest_ref": "checksums.json",
                    "complete_marker": "complete.marker",
                    "committed_at": datetime.now(UTC).isoformat(),
                    "password_inventory_status": "not_present",
                },
            },
        }

    send = AsyncMock(side_effect=reply)
    monkeypatch.setattr(ws_agent_manager, "send_agent_task", send)
    dispatcher = BrowserAccountDispatcher(factory)
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    payload = send.call_args.args[1]
    observation = LoginObservationV1(
        claim=payload["claim"],
        node_identity=identity,
        account_ref={"workspace_id": "save-w", "account_id": account.id},
        session_id=session.id,
        epoch=payload["claim"]["epoch"],
        rule_id=f"{platform}-qr",
        rule_version=rule_version,
        target={"tab_id": 1, "frame_id": 0, "document_id": "official-doc", "origin": origin},
        view_generation=1,
        state="verifying",
        evidence_kind="valid",
        external_identity={
            "provider": platform,
            "subject": "a" * 24 if platform == "xiaohongshu" else "12345",
        },
        observed_at=datetime.now(UTC),
    )
    async with factory() as db:
        await service.apply_login_observation(db, "save-w", account.id, observation)
        await db.commit()
    await dispatcher.tick()
    await asyncio.gather(*list(dispatcher.tasks.values()))
    async with factory() as db:
        saved = await db.get(BrowserAccount, account.id)
        if not valid_manifest:
            assert saved.status == "error"
            assert saved.status_reason_code == "save_failed"
            assert saved.profile_manifest_id is None
            assert (await db.get(EdgeNodeCapacity, "login-capacity")).occupied_slots == 0
            return
        assert saved.status == "saved"
        assert saved.profile_manifest_id
        assert saved.profile_version == 1
        assert (await db.get(BrowserLoginSession, session.id)).profile_state == "committed"
        assert (await db.get(EdgeNodeCapacity, "login-capacity")).occupied_slots == 0
