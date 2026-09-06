"""Pure C2/A/R contract gates for the QRAC2 browser portal.

This module does not own persistence or transport. It invokes the real
RecordSession lifecycle at the sensitive boundary, then leaves ticket CAS and
owner transport commits to their owning services.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
import secrets
import struct
from typing import Any, Callable, TypeVar

from pydantic import SecretStr

from backend.schemas.browser_account import (
    AccountRef,
    BrowserAccountErrorCode,
    BrowserAccountRevisionCASV1,
    BrowserAccountRevisionPreconditionV1,
    CommandExecutionGuardV1,
    DurableCommandV1,
    ERROR_HTTP_STATUS,
    NodeClaimV1,
    PortalControlMessageV1,
    PortalEntryBlockedV1,
    PortalEntryResponseV1,
    PortalModelDecisionV1,
    PortalOuterBindingV1,
    PortalOwnerRouteV1,
    PortalPerceptionV1,
    PortalPreEntryHandoffV1,
    PortalRecordSessionContractV1,
    PortalSensitivePayloadV1,
    PortalTicketConsumeCASV1,
    PortalTicketGrantV1,
    PortalTicketIssueRequestV1,
    PortalTicketIssuedV1,
    PortalTicketRecordV1,
    PortalTicketRedeemRequestV1,
    PortalTransientV1,
    PortalWireFrameV1,
    PortalWireLayoutV1,
    PortalPixelFrameV1,
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
    page_identity: Any
    frame_identity: Any
    document_identity: Any
    _reported_document: Any


_RECORD_SESSIONS: dict[str, _RegisteredRecordSession] = {}


def _record_identity(record_session: Any) -> tuple[Any, Any, Any, Any]:
    identity = getattr(record_session, "binding_identity", None)
    if callable(identity):
        value = identity()
        if isinstance(value, tuple) and len(value) == 4:
            return value
    page = getattr(record_session, "page", None)
    raw_page = getattr(page, "page", page)
    frame = getattr(raw_page, "main_frame", None)
    if callable(frame):
        frame = frame()
    document = getattr(raw_page, "document", None)
    if document is None:
        document = getattr(raw_page, "document_id", None)
    url = getattr(raw_page, "url", None)
    return raw_page, frame, (document, url), None


def _require_same_record_identity(
    registration: _RegisteredRecordSession,
    record_session: Any,
) -> None:
    page, frame, document, reported_document = _record_identity(record_session)
    if page is not registration.page_identity:
        raise ValueError("record session page changed")
    if frame is not registration.frame_identity:
        raise ValueError("record session frame changed")
    if document != registration.document_identity:
        raise ValueError("record session document changed")
    if (
        registration._reported_document is not None
        and reported_document != registration._reported_document
    ):
        raise ValueError("record session document changed")


async def _call_async(record_session: Any, name: str, *args: Any) -> Any:
    method = getattr(record_session, name, None)
    if not callable(method):
        raise ValueError(f"record session does not implement async {name}")
    result = method(*args)
    if not inspect.isawaitable(result):
        raise ValueError(f"record session {name} must be async")
    return await result



async def _page_is_closed(page: Any) -> bool:
    raw_page = getattr(page, "page", page)
    checker = getattr(raw_page, "is_closed", None)
    if callable(checker):
        result = checker()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    return bool(getattr(raw_page, "closed", False))


async def _require_open_page(page: Any) -> None:
    if await _page_is_closed(page):
        raise ValueError("record session page is closed")


def _listener_state(record_session: Any) -> tuple[bool, bool, bool]:
    installed = getattr(record_session, "is_common_listener_installed", None)
    revoked = getattr(record_session, "is_common_listener_revoked", None)
    pending = getattr(record_session, "has_pending_events", None)
    sensitive = bool(getattr(record_session, "sensitive", False))
    return (
        bool(installed()) if callable(installed) else not sensitive,
        bool(revoked()) if callable(revoked) else sensitive,
        bool(pending()) if callable(pending) else False,
    )


async def admit_portal_entry(
    command: DurableCommandV1,
    claim: NodeClaimV1,
    session: SessionEnvelopeV1,
    *,
    binding: SensitiveSessionBindingV1,
    perception: PortalPerceptionV1,
    model_decision: PortalModelDecisionV1,
    record_session: Any,
) -> PortalPreEntryHandoffV1:
    """Await real sensitive-mode transition before exposing the handoff."""

    guard = admit_command_before_side_effects(command, claim, session)
    await freeze_portal_record_session(binding, record_session)
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
    """Register a real RecordSession before its async sensitive transition."""

    record_id = binding.record_session_id
    actual_id = getattr(record_session, "session_id", None)
    actual_page = getattr(record_session, "page", None)
    if record_id is None or actual_id != record_id or actual_page is None:
        raise ValueError("record session must expose its real id and page")
    if page is not None and page is not actual_page:
        raise ValueError("record session page does not match registration")
    if getattr(record_session, "stopped", False) or getattr(record_session, "sensitive", False):
        raise ValueError("stopped or sensitive record session cannot be registered")
    if record_id in _RECORD_SESSIONS:
        raise ValueError("record session id is already registered")
    page_identity, frame_identity, document_identity, reported_document = _record_identity(
        record_session
    )
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
        page_identity=page_identity,
        frame_identity=frame_identity,
        document_identity=document_identity,
        _reported_document=reported_document,
    )
    return completion


async def freeze_portal_record_session(
    binding: SensitiveSessionBindingV1,
    record_session: Any,
) -> PortalRecordSessionContractV1:
    """Await RecordSession.set_sensitive(True), including listener drain."""

    registration = _RECORD_SESSIONS.get(binding.record_session_id or "")
    if (
        registration is None
        or registration.session is not record_session
        or registration.binding != binding
    ):
        raise ValueError("record session is not registered for this binding")
    if registration.contract.status != "active":
        raise ValueError("record session is not in active pre-entry state")
    if getattr(record_session, "stopped", False):
        raise ValueError("stopped record session cannot enter sensitive mode")
    _require_same_record_identity(registration, record_session)
    await _require_open_page(registration.page)
    result = await _call_async(record_session, "set_sensitive", True)
    installed, revoked, pending = _listener_state(record_session)
    if (
        result is False
        or getattr(record_session, "sensitive", False) is not True
        or installed
        or not revoked
        or pending
    ):
        raise ValueError("record session sensitive transition did not complete")
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


async def resolve_portal_record_session(binding: SensitiveSessionBindingV1) -> Any:
    """Resolve page/document lineage before sensitive transport."""

    registration = _RECORD_SESSIONS.get(binding.record_session_id or "")
    if registration is None or registration.binding != binding:
        raise ValueError("portal record session is not registered for this binding")
    if registration.contract.status not in {"active", "sensitive"}:
        raise ValueError("portal record session is no longer active")
    if getattr(registration.session, "stopped", False):
        raise ValueError("portal record session is stopped")
    if getattr(registration.session, "page", None) is not registration.page:
        raise ValueError("portal record session page changed")
    _require_same_record_identity(registration, registration.session)
    await _require_open_page(registration.page)
    return registration.session


async def _resolve_frozen_portal_record_session(binding: SensitiveSessionBindingV1) -> Any:
    """Resolve only the post-stop session permitted for sensitive routing."""

    session = await resolve_portal_record_session(binding)
    registration = _RECORD_SESSIONS[binding.record_session_id or ""]
    if registration.contract.status != "sensitive":
        raise ValueError("portal record session is not frozen for sensitive routing")
    return session


async def complete_portal_record_session(
    binding: SensitiveSessionBindingV1,
    *,
    aborted: bool = False,
) -> PortalRecordSessionContractV1:
    """Await real RecordSession.stop, verify closed state, then release."""

    registration = _RECORD_SESSIONS.get(binding.record_session_id or "")
    if registration is None or registration.binding != binding:
        raise ValueError("portal record session is not registered for this binding")
    if getattr(registration.session, "stopped", False):
        _RECORD_SESSIONS.pop(binding.record_session_id or "", None)
        raise ValueError("record session is already stopped")
    if registration.contract.status != "sensitive":
        raise ValueError("portal record session was not frozen before completion")
    try:
        await _call_async(registration.session, "stop")
        installed, revoked, pending = _listener_state(registration.session)
        if (
            not getattr(registration.session, "stopped", False)
            or installed
            or not revoked
            or pending
        ):
            raise ValueError("record completion requires revoked listener and drained events")
        completed = PortalRecordSessionContractV1(
            record_session_id=binding.record_session_id or "",
            target=binding.target,
            view_generation=binding.view_generation,
            status="aborted" if aborted else "completed",
            page_bound=True,
            listener_installed=installed,
            listener_revoked=revoked,
            pending_events_drained=not pending,
        )
        return completed
    finally:
        _RECORD_SESSIONS.pop(binding.record_session_id or "", None)


def _digest(value: SecretStr) -> str:
    return hashlib.sha256(value.get_secret_value().encode("utf-8")).hexdigest()


def resolve_account_revision_precondition(
    *,
    if_match: str | None,
    body_revision: int | None,
) -> int | None:
    """Resolve If-Match/body revision with mismatch rejection."""

    return BrowserAccountRevisionPreconditionV1(
        if_match=if_match,
        body_revision=body_revision,
    ).effective_revision


def build_account_revision_cas(
    account_ref: AccountRef,
    *,
    expected_revision: int,
) -> BrowserAccountRevisionCASV1:
    """Build the exact owner transaction predicate; owner must commit it."""

    return BrowserAccountRevisionCASV1(
        account_ref=account_ref,
        expected_revision=expected_revision,
        next_revision=expected_revision + 1,
    )


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
async def consume_portal_ticket_cas(
    request: PortalTicketRedeemRequestV1,
    *,
    record: PortalTicketRecordV1,
    authenticated_subject: str,
    now: datetime,
    cookie_name: str,
    websocket_path: str,
    cas_update: Callable[[PortalTicketConsumeCASV1, datetime], Any],
) -> PortalEntryResponseV1:
    """Await the owner's transaction CAS; a concurrent loser receives 410."""

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
    applied = cas_update(outcome.consume_cas, now)
    if inspect.isawaitable(applied):
        applied = await applied
    if not applied:
        return _blocked(request, BrowserAccountErrorCode.SESSION_EXPIRED)
    return outcome

