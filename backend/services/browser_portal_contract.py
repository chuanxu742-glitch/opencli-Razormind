"""Pure C2/A/R contract gates for the QRAC2 browser portal.

This module deliberately does not persist state or perform browser side effects.
The owning service must call these gates before its transaction/transport work,
then atomically consume a validated ticket in the same database transaction.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac

from pydantic import SecretStr

from backend.schemas.browser_account import (
    BrowserAccountErrorCode,
    CommandExecutionGuardV1,
    DurableCommandV1,
    ERROR_HTTP_STATUS,
    NodeClaimV1,
    PortalEntryBlockedV1,
    PortalEntryResponseV1,
    PortalOwnerRouteV1,
    PortalTicketGrantV1,
    PortalTicketIssueRequestV1,
    PortalTicketIssuedV1,
    PortalTicketRecordV1,
    PortalTicketRedeemRequestV1,
    PortalWireFrameV1,
    SessionEnvelopeV1,
)


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
) -> PortalTicketIssuedV1:
    """Build the first body-only ticket response after the caller's auth check.

    The caller persists :class:`PortalTicketRecordV1` using the returned secret
    digests; this response never places either secret in a URL.
    """

    if request.expected_session_revision != session_revision:
        raise ValueError("ticket issue session revision mismatch")
    return PortalTicketIssuedV1(
        account_ref=request.account_ref,
        session_id=request.session_id,
        session_revision=session_revision,
        ticket=ticket,
        csrf_token=request.csrf_token,
        issued_at=now,
        expires_at=expires_at,
        hard_expires_at=hard_expires_at,
    )

def ticket_record_from_issue(
    issued: PortalTicketIssuedV1,
    *,
    ticket_id: str,
    subject: str,
) -> PortalTicketRecordV1:
    """Create the durable digest-only record for one issued ticket."""

    return PortalTicketRecordV1(
        ticket_id=ticket_id,
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
    """Validate one ticket and return a typed HTTP outcome.

    The function has no persistence side effect. The owner must atomically set
    ``consumed_at`` only after a grant is returned; a non-null value is rejected
    as replay on every subsequent attempt.
    """

    if record.ticket_id != request.ticket_id:
        return _blocked(request, BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH)
    if record.consumed_at is not None or now >= record.expires_at:
        return _blocked(request, BrowserAccountErrorCode.SESSION_EXPIRED)
    if record.subject != authenticated_subject:
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)
    if record.account_ref != request.account_ref or record.session_id != request.session_id:
        return _blocked(request, BrowserAccountErrorCode.ACCOUNT_IDENTITY_MISMATCH)
    if record.session_revision != request.expected_session_revision:
        return _blocked(request, BrowserAccountErrorCode.STALE_GENERATION)
    if not hmac.compare_digest(record.ticket_digest, _digest(request.ticket)):
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)
    if not hmac.compare_digest(record.csrf_digest, _digest(request.csrf_token)):
        return _blocked(request, BrowserAccountErrorCode.PERMISSION_DENIED)

    return PortalTicketGrantV1(
        workspace_id=request.account_ref.workspace_id,
        account_id=request.account_ref.account_id,
        session_revision=record.session_revision,
        session_id=record.session_id,
        issued_at=record.issued_at,
        expires_at=record.expires_at,
        hard_expires_at=record.hard_expires_at,
        cookie_name=cookie_name,
        websocket_path=websocket_path,
    )


def route_portal_frame(
    owner_route: PortalOwnerRouteV1,
    frame: PortalWireFrameV1,
) -> PortalWireFrameV1:
    """Apply the A-owned session/page/record route to an R wire frame."""

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
    if frame.byte_length is not None and frame.byte_length > owner_route.max_frame_bytes:
        raise ValueError("portal frame exceeds owner route byte limit")
    return frame
