from datetime import datetime, timedelta, timezone

import pytest

from backend.models.browser import BrowserAccountStatus, BrowserAuthEvidence, BrowserCommandKind
from backend.schemas.browser_account import (
    AccountRef,
    AccountStateSnapshotV1,
    AccountStateViewV1,
    CommandExecutionGuardV1,
    DurableCommandV1,
    ExternalIdentityV1,
    LoginObservationV1,
    LoginRuleCommandPayloadV1,
    NodeClaimV1,
    NodeIdentityV1,
    PortalClipV1,
    PortalEntryBlockedV1,
    PortalEntryWaitingV1,
    PortalOwnerRouteV1,
    PortalPixelFrameV1,
    PortalRegionFocusV1,
    PortalTicketGrantV1,
    SensitiveSessionBindingV1,
    SessionEnvelopeV1,
    SessionTargetV1,
)


def _context() -> tuple[datetime, AccountRef, SessionTargetV1, SessionEnvelopeV1, DurableCommandV1, NodeClaimV1]:
    now = datetime.now(timezone.utc)
    ref = AccountRef(workspace_id="workspace", account_id="account")
    target = SessionTargetV1(
        tab_id="tab",
        frame_id="frame",
        document_id="document",
        origin="https://site.test",
    )
    session = SessionEnvelopeV1(
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        session_id="session",
        node_id="node",
        node_boot_id="boot",
        lease_id="lease",
        epoch=2,
        lease_expires_at=now + timedelta(minutes=5),
        runtime_bundle_id="bundle",
        runtime_bundle_version="1",
        login_rule_id="rule",
        login_rule_version="1",
        target=target,
        view_generation=4,
        purpose="login",
        command_id="command",
    )
    command = DurableCommandV1(
        command_id="command",
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        node_id="node",
        kind=BrowserCommandKind.APPLY_LOGIN_RULE,
        idempotency_scope="workspace:caller",
        idempotency_key="key",
        epoch=2,
        expected_revision=7,
        available_at=now,
        expires_at=now + timedelta(minutes=1),
        session_id="session",
        payload=LoginRuleCommandPayloadV1(login_rule_id="rule", login_rule_version="1"),
    )
    claim = NodeClaimV1(
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        command_id="command",
        session_id="session",
        node_id="node",
        boot_id="boot",
        epoch=2,
        expected_revision=7,
        claimed_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    return now, ref, target, session, command, claim


def test_h1_freezes_command_and_rejects_stale_guard_before_side_effects() -> None:
    now, ref, target, session, command, claim = _context()
    with pytest.raises(ValueError):
        DurableCommandV1(
            contract_version=2,
            command_id="unsupported",
            workspace_id=ref.workspace_id,
            account_id=ref.account_id,
            kind=BrowserCommandKind.START_LOGIN,
            idempotency_scope="scope",
            idempotency_key="key",
            epoch=0,
            expected_revision=0,
            available_at=now,
            expires_at=now + timedelta(seconds=1),
        )
    CommandExecutionGuardV1(command=command, claim=claim, session=session)
    with pytest.raises(ValueError):
        CommandExecutionGuardV1(
            command=command,
            claim=claim.model_copy(update={"workspace_id": "other"}),
            session=session,
        )
    with pytest.raises(ValueError):
        CommandExecutionGuardV1(
            command=command.model_copy(
                update={"payload": LoginRuleCommandPayloadV1(login_rule_id="rule", login_rule_version="2")}
            ),
            claim=claim,
            session=session,
        )


def test_h2_state_view_requires_same_trusted_snapshot() -> None:
    now, ref, target, _, _, _ = _context()
    identity = ExternalIdentityV1(provider="site", subject="subject")
    snapshot = AccountStateSnapshotV1(
        account_ref=ref,
        account_revision=7,
        session_id="session",
        session_revision=3,
        epoch=2,
        trusted_identity=identity,
        auth_evidence=BrowserAuthEvidence.VALID,
        status=BrowserAccountStatus.VERIFYING,
        target=target,
        rule_id="rule",
        rule_version="1",
        view_generation=4,
        validated_at=now,
        freshness_deadline=now + timedelta(seconds=1),
    )
    AccountStateViewV1(
        snapshot=snapshot,
        account_ref=ref,
        account_revision=7,
        session_id="session",
        session_revision=3,
        trusted_identity=identity,
        auth_evidence=BrowserAuthEvidence.VALID,
        status=BrowserAccountStatus.VERIFYING,
        session_epoch=2,
        target=target,
        rule_id="rule",
        rule_version="1",
        view_generation=4,
        validated_at=now,
        freshness_deadline=now + timedelta(seconds=1),
    )
    with pytest.raises(ValueError):
        AccountStateViewV1(
            snapshot=snapshot,
            account_ref=ref,
            account_revision=8,
            session_id="session",
            session_revision=3,
            trusted_identity=identity,
            auth_evidence=BrowserAuthEvidence.VALID,
            status=BrowserAccountStatus.VERIFYING,
            session_epoch=2,
            target=target,
            rule_id="rule",
            rule_version="1",
            view_generation=4,
            validated_at=now,
            freshness_deadline=now + timedelta(seconds=1),
        )


def test_h3_observation_requires_claim_and_authenticated_node_linkage() -> None:
    now, ref, target, _, _, claim = _context()
    observation = LoginObservationV1(
        claim=claim,
        node_identity=NodeIdentityV1(node_id="node", boot_id="boot"),
        account_ref=ref,
        session_id="session",
        epoch=2,
        rule_id="rule",
        rule_version="1",
        target=target,
        view_generation=4,
        state="verifying",
        evidence_kind=BrowserAuthEvidence.VALID,
        external_identity=ExternalIdentityV1(provider="site", subject="subject"),
        observed_at=now,
    )
    assert observation.claim.workspace_id == ref.workspace_id
    with pytest.raises(ValueError):
        LoginObservationV1(
            claim=claim.model_copy(update={"account_id": "other"}),
            node_identity=NodeIdentityV1(node_id="node", boot_id="boot"),
            account_ref=ref,
            session_id="session",
            epoch=2,
            rule_id="rule",
            rule_version="1",
            target=target,
            view_generation=4,
            state="verifying",
            evidence_kind=BrowserAuthEvidence.VALID,
            external_identity=ExternalIdentityV1(provider="site", subject="subject"),
            observed_at=now,
        )


def test_h4_ticket_entry_responses_cannot_be_mistaken_for_grants() -> None:
    now, ref, _, _, _, _ = _context()
    waiting = PortalEntryWaitingV1(
        account_ref=ref,
        session_id="session",
        session_revision=3,
        reason="capacity_missing",
    )
    blocked = PortalEntryBlockedV1(
        account_ref=ref,
        session_id="session",
        session_revision=3,
        error_code="capability_missing",
    )
    grant = PortalTicketGrantV1(
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        session_id="session",
        session_revision=3,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        hard_expires_at=now + timedelta(minutes=10),
        cookie_name="portal",
        websocket_path="/api/v1/portal",
    )
    assert waiting.status == "waiting"
    assert blocked.status == "blocked"
    assert grant.status == "granted"
    assert waiting.status != grant.status and blocked.status != grant.status


def test_h5_pixel_mime_bytes_and_owner_route_are_bounded() -> None:
    now, ref, target, _, _, _ = _context()
    raw = b"\x89PNG\r\n\x1a\n"
    frame = PortalPixelFrameV1(
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        session_id="session",
        epoch=2,
        target=target,
        view_generation=4,
        sequence=1,
        region_kind="form",
        mime_type="image/png",
        expires_at=now + timedelta(seconds=5),
        clip=PortalClipV1(x=0, y=0, width=10, height=10),
        byte_length=len(raw),
        frame_bytes=raw,
    )
    focus = PortalRegionFocusV1(
        target=target,
        view_generation=4,
        region_kind="form",
        approved_regions=[frame.clip],
        focused_field_ref="otp",
    )
    binding = SensitiveSessionBindingV1(
        account_ref=ref,
        session_id="session",
        epoch=2,
        target=target,
        view_generation=4,
        record_session_id="record-session",
    )
    route = PortalOwnerRouteV1(
        binding=binding,
        node_identity=NodeIdentityV1(node_id="node", boot_id="boot"),
        region_focus=focus,
        session_revision=3,
        route_expires_at=now + timedelta(minutes=1),
        max_frame_bytes=4_000_000,
        max_input_bytes=4_096,
    )
    assert frame.mime_type == "image/png"
    assert route.binding.record_session_id == "record-session"
    with pytest.raises(ValueError):
        PortalPixelFrameV1(
            workspace_id=ref.workspace_id,
            account_id=ref.account_id,
            session_id="session",
            epoch=2,
            target=target,
            view_generation=4,
            sequence=1,
            region_kind="form",
            mime_type="image/jpeg",
            expires_at=now + timedelta(seconds=5),
            clip=frame.clip,
            byte_length=len(raw),
            frame_bytes=raw,
        )
    with pytest.raises(ValueError):
        PortalOwnerRouteV1(
            binding=binding.model_copy(update={"record_session_id": None}),
            node_identity=NodeIdentityV1(node_id="node", boot_id="boot"),
            region_focus=focus,
            session_revision=3,
            route_expires_at=now + timedelta(minutes=1),
            max_frame_bytes=4_000_000,
            max_input_bytes=4_096,
        )