_PORTAL_WIRE_HEADER = struct.Struct(">4sBBBBHIH")
PORTAL_WIRE_HEADER_SIZE = 16
PORTAL_WIRE_HEADER_LAYOUT = (
    ("magic", 0, 4, "ASCII"),
    ("version", 4, 1, "uint8"),
    ("encoding", 5, 1, "uint8"),
    ("flags", 6, 1, "uint8"),
    ("padding", 7, 1, "zero"),
    ("metadata_bytes", 8, 2, "uint16 big-endian"),
    ("payload_bytes", 10, 4, "uint32 big-endian"),
    ("reserved", 14, 2, "zero"),
)
_PORTAL_WIRE_MAGIC = b"Q2P1"
_PORTAL_WIRE_VERSION = 1
_PORTAL_WIRE_ENCODING_CONTROL = 0
_PORTAL_WIRE_ENCODING_PIXEL = 1


def _wire_metadata(frame: PortalWireFrameV1) -> tuple[dict[str, Any], bytes]:
    payload = b""
    if frame.transient.control is not None:
        control = frame.transient.control
        metadata_message = control.model_dump(mode="json", exclude={"sensitive_payload"})
        sensitive = control.sensitive_payload
        if sensitive is not None:
            value = sensitive.value.get_secret_value() if sensitive.value is not None else None
            metadata_message["sensitive_payload"] = {
                "value_present": value is not None,
                "key": sensitive.key,
                "x": sensitive.x,
                "y": sensitive.y,
            }
            if value is not None:
                payload = value.encode("utf-8")
    else:
        pixel = frame.transient.pixel
        assert pixel is not None
        metadata_message = pixel.model_dump(mode="json", exclude={"frame_bytes"})
        payload = pixel.frame_bytes.get_secret_value()
    return (
        {
            "contract_version": frame.contract_version,
            "protocol": frame.protocol,
            "sequence": frame.sequence,
            "encoding": frame.encoding,
            "content_type": frame.content_type,
            "mime_type": frame.mime_type,


            "byte_length": frame.byte_length,
            "binding": frame.transient.binding.model_dump(mode="json"),
            "message": metadata_message,
        },
        payload,
    )
