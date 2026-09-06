"""Pure C2/A/R contract gates for the QRAC2 browser portal.

This module deliberately does not persist state or perform browser side effects.
The owning service must call these gates before its transaction/transport work,
then atomically consume a validated ticket in the same database transaction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar
import hashlib
import hmac
import secrets

from pydantic import SecretStr

from backend.schemas.browser_account import (
    BrowserAccountErrorCode,
    CommandExecutionGuardV1,
    DurableCommandV1,
    ERROR_HTTP_STATUS,
    NodeClaimV1,
    PortalEntryBlockedV1,
    PortalEntryResponseV1,
    PortalModelDecisionV1,
    PortalOwnerRouteV1,
    PortalPerceptionV1,
    PortalPreEntryHandoffV1,
    PortalRecordSessionContractV1,
    PortalTicketConsumeCASV1,
    PortalTicketGrantV1,
    PortalTicketIssueRequestV1,
    PortalTicketIssuedV1,
    PortalTicketRecordV1,
    PortalTicketRedeemRequestV1,
    PortalWireFrameV1,
    SessionEnvelopeV1,
    SensitiveSessionBindingV1,
)


_TransportResult = TypeVar("_TransportResult")


def admit_command_before_side_effects(
    command: DurableCommandV1,
    claim: NodeClaimV1,
    session: SessionEnvelopeV1,
) -> CommandExecutionGuardV1:
    """Admit only the exact command/claim/session tuple before execution.

    Pydantic validation is intentionally performed here, at the side-effect
    boundary. A caller must not persist a result, launch a browser action, or
    emit an event until this function returns the guard.
    """

    return CommandExecutionGuardV1(command=command, claim=claim, session=session)


@dataclass
class _RegisteredRecordSession:
    session: Any
    page: Any
    binding: SensitiveSessionBindingV1
    contract: PortalRecordSessionContractV1


_RECORD_SESSIONS: dict[str, _RegisteredRecordSession] = {}


def _runtime_method(record_session: Any, name: str) -> Callable[..., Any]:
    method = getattr(record_session, name, None)
    if not callable(method):
        raise ValueError(f"record session does not implement {name}")
    return method


def admit_portal_entry(
    command: DurableCommandV1,
    claim: NodeClaimV1,
    session: SessionEnvelopeV1,
    *,
    binding: SensitiveSessionBindingV1,
    perception: PortalPerceptionV1,
    model_decision: PortalModelDecisionV1,
    record_session: Any,
) -> PortalPreEntryHandoffV1:
    """Stop common capture and drain events before freezing portal handoff."""

    guard = admit_command_before_side_effects(command, claim, session)
    freeze_portal_record_session(binding, record_session)
    registration = _RECORD_SESSIONS[binding.record_session_id or ""]
    handoff = PortalPreEntryHandoffV1(
        guard=guard,
        binding=binding,
        perception=perception,
        model_decision=model_decision,
        record=registration.contract,
    )
    if registration.contract.status != "sensitive":
        raise ValueError("portal pre-entry requires a stopped and drained record listener")
    return handoff


def register_portal_record_session(
    binding: SensitiveSessionBindingV1,
    record_session: Any,
    *,
    page: Any | None = None,
) -> PortalRecordSessionContractV1:
    """Register a real RecordSession and derive its live listener state."""

    record_id = binding.record_session_id
    actual_id = getattr(record_session, "session_id", None)
    actual_page = getattr(record_session, "page", None)
    if record_id is None or actual_id != record_id or actual_page is None:
        raise ValueError("record session must expose its real id and page")
    if page is not None and page is not actual_page:
        raise ValueError("record session page does not match registration")
    if getattr(record_session, "stopped", False):
        raise ValueError("stopped record session cannot be registered")
    is_installed = _runtime_method(record_session, "is_common_listener_installed")
    is_revoked = _runtime_method(record_session, "is_common_listener_revoked")
    if not is_installed() or is_revoked():
        raise ValueError("record session listener is not active")
    completion = PortalRecordSessionContractV1(
        record_session_id=record_id,
        target=binding.target,
        view_generation=binding.view_generation,
        status="active",
        page_bound=True,
        listener_installed=True,
        listener_revoked=False,
        pending_events_drained=False,
    )
    _RECORD_SESSIONS[record_id] = _RegisteredRecordSession(
        session=record_session,
        page=actual_page,
        binding=binding,
        contract=completion,
    )
    return completion


def freeze_portal_record_session(
    binding: SensitiveSessionBindingV1,
    record_session: Any,
) -> PortalRecordSessionContractV1:
    """Perform the real listener-stop and pending-event-drain transition."""

    registration = _RECORD_SESSIONS.get(binding.record_session_id or "")
    if registration is None or registration.session is not record_session:
        raise ValueError("record session is not registered for this binding")
    if registration.contract.status != "active":
        raise ValueError("record session is not in active pre-entry state")
    _runtime_method(record_session, "stop_common_listener")()
    if not _runtime_method(record_session, "drain_pending_events")():
        raise ValueError("record session pending events were not drained")
    is_installed = _runtime_method(record_session, "is_common_listener_installed")
    is_revoked = _runtime_method(record_session, "is_common_listener_revoked")
    has_pending = _runtime_method(record_session, "has_pending_events")
    if is_installed() or not is_revoked() or has_pending():
        raise ValueError("record listener stop/drain state was not confirmed")
    registration.contract = PortalRecordSessionContractV1(
        record_session_id=binding.record_session_id or "",
        target=binding.target,
        view_generation=binding.view_generation,
        status="sensitive",
        page_bound=True,
        listener_installed=False,
        listener_revoked=True,
        pending_events_drained=True,
    )
    return registration.contract


def resolve_portal_record_session(binding: SensitiveSessionBindingV1) -> Any:
    """Resolve the registered RecordSession and enforce its page identity."""

    registration = _RECORD_SESSIONS.get(binding.record_session_id or "")
    if registration is None or registration.binding != binding:
        raise ValueError("portal record session is not registered for this binding")
    if registration.contract.status not in {"active", "sensitive"}:
        raise ValueError("portal record session is no longer active")
    if getattr(registration.session, "page", None) is not registration.page:
        raise ValueError("portal record session page changed")
    return registration.session


def _resolve_frozen_portal_record_session(binding: SensitiveSessionBindingV1) -> Any:
    """Resolve only the post-stop session permitted for sensitive routing."""

    session = resolve_portal_record_session(binding)
    registration = _RECORD_SESSIONS[binding.record_session_id or ""]
    if registration.contract.status != "sensitive":
        raise ValueError("portal record session is not frozen for sensitive routing")
    return session


def complete_portal_record_session(
    binding: SensitiveSessionBindingV1,
    *,
    aborted: bool = False,
) -> PortalRecordSessionContractV1:
    """Require actual stop/listener-drain state, then release registration."""

    registration = _RECORD_SESSIONS.get(binding.record_session_id or "")
    if registration is None or registration.binding != binding:
        raise ValueError("portal record session is not registered for this binding")
    if registration.contract.status != "sensitive":
        raise ValueError("portal record session was not frozen before completion")
    if not getattr(registration.session, "stopped", False):
        raise ValueError("record completion requires RecordSession.stop")
    if (
        _runtime_method(registration.session, "is_common_listener_installed")()
        or not _runtime_method(registration.session, "is_common_listener_revoked")()
        or _runtime_method(registration.session, "has_pending_events")()
    ):
        raise ValueError("record completion requires revoked listener and drained events")
    completed = PortalRecordSessionContractV1(
        record_session_id=binding.record_session_id or "",
        target=binding.target,
        view_generation=binding.view_generation,
        status="aborted" if aborted else "completed",
        page_bound=True,
        listener_installed=False,
        listener_revoked=True,
        pending_events_drained=True,
    )
    del _RECORD_SESSIONS[binding.record_session_id or ""]
    return completed


def _digest(value: SecretStr) -> str:
    return hashlib.sha256(value.get_secret_value().encode("utf-8")).hexdigest()


def issue_first_portal_ticket(
    request: PortalTicketIssueRequestV1,
    *,
    ticket: SecretStr,
    now: datetime,
    expires_at: datetime,
    hard_expires_at: datetime,
    session_revision: int,
    ticket_id: str | None = None,
) -> PortalTicketIssuedV1:
    """Issue the first ticket and freeze its public id for persistence/CAS."""

    if request.expected_session_revision != session_revision:
        raise ValueError("ticket issue session revision mismatch")
    if now >= expires_at or expires_at > hard_expires_at:
        raise ValueError("ticket issue expiry is invalid")
    if ticket_id is None:
        ticket_id = secrets.token_urlsafe(24)
    return PortalTicketIssuedV1(
        account_ref=request.account_ref,
        session_id=request.session_id,
        session_revision=session_revision,
        ticket_id=ticket_id,
        ticket=ticket,
        csrf_token=request.csrf_token,
        issued_at=now,
        expires_at=expires_at,
        hard_expires_at=hard_expires_at,
    )




def ticket_record_from_issue(
    issued: PortalTicketIssuedV1,
    *,
    subject: str,
    ticket_id: str | None = None,
) -> PortalTicketRecordV1:
    """Create the digest-only record for the exact issued ticket id."""

    if ticket_id is not None and ticket_id != issued.ticket_id:
        raise ValueError("ticket record id does not match issued ticket")
    return PortalTicketRecordV1(
        ticket_id=issued.ticket_id,
        ticket_digest=_digest(issued.ticket),
        csrf_digest=_digest(issued.csrf_token),
        subject=subject,
        account_ref=issued.account_ref,
        session_id=issued.session_id,
        session_revision=issued.session_revision,
        issued_at=issued.issued_at,
        expires_at=issued.expires_at,
        hard_expires_at=issued.hard_expires_at,
    )


def _blocked(
    request: PortalTicketRedeemRequestV1,
    error_code: BrowserAccountErrorCode,
) -> PortalEntryBlockedV1:
    return PortalEntryBlockedV1(
        http_status=ERROR_HTTP_STATUS[error_code],
        account_ref=request.account_ref,
        session_id=request.session_id,
        session_revision=request.expected_session_revision,
        error_code=error_code,
    )


def redeem_portal_ticket(
    request: PortalTicketRedeemRequestV1,
    *,
    record: PortalTicketRecordV1,
    authenticated_subject: str,
    now: datetime,
    cookie_name: str,
    websocket_path: str,
) -> PortalEntryResponseV1:
    """Validate a ticket and return HTTP outcome plus an atomic CAS predicate."""

    if (
        record.account_ref.workspace_id != request.account_ref.workspace_id
        or record.account_ref.account_id != request.account_ref.account_id
        or record.session_id != request.session_id
    ):
        return _blocked(request, BrowserAccountErrorCode.PORTAL_NOT_FOUND)
    if record.ticket_id != request.ticket_id:
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)
    if record.consumed_at is not None or now >= record.expires_at:
        return _blocked(request, BrowserAccountErrorCode.SESSION_EXPIRED)
    if record.subject != authenticated_subject:
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)
    if record.session_revision != request.expected_session_revision:
        return _blocked(request, BrowserAccountErrorCode.STALE_GENERATION)
    if not hmac.compare_digest(record.ticket_digest, _digest(request.ticket)):
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)
    if not hmac.compare_digest(record.csrf_digest, _digest(request.csrf_token)):
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)

    cas = PortalTicketConsumeCASV1(
        ticket_id=record.ticket_id,
        account_ref=record.account_ref,
        session_id=record.session_id,
        expected_session_revision=record.session_revision,
    )
    return PortalTicketGrantV1(
        workspace_id=request.account_ref.workspace_id,
        account_id=request.account_ref.account_id,
        ticket_id=record.ticket_id,
        session_revision=record.session_revision,
        session_id=record.session_id,
        issued_at=record.issued_at,
        expires_at=record.expires_at,
        hard_expires_at=record.hard_expires_at,
        cookie_name=cookie_name,
        websocket_path=websocket_path,
        consume_cas=cas,
    )


def consume_portal_ticket_cas(
    request: PortalTicketRedeemRequestV1,
    *,
    record: PortalTicketRecordV1,
    authenticated_subject: str,
    now: datetime,
    cookie_name: str,
    websocket_path: str,
    cas_update: Callable[[PortalTicketConsumeCASV1, datetime], bool],
) -> PortalEntryResponseV1:
    """Apply the owner persistence CAS; a concurrent loser receives 410."""

    outcome = redeem_portal_ticket(
        request,
        record=record,
        authenticated_subject=authenticated_subject,
        now=now,
        cookie_name=cookie_name,
        websocket_path=websocket_path,
    )
    if not isinstance(outcome, PortalTicketGrantV1):
        return outcome
    if not cas_update(outcome.consume_cas, now):
        return _blocked(request, BrowserAccountErrorCode.SESSION_EXPIRED)
    return outcome


def _clip_contains(container: Any, candidate: Any) -> bool:
    return (
        container.x <= candidate.x
        and container.y <= candidate.y
        and container.x + container.width >= candidate.x + candidate.width
        and container.y + container.height >= candidate.y + candidate.height
    )


def route_portal_frame(
    owner_route: PortalOwnerRouteV1,
    frame: PortalWireFrameV1,
    *,
    now: datetime | None = None,
) -> PortalWireFrameV1:
    """Validate expiry, page/record lineage, focus, and bounded frame bytes."""

    _resolve_frozen_portal_record_session(owner_route.binding)
    checked_at = now or datetime.now(timezone.utc)
    if owner_route.route_expires_at <= checked_at:
        raise ValueError("portal owner route has expired")
    binding = frame.transient.binding
    route_binding = owner_route.binding
    if (
        binding.workspace_id != route_binding.account_ref.workspace_id
        or binding.account_id != route_binding.account_ref.account_id
        or binding.session_id != route_binding.session_id
        or binding.epoch != route_binding.epoch
        or binding.target != route_binding.target
        or binding.view_generation != route_binding.view_generation
    ):
        raise ValueError("portal frame is outside the owner route binding")
    if frame.contract_version != owner_route.contract_version:
        raise ValueError("portal frame and owner route contract versions differ")
    if frame.encoding == "pixel-binary":
        pixel = frame.transient.pixel
        assert pixel is not None
        if pixel.expires_at <= checked_at:
            raise ValueError("portal pixel frame has expired")
        if pixel.byte_length > owner_route.max_frame_bytes:
            raise ValueError("portal frame exceeds owner route byte limit")
        focus = owner_route.region_focus
        if pixel.region_kind != focus.region_kind and focus.region_kind != "approved":
            raise ValueError("portal pixel region is outside approved focus")
        if not any(_clip_contains(region, pixel.clip) for region in focus.approved_regions):
            raise ValueError("portal pixel clip is outside approved regions")
        if any(
            not any(_clip_contains(region, masked) for region in focus.approved_regions)
            for masked in pixel.masked_regions
        ):
            raise ValueError("portal pixel mask is outside approved regions")
    else:
        control = frame.transient.control
        assert control is not None
        payload = control.sensitive_payload
        if payload is not None and payload.value is not None:
            input_bytes = len(payload.value.get_secret_value().encode("utf-8"))
            if input_bytes > owner_route.max_input_bytes:
                raise ValueError("portal input exceeds owner route byte limit")
        if control.kind == "field_input":
            if control.field_ref != owner_route.region_focus.focused_field_ref:
                raise ValueError("portal input is outside approved focus")
    return frame


def invoke_portal_owner_transport(
    owner_route: PortalOwnerRouteV1,
    frame: PortalWireFrameV1,
    *,
    authenticate_owner: Callable[[PortalOwnerRouteV1], bool],
    transport: Callable[[PortalOwnerRouteV1, PortalWireFrameV1], _TransportResult],
    now: datetime | None = None,
) -> _TransportResult:
    """Authenticate the node tunnel, then invoke R after A-side validation."""

    validated = route_portal_frame(owner_route, frame, now=now)
    if not authenticate_owner(owner_route):
        raise ValueError("portal owner transport authentication failed")
    return transport(owner_route, validated)
