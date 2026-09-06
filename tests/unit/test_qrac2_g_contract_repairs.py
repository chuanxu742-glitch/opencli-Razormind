from datetime import datetime, timedelta, timezone


import pytest
from pydantic import SecretStr

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
    PortalModelDecisionV1,
    PortalOwnerRouteV1,
    PortalPerceptionElementV1,
    PortalPerceptionV1,
    PortalPixelFrameV1,
    PortalRegionFocusV1,
    PortalOuterBindingV1,
    PortalRecordSessionContractV1,
    PortalTicketConsumeCASV1,
    PortalTicketGrantV1,
    PortalTicketIssueRequestV1,
    PortalTicketRecordV1,
    PortalTicketRedeemRequestV1,
    PortalTransientV1,
    PortalWireLayoutV1,
    PortalWireFrameV1,
    SensitiveSessionBindingV1,
    SessionEnvelopeV1,
    SessionTargetV1,
)
from backend.services.browser_portal_contract import (
    admit_command_before_side_effects,
    admit_portal_entry,
    freeze_portal_record_session,
    complete_portal_record_session,
    invoke_portal_owner_transport,
    issue_first_portal_ticket,
    redeem_portal_ticket,
    register_portal_record_session,
    route_portal_frame,
    consume_portal_ticket_cas,
    ticket_record_from_issue,
)
class _RecordSessionProbe:
    def __init__(self) -> None:
        self.session_id = "record-session"
        self.page = object()
        self.stopped = False
        self._listener_installed = True
        self._listener_revoked = False
        self._pending_events = 1

    def is_common_listener_installed(self) -> bool:
        return self._listener_installed

    def is_common_listener_revoked(self) -> bool:
        return self._listener_revoked

    def stop_common_listener(self) -> None:
        self._listener_installed = False
        self._listener_revoked = True

    def drain_pending_events(self) -> bool:
        self._pending_events = 0
        return True

    def has_pending_events(self) -> bool:
        return self._pending_events != 0

    def stop(self) -> None:
        self.stopped = True




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
    admitted = admit_command_before_side_effects(command, claim, session)
    assert admitted.claim.command_id == "command"
    with pytest.raises(ValueError):
        admit_command_before_side_effects(
            command,
            claim.model_copy(update={"workspace_id": "other"}),
            session,
        )
    with pytest.raises(ValueError):
        admit_command_before_side_effects(
            command.model_copy(
                update={"payload": LoginRuleCommandPayloadV1(login_rule_id="rule", login_rule_version="2")}
            ),
            claim,
            session,
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
        http_status=503,
        account_ref=ref,
        session_id="session",
        session_revision=3,
        error_code="capability_missing",
    )
    grant = PortalTicketGrantV1(
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        ticket_id="ticket-id",
        session_id="session",
        session_revision=3,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        hard_expires_at=now + timedelta(minutes=10),
        cookie_name="portal",
        websocket_path="/api/v1/portal",
        consume_cas=PortalTicketConsumeCASV1(
            ticket_id="ticket-id",
            account_ref=ref,
            session_id="session",
            expected_session_revision=3,
        ),
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
        owner_endpoint="https://owner.test",
        tunnel_handle="tunnel-handle",
        tunnel_auth_digest="a" * 64,
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
            owner_endpoint="https://owner.test",
            tunnel_handle="tunnel-handle",
            tunnel_auth_digest="a" * 64,
            region_focus=focus,
            session_revision=3,
            route_expires_at=now + timedelta(minutes=1),
            max_frame_bytes=4_000_000,
            max_input_bytes=4_096,
        )


def test_h3_first_ticket_redeem_binds_csrf_session_and_replay() -> None:
    now, ref, target, _, _, _ = _context()
    csrf = SecretStr("csrf-token-012345")
    ticket = SecretStr("ticket-secret-012345")
    issue = issue_first_portal_ticket(
        PortalTicketIssueRequestV1(
            account_ref=ref,
            session_id="session",
            expected_session_revision=3,
            csrf_token=csrf,
        ),
        ticket=ticket,
        now=now,
        expires_at=now + timedelta(minutes=5),
        hard_expires_at=now + timedelta(minutes=10),
        session_revision=3,
        ticket_id="ticket-id",
    )
    assert issue.ticket_id == "ticket-id"
    record = ticket_record_from_issue(issue, subject="operator")
    redeem = PortalTicketRedeemRequestV1(
        first_entry="initial",
        account_ref=ref,
        session_id="session",
        expected_session_revision=3,
        ticket_id="ticket-id",
        ticket=ticket,
        csrf_token=csrf,
    )
    grant = redeem_portal_ticket(
        redeem,
        record=record,
        authenticated_subject="operator",
        now=now,
        cookie_name="qrac2",
        websocket_path="/portal",
    )
    assert isinstance(grant, PortalTicketGrantV1)
    cas_loser = consume_portal_ticket_cas(
        redeem,
        record=record,
        authenticated_subject="operator",
        now=now,
        cookie_name="qrac2",
        websocket_path="/portal",
        cas_update=lambda _cas, _now: False,
    )
    assert getattr(cas_loser, "status") == "blocked"
    assert getattr(cas_loser, "http_status") == 410
    replay = redeem_portal_ticket(
        redeem,
        record=record.model_copy(update={"consumed_at": now}),
        authenticated_subject="operator",
        now=now,
        cookie_name="qrac2",
        websocket_path="/portal",
    )
    assert getattr(replay, "status") == "blocked"
    assert getattr(replay, "http_status") == 410
    mismatch = redeem_portal_ticket(
        redeem.model_copy(update={"session_id": "other"}),
        record=record,
        authenticated_subject="operator",
        now=now,
        cookie_name="qrac2",
        websocket_path="/portal",
    )
    assert getattr(mismatch, "status") == "blocked"
    assert getattr(mismatch, "http_status") == 404