def portal_wire_metadata_length(frame: PortalWireFrameV1) -> int:
    """Return canonical UTF-8 metadata length for the fixed header."""

    metadata, _ = _wire_metadata(frame)
    return len(
        json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def encode_portal_wire_frame(frame: PortalWireFrameV1) -> bytes:
    """Encode a canonical frame using the fixed 16-byte big-endian header."""

    metadata, payload = _wire_metadata(frame)
    metadata_bytes = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not metadata_bytes or len(metadata_bytes) > 65_535:
        raise ValueError("portal wire metadata exceeds uint16")
    if len(payload) > 4_000_000:
        raise ValueError("portal wire payload exceeds contract limit")
    if frame.layout.metadata_bytes != len(metadata_bytes):
        raise ValueError("portal wire metadata length differs")
    if frame.layout.payload_bytes != len(payload):
        raise ValueError("portal wire payload length differs")
    encoding = (
        _PORTAL_WIRE_ENCODING_CONTROL
        if frame.encoding == "control-json"
        else _PORTAL_WIRE_ENCODING_PIXEL
    )
    header = _PORTAL_WIRE_HEADER.pack(
        _PORTAL_WIRE_MAGIC,
        _PORTAL_WIRE_VERSION,
        encoding,
        0,
        0,
        len(metadata_bytes),
        len(payload),
        0,
    )
    return header + metadata_bytes + payload


def decode_portal_wire_frame(data: bytes) -> PortalWireFrameV1:
    """Decode transient bytes and wipe the mutable receive buffer."""

    buffer = bytearray(data)
    try:
        if len(buffer) < _PORTAL_WIRE_HEADER.size:
            raise ValueError("portal wire frame is shorter than header")
        magic, version, encoding_code, flags, padding, metadata_len, payload_len, reserved = (
            _PORTAL_WIRE_HEADER.unpack(buffer[: _PORTAL_WIRE_HEADER.size])
        )
        if (
            magic != _PORTAL_WIRE_MAGIC
            or version != _PORTAL_WIRE_VERSION
            or encoding_code not in {_PORTAL_WIRE_ENCODING_CONTROL, _PORTAL_WIRE_ENCODING_PIXEL}
            or flags != 0
            or padding != 0
            or reserved != 0
        ):
            raise ValueError("portal wire header is invalid")
        if metadata_len == 0 or payload_len > 4_000_000:
            raise ValueError("portal wire lengths are invalid")
        start = _PORTAL_WIRE_HEADER.size
        metadata_end = start + metadata_len
        payload_end = metadata_end + payload_len
        if payload_end != len(buffer):
            raise ValueError("portal wire lengths do not match frame bytes")
        metadata = json.loads(bytes(buffer[start:metadata_end]).decode("utf-8"))
        expected_keys = {
            "binding",
            "byte_length",
            "content_type",
            "contract_version",
            "encoding",
            "message",
            "mime_type",
            "protocol",
            "sequence",
        }
        if not isinstance(metadata, dict) or set(metadata) != expected_keys:
            raise ValueError("portal wire metadata object is not canonical")
        expected_encoding = (
            "control-json" if encoding_code == _PORTAL_WIRE_ENCODING_CONTROL else "pixel-binary"
        )
        if metadata["encoding"] != expected_encoding:
            raise ValueError("portal wire header and metadata encoding differ")
        binding = PortalOuterBindingV1.model_validate(metadata["binding"])
        payload = bytes(buffer[metadata_end:payload_end])
        message = metadata["message"]
        if not isinstance(message, dict):
            raise ValueError("portal wire message metadata is invalid")
        if expected_encoding == "control-json":
            sensitive = message.pop("sensitive_payload", None)
            if sensitive is not None:
                if not isinstance(sensitive, dict) or set(sensitive) != {
                    "key",
                    "value_present",
                    "x",
                    "y",
                }:
                    raise ValueError("portal sensitive metadata is invalid")
                if sensitive["value_present"]:
                    value = SecretStr(payload.decode("utf-8"))
                else:
                    if payload:
                        raise ValueError("portal unexpected control payload bytes")
                    value = None
                message["sensitive_payload"] = PortalSensitivePayloadV1(
                    value=value,
                    key=sensitive["key"],
                    x=sensitive["x"],
                    y=sensitive["y"],
                )
            elif payload:
                raise ValueError("portal unexpected control payload bytes")
            control = PortalControlMessageV1.model_validate(message)
            transient = PortalTransientV1(binding=binding, control=control)
            return PortalWireFrameV1(
                contract_version=metadata["contract_version"],
                protocol=metadata["protocol"],
                sequence=metadata["sequence"],
                encoding=expected_encoding,
                content_type=metadata["content_type"],
                mime_type=metadata["mime_type"],
                byte_length=None,
                layout=PortalWireLayoutV1(
                    metadata_bytes=metadata_len,
                    payload_bytes=payload_len,
                ),
                transient=transient,
            )
        pixel = PortalPixelFrameV1.model_validate(
            {**message, "frame_bytes": payload, "byte_length": payload_len}
        )
        transient = PortalTransientV1(binding=binding, pixel=pixel)
        return PortalWireFrameV1(
            contract_version=metadata["contract_version"],
            protocol=metadata["protocol"],
            sequence=metadata["sequence"],
            encoding=expected_encoding,
            content_type=metadata["content_type"],
            mime_type=metadata["mime_type"],
            byte_length=metadata["byte_length"],
            layout=PortalWireLayoutV1(
                metadata_bytes=metadata_len,
                payload_bytes=payload_len,
            ),
            transient=transient,
        )
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("portal "):
            raise
        raise ValueError("portal wire frame cannot be decoded") from None
    finally:
        buffer[:] = b"\x00" * len(buffer)

def _clip_contains(container: Any, candidate: Any) -> bool:
    return (
        container.x <= candidate.x
        and container.y <= candidate.y
        and container.x + container.width >= candidate.x + candidate.width
        and container.y + container.height >= candidate.y + candidate.height
    )


async def route_portal_frame(
    owner_route: PortalOwnerRouteV1,
    frame: PortalWireFrameV1,
    *,
    now: datetime | None = None,
) -> PortalWireFrameV1:
    """Validate complete wire binding before resolving the live page."""

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
    await _resolve_frozen_portal_record_session(owner_route.binding)
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


async def invoke_portal_owner_transport(
    owner_route: PortalOwnerRouteV1,
    frame: PortalWireFrameV1,
    *,
    authenticate_owner: Callable[[PortalOwnerRouteV1], bool],
    transport: Callable[[PortalOwnerRouteV1, PortalWireFrameV1], _TransportResult],
    now: datetime | None = None,
) -> _TransportResult:
    """Authenticate the node tunnel, then invoke R after A-side validation."""

    validated = await route_portal_frame(owner_route, frame, now=now)
    if not authenticate_owner(owner_route):
        raise ValueError("portal owner transport authentication failed")
    return transport(owner_route, validated)
