"""Drive durable browser commands over authenticated, generation-bound sockets."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from backend import ws_agent_manager
from backend.database import AsyncSessionLocal
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserAuthEvidence,
    BrowserDurableCommand,
    BrowserEvidenceSource,
    BrowserLoginSession,
    BrowserProfileManifest,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.schemas.browser_account import (
    BrowserAccountErrorCode,
    LoginObservationV1,
    NodeCapacityFactV1,
    NodeIdentityV1,
    NodeResultV1,
    ProfileManifestV1,
)
from backend.services.browser_account_scheduler import (
    BrowserAccountScheduler,
    NodeIdentity,
    command_contract,
)
from backend.services.browser_account_service import (
    BrowserAccountError,
    session_envelope,
)
from backend.services.browser_account_session_contract import ACTIVE_BROWSER_SESSION_STATUSES

logger = logging.getLogger(__name__)


# Only audited codes cross the node boundary; never persist node messages or paths.
_RUNTIME_DIAGNOSTIC_CODES = frozenset({
    "capability_missing",
    "capacity_missing",
    "claim_expired",
    "credential_policy_unverified",
    "duplicate_claim",
    "epoch_invalid",
    "epoch_lock_unavailable",
    "epoch_state_corrupt",
    "epoch_state_invalid",
    "identifier_invalid",
    "lease_deadline_invalid",
    "lease_lost",
    "lease_owner_invalid",
    "login_rule_unknown",
    "metadata_invalid",
    "node_identity_mismatch",
    "password_database_unlisted",
    "password_inventory_blocked",
    "password_inventory_changed",
    "password_inventory_unbounded",
    "password_manifest_mismatch",
    "password_manifest_unverified",
    "path_invalid",
    "path_permissions",
    "path_symlink",
    "portal_record_missing",
    "portal_registration_invalid",
    "profile_dirty",
    "profile_file_invalid",
    "profile_identity_mismatch",
    "profile_locked",
    "profile_manifest_incompatible",
    "profile_manifest_invalid",
    "profile_missing",
    "profile_not_stopped",
    "profile_path_invalid",
    "profile_state_overlap",
    "profile_symlink",
    "profile_version_ambiguous",
    "profile_version_invalid",
    "profile_version_missing",
    "result_claim_mismatch",
    "runtime_binary_missing",
    "runtime_bundle_incompatible",
    "runtime_bundle_unavailable",
    "runtime_command_invalid",
    "runtime_configuration_invalid",
    "runtime_context_invalid",
    "runtime_endpoint_invalid",
    "runtime_platform_unsupported",
    "runtime_process_exited",
    "runtime_readiness_timeout",
    "runtime_save_invalid",
    "runtime_state_corrupt",
    "runtime_state_invalid",
    "runtime_stop_unconfirmed",
    "runtime_supervisor_running",
    "session_claim_mismatch",
    "stale_boot",
    "stale_claim",
    "stale_epoch",
    "stale_lease",
    "stale_owner",
    "stale_portal_registration",
    "stale_runtime_binding",
    "state_path_invalid",
}) | frozenset(code.value for code in BrowserAccountErrorCode)


def _safe_diagnostic_code(value, fallback="capability_missing"):
    return value if isinstance(value, str) and value in _RUNTIME_DIAGNOSTIC_CODES else fallback


def _contract_error_code(code):
    return code if code in BrowserAccountErrorCode._value2member_map_ else "capability_missing"


async def _startup_failure_code(db, command):
    # Scope provenance to this session, never inherit another session's account reason.
    start = await db.scalar(
        select(BrowserDurableCommand)
        .where(
            BrowserDurableCommand.workspace_id == command.workspace_id,
            BrowserDurableCommand.account_id == command.account_id,
            BrowserDurableCommand.session_id == command.session_id,
            BrowserDurableCommand.kind == "start_login",
            BrowserDurableCommand.status == "failed",
        )
        .order_by(BrowserDurableCommand.created_at.desc(), BrowserDurableCommand.id.desc())
        .limit(1)
    )
    if start is not None and isinstance(start.result, dict):
        return _safe_diagnostic_code(start.result.get("diagnostic_code"), None)
    return None


def _refresh_browser_auth_from_pre_save_observation(
    account, session, claim, bundle, raw
) -> None:
    if session.purpose != "browser":
        return
    account.auth_evidence = BrowserAuthEvidence.UNKNOWN.value
    account.auth_required = True
    account.evidence_source = None
    account.evidence_observed_at = None
    if not isinstance(raw, dict):
        return
    try:
        observation = LoginObservationV1.model_validate(raw)
    except (TypeError, ValueError):
        return
    now = datetime.now(UTC)
    claim_fields = (
        "workspace_id",
        "account_id",
        "command_id",
        "session_id",
        "node_id",
        "boot_id",
        "epoch",
        "expected_revision",
    )
    if (
        observation.session_id != session.id
        or observation.epoch != session.epoch
        or observation.rule_id != session.login_rule_id
        or observation.rule_version != session.login_rule_version
        or observation.node_identity.node_id != claim.node_id
        or observation.node_identity.boot_id != claim.boot_id
        or any(
            getattr(observation.claim, field) != getattr(claim, field)
            for field in claim_fields
        )
        or not (
            now - timedelta(seconds=15)
            <= _utc(observation.observed_at)
            <= now + timedelta(seconds=2)
        )
    ):
        return
    from backend.browser_login_rules import load_bundle_login_rule

    rule = load_bundle_login_rule(
        Path(__file__).resolve().parents[2],
        bundle_name=bundle.name,
        bundle_version=bundle.version,
        bundle_manifest=bundle.manifest,
        rule_id=observation.rule_id,
        rule_version=observation.rule_version,
    )
    if rule is None or observation.target.origin not in set(
        rule.get("allowed_origins", [])
    ) | set(rule.get("allowed_redirect_origins", [])):
        return
    if observation.evidence_kind.value != "valid" or observation.external_identity is None:
        if observation.evidence_kind.value == "invalid":
            account.auth_evidence = BrowserAuthEvidence.INVALID.value
        return
    identity = observation.external_identity.to_wire()
    if account.platform_identity is not None and account.platform_identity != identity:
        account.auth_evidence = BrowserAuthEvidence.INVALID.value
        account.status_reason_code = "account_identity_mismatch"
        return
    account.platform_identity = identity
    account.auth_evidence = BrowserAuthEvidence.VALID.value
    account.auth_required = False
    account.evidence_source = BrowserEvidenceSource.RULE_VERIFIED.value
    account.evidence_observed_at = observation.observed_at
    account.status_reason_code = None


async def _persist_saved_profile(
    db, claim, session, raw, *, pre_save_login_observation=None
):
    if not isinstance(raw, dict):
        raise ValueError("stopped browser did not return a profile manifest")
    manifest = ProfileManifestV1.model_validate(
        {key: raw[key] for key in ProfileManifestV1.model_fields if key in raw}
    )
    account = await db.get(BrowserAccount, claim.account_id, with_for_update=True)
    if (
        manifest.workspace_id,
        manifest.account_id,
        manifest.node_id,
        manifest.command_id,
        manifest.writer_epoch,
    ) != (
        claim.workspace_id,
        claim.account_id,
        claim.node_id,
        claim.command_id,
        claim.epoch,
    ):
        raise ValueError("profile snapshot does not match the stopped writer")
    if account is None:
        raise ValueError("saving requires independently verified authentication")
    bundle = await db.get(BrowserRuntimeBundle, account.runtime_bundle_id)
    if bundle is None:
        raise ValueError("profile snapshot bundle mismatch")
    _refresh_browser_auth_from_pre_save_observation(
        account,
        session,
        claim,
        bundle,
        pre_save_login_observation,
    )
    verified_identity = bool(
        account.auth_evidence == "valid" and not account.auth_required
    )
    if not verified_identity and session.purpose != "browser":
        raise ValueError("saving requires independently verified authentication")
    if (account.profile_id is not None and manifest.profile_id != account.profile_id) or (
        account.profile_version is not None and manifest.version <= account.profile_version
    ):
        raise ValueError("profile snapshot lineage is stale")
    if manifest.password_inventory_status.value not in {"not_present", "verified"}:
        raise ValueError("profile password inventory was not approved")
    if manifest.bundle != f"{bundle.name}:{bundle.version}":
        raise ValueError("profile snapshot bundle mismatch")
    values = manifest.model_dump(mode="python")
    values["bundle_name"] = values.pop("bundle")
    row = BrowserProfileManifest(**values, state="committed")
    db.add(row)
    await db.flush()
    account.profile_manifest_id = row.id
    account.profile_id = manifest.profile_id
    account.profile_version = manifest.version
    session.status = "saved"
    session.closed_at = datetime.now(UTC)
    session.revision += 1
    account.status = "saved" if verified_identity else "dormant"
    account.revision += 1
    session.profile_id = manifest.profile_id
    session.profile_version = manifest.version
    session.profile_state = "committed"
    return row


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def record_capacity(db, identity: NodeIdentityV1, fact: NodeCapacityFactV1):
    now = datetime.now(UTC)
    if (fact.node_id, fact.boot_id) != (identity.node_id, identity.boot_id):
        raise ValueError("capacity generation does not match authenticated node")
    if not (now - timedelta(seconds=60) <= fact.observed_at <= now + timedelta(seconds=10)):
        raise ValueError("capacity observation is not fresh")
    if not (now < fact.expires_at <= now + timedelta(seconds=120)):
        raise ValueError("capacity deadline is not bounded")
    node = await db.get(EdgeNode, identity.node_id, with_for_update=True)
    boot = await db.scalar(
        select(EdgeNodeBoot).where(
            EdgeNodeBoot.node_id == identity.node_id,
            EdgeNodeBoot.boot_id == identity.boot_id,
            EdgeNodeBoot.status == "active",
        )
    )
    if node is None or boot is None or node.boot_id != identity.boot_id or node.quarantined:
        raise ValueError("capacity node boot is not current")
    row = await db.scalar(
        select(EdgeNodeCapacity)
        .where(
            EdgeNodeCapacity.node_id == identity.node_id,
            EdgeNodeCapacity.boot_id == identity.boot_id,
        )
        .with_for_update()
    )
    if row is not None and _utc(row.observed_at) >= fact.observed_at:
        return
    reserved = await db.scalar(
        select(func.count())
        .select_from(BrowserAccountLease)
        .where(
            BrowserAccountLease.node_id == identity.node_id,
            BrowserAccountLease.node_boot_id == identity.boot_id,
            BrowserAccountLease.status.in_(["active", "quarantined"]),
        )
    )
    occupied = max(fact.occupied_slots, reserved or 0)
    values = fact.to_wire()
    values.pop("node_id")
    values.pop("boot_id")
    # JSON wire timestamps become database datetime values here.
    values.update(
        observed_at=fact.observed_at,
        expires_at=fact.expires_at,
        occupied_slots=min(occupied, fact.slot_limit),
        valid=occupied <= fact.slot_limit,
    )
    if row is None:
        row = EdgeNodeCapacity(node_id=identity.node_id, boot_id=identity.boot_id, **values)
        db.add(row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    node.capacity_revision += 1
    node.last_seen_at = now
    await db.flush()


class BrowserAccountDispatcher:
    def __init__(self, session_factory=AsyncSessionLocal):
        self.session_factory = session_factory
        self.scheduler = BrowserAccountScheduler(session_factory=session_factory)
        self.tasks: dict[str, asyncio.Task] = {}
        self.running = {}
        self.stop_event = asyncio.Event()

    async def _envelope(self, db, claim):
        command = await db.get(BrowserDurableCommand, claim.command_id)
        account = await db.get(BrowserAccount, claim.account_id)
        session = await db.get(BrowserLoginSession, claim.session_id)
        lease = await db.scalar(
            select(BrowserAccountLease).where(
                BrowserAccountLease.workspace_id == claim.workspace_id,
                BrowserAccountLease.account_id == claim.account_id,
                BrowserAccountLease.node_id == claim.node_id,
                BrowserAccountLease.node_boot_id == claim.boot_id,
                BrowserAccountLease.epoch == claim.epoch,
                BrowserAccountLease.status == "active",
            )
        )
        if command is None or account is None or session is None or lease is None:
            raise ValueError("claimed account session is incomplete")
        if session.command_id != command.id:
            raise ValueError("claimed command was superseded")
        account.node_id = claim.node_id
        session.node_id = claim.node_id
        session.node_boot_id = claim.boot_id
        session.lease_id = lease.lease_id
        session.epoch = claim.epoch
        envelope = (await session_envelope(account, session)).model_copy(
            update={"lease_expires_at": claim.expires_at},
        )
        return command, session, envelope

    async def tick(self):
        for key, (url, identity, claim) in list(self.running.items()):
            if not ws_agent_manager.is_connected(url):
                self.running.pop(key, None)
                continue
            try:
                await self._renew_one(key, url, identity, claim)
            except Exception:
                self.running.pop(key, None)
                logger.warning("Browser lease renewal stopped command_id=%s", key, exc_info=True)

        for url in ws_agent_manager.list_connected():
            authenticated = ws_agent_manager.account_connection_identity(url)
            if authenticated is None:
                continue
            async with self.session_factory() as db:
                node = await db.get(EdgeNode, authenticated.node_id)
                if node is None or node.boot_id != authenticated.boot_id:
                    continue
                identity = NodeIdentity(
                    authenticated.node_id,
                    authenticated.boot_id,
                    f"account-ws:{authenticated.node_id}:{authenticated.boot_id}",
                    node.credential_id,
                )
                claims = await self.scheduler.claim(identity, limit=10, db=db)
                envelopes = []
                for claim in claims:
                    try:
                        command, _, envelope = await self._envelope(db, claim)
                        envelopes.append((claim, command_contract(command), envelope))
                    except (ValueError, BrowserAccountError):
                        command = await db.get(BrowserDurableCommand, claim.command_id)
                        command.status = "failed"
                        command.error_code = "capability_missing"
                        command.completed_at = datetime.now(UTC)
                        # No RPC has been sent for this claim. Preserve any writer reservation.
                        logger.warning("Account command envelope rejected id=%s", claim.command_id)
                await db.commit()
            for claim, command, envelope in envelopes:
                self.running[claim.command_id] = (url, identity, claim)
                task = asyncio.create_task(self._dispatch(url, identity, claim, command, envelope))
                self.tasks[claim.command_id] = task
                task.add_done_callback(lambda _, key=claim.command_id: self.tasks.pop(key, None))

    async def _renew_one(self, key, url, identity, claim):
        async with self.session_factory() as db:
            session = await db.get(BrowserLoginSession, claim.session_id)
            command = await db.get(BrowserDurableCommand, key)
            if session is None or session.command_id != key:
                old = command
                if old is not None and old.status in {"claimed", "running"}:
                    old.status = "cancelled"
                    old.completed_at = datetime.now(UTC)
                await db.commit()
                self.running.pop(key, None)
                return
            if command is None:
                self.running.pop(key, None)
                return
            now = datetime.now(UTC)
            completion_command = command.kind in {
                "stop_and_save",
                "close_session",
                "isolate",
            }
            if completion_command and _utc(command.expires_at) <= now:
                command.status = "expired"
                command.error_code = "session_expired"
                command.completed_at = now
                session.status = "error"
                session.revision += 1
                account = await db.get(BrowserAccount, claim.account_id)
                if account is not None:
                    account.status = "error"
                    account.status_reason_code = (
                        await _startup_failure_code(db, command) or "save_failed"
                    )
                    account.revision += 1
                await db.commit()
                self.running.pop(key, None)
                return
            if (
                not completion_command
                and session.expires_at
                and _utc(session.expires_at) <= now
            ):
                from backend.services.browser_account_service import close_login_session

                await close_login_session(
                    db, claim.workspace_id, claim.account_id, claim.session_id, reason="expired"
                )
                await db.commit()
                return
            if _utc(claim.expires_at) - now > timedelta(seconds=20):
                return
            renewed = await self.scheduler.renew(identity, claim, db=db)
            _, _, envelope = await self._envelope(db, renewed)
            await db.commit()
        await ws_agent_manager.send_account_lease_renewal(url, renewed, envelope)
        self.running[key] = (url, identity, renewed)

    async def _dispatch(self, url, identity, claim, command, envelope):
        try:
            response = await ws_agent_manager.send_agent_task(
                url,
                {
                    "runtime": "opencli",
                    "command": command.to_wire(),
                    "claim": claim.to_wire(),
                    "session": envelope.to_wire(),
                    "node_identity": {
                        "node_id": claim.node_id,
                        "boot_id": claim.boot_id,
                    },
                },
                on_event=lambda event: None,
                timeout=120,
            )
            response = response if isinstance(response, dict) else {}
            runtime_result = response.get("result")
            runtime_result = runtime_result if isinstance(runtime_result, dict) else {}
            healthy = (
                response.get("type") == "done"
                and runtime_result.get("runtime_status") == "healthy"
            )
            stopped = (
                response.get("type") == "done"
                and runtime_result.get("runtime_status") == "stopped"
            )
            async with self.session_factory() as db:
                row = await db.get(BrowserDurableCommand, claim.command_id, with_for_update=True)
                session = await db.get(BrowserLoginSession, claim.session_id)
                if row is None or session is None or session.command_id != claim.command_id:
                    return
                lease = await db.scalar(
                    select(BrowserAccountLease)
                    .where(
                        BrowserAccountLease.lease_id == session.lease_id,
                        BrowserAccountLease.status == "active",
                        BrowserAccountLease.node_id == claim.node_id,
                        BrowserAccountLease.node_boot_id == claim.boot_id,
                        BrowserAccountLease.epoch == claim.epoch,
                    )
                    .with_for_update()
                )
                if (
                    lease is None
                    or _utc(lease.expires_at) <= datetime.now(UTC)
                    or row.status not in {"claimed", "running"}
                    or ws_agent_manager.account_connection_identity(url)
                    != NodeIdentityV1(
                        node_id=claim.node_id,
                        boot_id=claim.boot_id,
                    )
                ):
                    raise ValueError("runtime reply no longer owns an active lease")
                if command.kind.value == "start_login" and healthy:
                    row.status = "running"
                    profile_id = runtime_result.get("profile_id")
                    if not isinstance(profile_id, str) or not profile_id:
                        raise ValueError("healthy runtime reply omitted its profile identity")
                    row.result = {
                        "runtime_status": "healthy",
                        "profile_id": profile_id,
                    }
                    session.profile_id = profile_id
                    if session.profile_state == "new":
                        session.profile_state = "uncommitted"
                elif command.kind.value == "start_login":
                    from backend.services.browser_account_service import close_login_session

                    # Startup can fail after Chrome is already running. Stop that same
                    # leased writer through the durable queue before releasing its slot.
                    row.status = "failed"
                    diagnostic_code = _safe_diagnostic_code(response.get("error_code"))
                    row.error_code = _contract_error_code(diagnostic_code)
                    row.result = {"diagnostic_code": diagnostic_code}
                    row.completed_at = datetime.now(UTC)
                    await close_login_session(
                        db, claim.workspace_id, claim.account_id, claim.session_id, reason="error"
                    )
                    session.status = "error"
                    session.revision += 1
                    account = await db.get(BrowserAccount, claim.account_id)
                    account.status = "error"
                    account.status_reason_code = diagnostic_code
                else:
                    diagnostic_code = _safe_diagnostic_code(response.get("error_code"))
                    startup_code = await _startup_failure_code(db, row)
                    result = NodeResultV1(
                        **{
                            key: getattr(claim, key)
                            for key in (
                                "workspace_id",
                                "account_id",
                                "command_id",
                                "session_id",
                                "node_id",
                                "boot_id",
                                "epoch",
                                "expected_revision",
                            )
                        },
                        status="succeeded" if stopped else "blocked",
                        error_code=None if stopped else _contract_error_code(diagnostic_code),
                        evidence={"runtime_status": "stopped" if stopped else "unavailable"},
                    )
                    await self.scheduler.commit_result(identity, claim, result, db=db)
                    if stopped and command.kind.value == "stop_and_save":
                        from sqlalchemy.exc import IntegrityError

                        try:
                            async with db.begin_nested():
                                await _persist_saved_profile(
                                    db,
                                    claim,
                                    session,
                                    response.get("result", {}).get("profile_manifest"),
                                    pre_save_login_observation=response.get("result", {}).get(
                                        "pre_save_login_observation"
                                    ),
                                )
                        except (ValueError, TypeError, IntegrityError):
                            # The authenticated node already proved this writer stopped.
                            # Keep that release, while rejecting only the invalid snapshot.
                            account = await db.get(BrowserAccount, claim.account_id)
                            session.status = account.status = "error"
                            session.revision += 1
                            account.status_reason_code = startup_code or "save_failed"
                            account.revision += 1
                            row.status = "failed"
                            row.error_code = "save_failed"
                            row.result = result.to_wire() | {
                                "status": "failed",
                                "error_code": "save_failed",
                            }
                    if not stopped:
                        row.result = result.to_wire() | {"diagnostic_code": diagnostic_code}
                        if startup_code:
                            row.result = row.result | {"startup_error_code": startup_code}
                        session.status = "error"
                        session.revision += 1
                        account = await db.get(BrowserAccount, claim.account_id)
                        account.status = "error"
                        account.status_reason_code = startup_code or diagnostic_code
                        account.revision += 1
                    self.running.pop(claim.command_id, None)
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Uncertain effects retain their lease; expiry quarantines the writer.
            self.running.pop(claim.command_id, None)
            logger.exception("Browser account dispatch failed command_id=%s", claim.command_id)

    async def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Browser account dispatch cycle failed")
                try:
                    await self.scheduler.recover(batch_limit=10)
                    await self._mark_failed_sessions()
                except Exception:
                    logger.exception("Browser account recovery cycle failed")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=2)
                except TimeoutError:
                    pass
        finally:
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _mark_failed_sessions(self):
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(BrowserLoginSession.id, BrowserDurableCommand.id)
                    .join(
                        BrowserDurableCommand,
                        BrowserLoginSession.command_id == BrowserDurableCommand.id,
                    )
                    .where(
                        BrowserDurableCommand.status.in_(["failed", "expired"]),
                        BrowserLoginSession.status.in_(ACTIVE_BROWSER_SESSION_STATUSES),
                    )
                    .limit(10)
                )
            ).all()
        for session_id, command_id in rows:
            async with self.session_factory() as db:
                # Keep this order aligned with recovery: command, account, session.
                command = await db.scalar(
                    select(BrowserDurableCommand)
                    .where(BrowserDurableCommand.id == command_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if command is None or command.status not in {"failed", "expired"}:
                    continue
                account = await db.scalar(
                    select(BrowserAccount)
                    .where(
                        BrowserAccount.workspace_id == command.workspace_id,
                        BrowserAccount.id == command.account_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                session = await db.scalar(
                    select(BrowserLoginSession)
                    .where(
                        BrowserLoginSession.workspace_id == command.workspace_id,
                        BrowserLoginSession.id == session_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    account is None
                    or session is None
                    or session.account_id != account.id
                    or session.command_id != command.id
                    or session.status not in ACTIVE_BROWSER_SESSION_STATUSES
                ):
                    continue
                session.status = "error"
                session.revision += 1
                latest = await db.scalar(
                    select(BrowserLoginSession.id)
                    .where(
                        BrowserLoginSession.workspace_id == session.workspace_id,
                        BrowserLoginSession.account_id == session.account_id,
                    )
                    .order_by(BrowserLoginSession.created_at.desc(), BrowserLoginSession.id.desc())
                    .limit(1)
                )
                if latest == session.id:
                    account.status = "error"
                    account.status_reason_code = (
                        await _startup_failure_code(db, command)
                        or _safe_diagnostic_code(command.error_code, "lease_lost")
                    )
                    account.revision += 1
                await db.commit()