def test_h5_owner_route_applies_real_wire_binding() -> None:
    now, ref, target, _, _, _ = _context()
    raw = b"\x89PNG\r\n\x1a\n"
    frame = PortalPixelFrameV1(
        workspace_id=ref.workspace_id,
        account_id=ref.account_id,
        session_id="session",
        epoch=2,
        target=target,
        view_generation=4,
        sequence=2,
        region_kind="form",
        mime_type="image/png",
        expires_at=now + timedelta(seconds=5),
        clip=PortalClipV1(x=0, y=0, width=10, height=10),
        byte_length=len(raw),
        frame_bytes=raw,
    )
    wire = PortalWireFrameV1(
        sequence=2,
        encoding="pixel-binary",
        content_type="application/octet-stream",
        mime_type="image/png",
        byte_length=len(raw),
        layout=PortalWireLayoutV1(metadata_bytes=16, payload_bytes=len(raw)),
        transient=PortalTransientV1(
            binding=PortalOuterBindingV1(
                workspace_id=ref.workspace_id,
                account_id=ref.account_id,
                session_id="session",
                epoch=2,
                target=target,
                view_generation=4,
            ),
            pixel=frame,
        ),
    )
    with pytest.raises(ValueError):
        PortalWireFrameV1(
            sequence=2,
            encoding="pixel-binary",
            content_type="application/octet-stream",
            mime_type="image/png",
            byte_length=len(raw),
            layout=PortalWireLayoutV1(metadata_bytes=16, payload_bytes=len(raw) + 1),
            transient=wire.transient,
        )
    binding = SensitiveSessionBindingV1(
        account_ref=ref,
        session_id="session",
        epoch=2,
        target=target,
        view_generation=4,
        record_session_id="record-session",
    )
    record_session = _RecordSessionProbe()
    register_portal_record_session(binding, record_session)
    freeze_portal_record_session(binding, record_session)
    route = PortalOwnerRouteV1(
        binding=binding,
        node_identity=NodeIdentityV1(node_id="node", boot_id="boot"),
        owner_endpoint="https://owner.test",
        tunnel_handle="tunnel-handle",
        tunnel_auth_digest="a" * 64,
        region_focus=PortalRegionFocusV1(
            target=target,
            view_generation=4,
            region_kind="form",
            approved_regions=[frame.clip],
            focused_field_ref="otp",
        ),
        session_revision=3,
        route_expires_at=now + timedelta(minutes=1),
        max_frame_bytes=4_000_000,
        max_input_bytes=4_096,
    )
    assert route_portal_frame(route, wire) is wire
    seen: list[tuple[str, int]] = []
    assert (
        invoke_portal_owner_transport(
            route,
            wire,
            authenticate_owner=lambda owner: owner.tunnel_handle == "tunnel-handle"
            and owner.tunnel_auth_digest == "a" * 64,
            now=now,
            transport=lambda owner, value: (
                seen.append((owner.node_identity.node_id, value.sequence)) or value
            ),
        )
        is wire
    )
    assert seen == [("node", 2)]
    with pytest.raises(ValueError):
        invoke_portal_owner_transport(
            route,
            wire,
            authenticate_owner=lambda _: False,
            now=now,
            transport=lambda *_: pytest.fail("unauthenticated route reached owner transport"),
        )
    with pytest.raises(ValueError):
        invoke_portal_owner_transport(
            route.model_copy(update={"route_expires_at": now - timedelta(seconds=1)}),
            wire,
            authenticate_owner=lambda _: True,
            now=now,
            transport=lambda *_: pytest.fail("expired route reached owner transport"),
        )
    with pytest.raises(ValueError):
        route_portal_frame(
            route,
            wire.model_copy(
                update={
                    "transient": wire.transient.model_copy(
                        update={"binding": wire.transient.binding.model_copy(update={"session_id": "other"})}
                    )
                }
            ),
        )
    record_session.stop()
    completed = complete_portal_record_session(binding)
    assert completed.status == "completed"
    with pytest.raises(ValueError):
        route_portal_frame(route, wire, now=now)


def test_h1_pre_entry_freezes_perception_model_and_record_tuple() -> None:
    now, ref, target, session, command, claim = _context()
    binding = SensitiveSessionBindingV1(
        account_ref=ref,
        session_id="session",
        epoch=2,
        target=target,
        view_generation=4,
        record_session_id="record-session",
    )
    focus = PortalRegionFocusV1(
        target=target,
        view_generation=4,
        region_kind="form",
        approved_regions=[PortalClipV1(x=0, y=0, width=10, height=10)],
        focused_field_ref="otp",
    )
    record_session = _RecordSessionProbe()
    register_portal_record_session(binding, record_session)
    handoff = admit_portal_entry(
        command,
        claim,
        session,
        binding=binding,
        perception=PortalPerceptionV1(
            target=target,
            view_generation=4,
            elements=[PortalPerceptionElementV1(ref="0", role="input", field_ref="otp")],
            region_focus=focus,
        ),
        model_decision=PortalModelDecisionV1(
            target=target,
            view_generation=4,
            action="input",
            focused_field_ref="otp",
        ),
        record_session=record_session,
    )
    assert handoff.guard.command.command_id == "command"
    with pytest.raises(ValueError):
        PortalPerceptionElementV1(ref="secret", role="input", name="OTP")
    with pytest.raises(ValueError):
        admit_portal_entry(
            command,
            claim,
            session,
            binding=binding,
            perception=handoff.perception,
            model_decision=handoff.model_decision.model_copy(update={"view_generation": 3}),
            record_session=record_session,
        )
