"""Center-side manager for reverse WebSocket connections from edge agents.

When an edge agent cannot be reached by the center (NAT, firewall), it initiates
a persistent WebSocket connection to the center instead. The center dispatches
tasks by sending JSON messages down this connection and awaiting results.

Two independent request/response families share the same connection:

- ``collect`` / ``result`` — single-shot opencli collection tasks (unchanged).
- ``agent_task`` / ``agent_event`` (0..N) / ``agent_result`` — streaming
  agent-runtime task dispatch.

Wire protocol — every reverse-channel message type, one-line field shapes:

  register      agent→center  {"type": "register", "agent_url": str,
                                "mode": "bridge"|"cdp", "node_type"?: str,
                                "label"?: str, "runtimes"?: list[str]}
  registered    center→agent  {"type": "registered", "agent_url": str}
  collect       center→agent  {"type": "collect", "request_id": uuid,
                                "site": str, "command": str, "args": dict,
                                "positional_args": list, "format": str, "mode": str}
  result        agent→center  {"type": "result", "request_id": uuid,
                                "success": bool, "items": list, "error": str|None}
  ping          either→other  {"type": "ping"}
  pong          either→other  {"type": "pong"}
  agent_task    center→agent  {"type": "agent_task", "request_id": uuid,
                                "runtime": str, "workflow": str, "input": dict,
                                "config": dict, "session_id": str|None}
  agent_event   agent→center  {"type": "agent_event", "request_id": uuid,
                                "event": dict, "event_id"?: uuid,
                                "ack_required"?: bool}
                                # one RuntimeEvent; 0..N per task
  agent_event_ack center→agent {"type": "agent_event_ack", "request_id": uuid,
                                "event_id": uuid, "status": "persisted"}
                                # sent only after an ack-required event's callback
                                # has durably consumed the event
  agent_result  agent→center  {"type": "agent_result", "request_id": uuid,
                                "result": dict}
                                # terminal done/error RuntimeEvent; exactly 1
  cancel        center→agent  {"type": "cancel", "request_id": uuid}
                                # stops collect or agent_task with the same id
  agent_task_status center→agent {"type": "agent_task_status", "request_id": probe_uuid,
                                  "task_id": original_task_uuid}
  agent_task_status_result agent→center {"type": "agent_task_status_result",
                                         "request_id": probe_uuid, "task_id": original_task_uuid,
                                         "result": terminal | {"status": "running"|"unknown"}}

Protocol (collect/result path):
  1. Agent connects to  ws(s)://{center}/api/v1/browsers/agents/ws
  2. Agent → center:  {"type": "register", "agent_url": "...", "mode": "bridge", "label": "..."}
  3. Center → agent:  {"type": "registered", "agent_url": "..."}
  4. Center → agent:  {"type": "collect", "request_id": "<uuid>", "site": "...", ...}
  5. Agent → center:  {"type": "result", "request_id": "<uuid>", "success": true, "items": [...]}
  6. Either side:      {"type": "ping"} / {"type": "pong"}

Protocol (agent_task streaming path):
  1-3. Same registration handshake as above (registration may additionally
       carry ``runtimes`` — the agent-runtime types available on this node).
  4. Center → agent:  {"type": "agent_task", "request_id": "<uuid>", "runtime": "pi", ...}
  5. Agent → center:  0..N  {"type": "agent_event", "request_id": "<uuid>", "event": {...}}
  6. Agent → center:  exactly 1  {"type": "agent_result", "request_id": "<uuid>", "result": {...}}
"""

import asyncio
import hashlib
import inspect
import logging
import math
import struct
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from backend.browser_desktop_protocol import (
    DESKTOP_MAX_BUFFERED_FRAMES,
    BrowserDesktopRouteV1,
    decode_desktop_frame,
    encode_desktop_frame,
)
from backend.schemas.browser_account import (
    CommandExecutionGuardV1,
    DurableCommandV1,
    NodeClaimV1,
    NodeIdentityV1,
    PortalOwnerRouteV1,
    PortalWireFrameV1,
    SessionEnvelopeV1,
)
from backend.services.browser_portal_contract import (
    decode_portal_wire_frame,
    encode_portal_wire_frame,
    validate_portal_frame_binding,
)

logger = logging.getLogger(__name__)

# agent_url → active WebSocket connection
_connections: dict[str, WebSocket] = {}

# agent_url → authenticated node identity observed at registration
_connection_identities: dict[str, NodeIdentityV1] = {}

# agent_url → per-connection tunnel handle and digest issued after node auth
_connection_tunnels: dict[str, tuple[str, str]] = {}

# request_id → Future awaiting agent result (collect/result path)
_pending: dict[str, asyncio.Future] = {}
_collect_owners: dict[str, WebSocket] = {}
_task_owners: dict[str, WebSocket] = {}

# request_id → Future awaiting the terminal agent_result (agent_task path)
_pending_agent_tasks: dict[str, asyncio.Future] = {}
_task_owners: dict[str, WebSocket] = {}

# request_id → (on_event callback, owning agent_url) for streaming agent_event dispatch
_agent_task_callbacks: dict[str, tuple[Callable[[dict[str, Any]], Any], str]] = {}

# request_id → Future awaiting a typed portal descriptor from its edge owner.
_pending_portal_prepares: dict[str, asyncio.Future[PortalOwnerRouteV1]] = {}
# request_id → owning edge WebSocket for response fencing and disconnect cleanup.
_portal_prepare_owners: dict[str, WebSocket] = {}


_PORTAL_QUEUE_MAX = 4
_PORTAL_MAX_WIRE_BYTES = 4_200_000


class PortalTransport:
    """Bounded, transient transport for one authenticated portal route."""

    def __init__(
        self,
        *,
        agent_url: str,
        websocket: WebSocket,
        route: PortalOwnerRouteV1,
    ) -> None:
        self.agent_url = agent_url
        self.websocket = websocket
        self.route = route
        self.portal_id = route.binding.session_id
        self._frames: asyncio.Queue[PortalWireFrameV1 | None] = asyncio.Queue(
            maxsize=_PORTAL_QUEUE_MAX
        )
        self._closed = False
        self._close_notified = False
        self._sequence = 0

    def _enqueue(self, frame: PortalWireFrameV1) -> None:
        if self._closed:
            return
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull as exc:
            self._closed = True
            raise RuntimeError("portal receive buffer is full") from exc

    def _finish(self) -> None:
        self._closed = True
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._frames.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def receive(self, *, timeout: float | None = None) -> PortalWireFrameV1 | None:
        """Await one validated frame; timeout raises, None is transport EOF."""
        if self._closed and self._frames.empty():
            return None
        item = (
            await asyncio.wait_for(self._frames.get(), timeout=timeout)
            if timeout is not None
            else await self._frames.get()
        )
        if item is None:
            return None
        return item

    async def send(self, frame: PortalWireFrameV1) -> None:
        """Validate and send one control frame to the real edge runtime."""
        if self._closed:
            raise RuntimeError("portal transport is closed")
        if frame.encoding != "control-json":
            raise ValueError("portal owner transport accepts control frames only")
        if frame.sequence <= self._sequence:
            raise ValueError("portal control sequence must increase")
        validated = validate_portal_frame_binding(self.route, frame)
        encoded = encode_portal_wire_frame(validated)
        if len(encoded) > _PORTAL_MAX_WIRE_BYTES:
            raise ValueError("portal wire frame exceeds transport limit")
        sender = getattr(self.websocket, "send_bytes", None)
        if callable(sender):
            await sender(encoded)
        else:
            await self.websocket.send(encoded)
        self._sequence = frame.sequence

    async def close(self, *, reason: str = "closed") -> None:
        """Explicitly close the transient route and release its buffers."""
        if self._closed:
            return
        try:
            await self.websocket.send_json(
                {"type": "portal_close", "portal_id": self.portal_id, "reason": reason[:128]}
            )
        except Exception:
            logger.debug("portal close send failed for %s", self.portal_id, exc_info=True)
        if _portal_transports.get(self.portal_id) is self:
            _portal_transports.pop(self.portal_id, None)
            _portal_ready.pop(self.portal_id, None)
        self._finish()


_portal_transports: dict[str, PortalTransport] = {}
_portal_ready: dict[str, asyncio.Future[None]] = {}


class BrowserDesktopTransport:
    """Bounded bidirectional RFB relay over one authenticated node socket."""

    def __init__(self, *, agent_url: str, websocket: WebSocket, route: BrowserDesktopRouteV1):
        self.agent_url = agent_url
        self.websocket = websocket
        self.route = route
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=DESKTOP_MAX_BUFFERED_FRAMES
        )
        self._closed = False
        self._close_notified = False
        self._send_sequence = 0
        self._receive_sequence = 0

    def _enqueue(self, sequence: int, payload: bytes) -> None:
        if self._closed:
            return
        if sequence <= self._receive_sequence:
            raise RuntimeError("desktop output sequence did not increase")
        try:
            self._frames.put_nowait(payload)
        except asyncio.QueueFull as exc:
            self._finish()
            raise RuntimeError("desktop receive buffer is full") from exc
        self._receive_sequence = sequence

    def _finish(self) -> None:
        self._closed = True
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._frames.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def receive(self) -> bytes | None:
        if self._closed and self._frames.empty():
            return None
        return await self._frames.get()

    async def send(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("desktop transport is closed")
        self._send_sequence += 1
        await self.websocket.send_bytes(
            encode_desktop_frame(self.route.route_id, 0, self._send_sequence, payload)
        )

    async def close(self, *, reason: str = "closed") -> None:
        if _desktop_transports.get(self.route.route_id) is self:
            _desktop_transports.pop(self.route.route_id, None)
        if _desktop_sessions.get(self.route.session_id) == self.route.route_id:
            _desktop_sessions.pop(self.route.session_id, None)
        ready = _desktop_ready.pop(self.route.route_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError("desktop route closed before opening"))
        self._finish()
        if self._close_notified:
            return
        self._close_notified = True
        try:
            await self.websocket.send_json(
                {
                    "type": "browser_desktop_close",
                    "route_id": self.route.route_id,
                    "reason": reason[:128],
                }
            )
        except Exception:
            logger.debug("desktop close send failed", exc_info=True)


_desktop_transports: dict[str, BrowserDesktopTransport] = {}
_desktop_sessions: dict[str, str] = {}
_desktop_ready: dict[str, asyncio.Future[None]] = {}


async def open_browser_desktop_route(
    agent_url: str,
    session: SessionEnvelopeV1,
    *,
    expires_at,
    timeout: float = 15.0,
) -> BrowserDesktopTransport:
    """Open a VNC route whose destination can only be resolved by the edge binding."""

    envelope = SessionEnvelopeV1.model_validate(session)
    if envelope.purpose != "browser" or envelope.profile_id is None:
        raise ValueError("desktop routes require a live browser profile")
    websocket = _connections.get(agent_url)
    identity = _connection_identities.get(agent_url)
    tunnel = _connection_tunnels.get(agent_url)
    if websocket is None or identity is None or tunnel is None:
        raise RuntimeError("desktop node is not connected through an authenticated tunnel")
    if (identity.node_id, identity.boot_id) != (envelope.node_id, envelope.node_boot_id):
        raise RuntimeError("desktop node identity does not own the session")
    scope = getattr(websocket, "scope", None)
    if isinstance(scope, dict) and scope.get("scheme") not in {"https", "wss"}:
        raise RuntimeError("desktop node transport requires TLS WebSocket")
    if envelope.session_id in _desktop_sessions:
        raise RuntimeError("desktop session already has an active viewer")
    route = BrowserDesktopRouteV1(
        route_id=str(uuid.uuid4()),
        workspace_id=envelope.workspace_id,
        account_id=envelope.account_id,
        session_id=envelope.session_id,
        profile_id=envelope.profile_id,
        node_id=envelope.node_id,
        boot_id=envelope.node_boot_id,
        lease_id=envelope.lease_id,
        command_id=envelope.command_id,
        epoch=envelope.epoch,
        tunnel_handle=tunnel[0],
        tunnel_auth_digest=tunnel[1],
        expires_at=expires_at,
    )
    transport = BrowserDesktopTransport(agent_url=agent_url, websocket=websocket, route=route)
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _desktop_transports[route.route_id] = transport
    _desktop_sessions[route.session_id] = route.route_id
    _desktop_ready[route.route_id] = ready
    try:
        await websocket.send_json(
            {"type": "browser_desktop_open", "route": route.model_dump(mode="json")}
        )
        await asyncio.wait_for(ready, timeout=timeout)
        return transport
    except BaseException:
        _desktop_transports.pop(route.route_id, None)
        _desktop_sessions.pop(route.session_id, None)
        _desktop_ready.pop(route.route_id, None)
        transport._finish()
        try:
            await websocket.send_json(
                {
                    "type": "browser_desktop_close",
                    "route_id": route.route_id,
                    "reason": "desktop_open_failed",
                }
            )
        except Exception:
            logger.debug("desktop open rollback send failed", exc_info=True)
        raise


def resolve_browser_desktop_ready(
    agent_url: str, msg: dict[str, Any], source_ws: WebSocket | None = None
) -> None:
    route_id = msg.get("route_id", "")
    transport = _desktop_transports.get(route_id)
    if (
        transport is None
        or transport.agent_url != agent_url
        or (source_ws is not None and transport.websocket is not source_ws)
    ):
        logger.warning("WS: unexpected desktop acknowledgement")
        return
    ready = _desktop_ready.get(route_id)
    message_type = msg.get("type")
    if message_type == "browser_desktop_closed":
        transport._finish()
        if _desktop_transports.get(route_id) is transport:
            _desktop_transports.pop(route_id, None)
        if _desktop_sessions.get(transport.route.session_id) == route_id:
            _desktop_sessions.pop(transport.route.session_id, None)
        _desktop_ready.pop(route_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError("desktop route ended before opening"))
        return
    if ready is None or ready.done():
        return
    if message_type == "browser_desktop_ready":
        ready.set_result(None)
    else:
        error = RuntimeError(str(msg.get("error") or "desktop route admission failed"))
        transport._finish()
        _desktop_transports.pop(route_id, None)
        _desktop_sessions.pop(transport.route.session_id, None)
        _desktop_ready.pop(route_id, None)
        ready.set_exception(error)


async def resolve_browser_desktop_binary(
    agent_url: str, data: bytes, source_ws: WebSocket | None = None
) -> None:
    if source_ws is not None and _connections.get(agent_url) is not source_ws:
        return
    transport: BrowserDesktopTransport | None = None
    try:
        route_id, direction, sequence, payload = decode_desktop_frame(data)
        if direction != 1:
            raise ValueError("edge desktop frames must flow toward the center")
        transport = _desktop_transports.get(route_id)
        if (
            transport is None
            or transport.agent_url != agent_url
            or (source_ws is not None and transport.websocket is not source_ws)
        ):
            return
        transport._enqueue(sequence, payload)
    except (ValueError, RuntimeError) as exc:
        logger.warning("WS: desktop frame rejected: %s", exc)
        if transport is not None:
            await transport.close(reason="desktop_frame_rejected")
        else:
            await _abort_desktops_for_agent(agent_url, source_ws)


async def _abort_desktops_for_agent(
    agent_url: str,
    source_ws: WebSocket | None = None,
) -> None:
    transports = [
        transport
        for transport in tuple(_desktop_transports.values())
        if transport.agent_url == agent_url
        and (source_ws is None or transport.websocket is source_ws)
    ]
    await asyncio.gather(
        *(transport.close(reason="desktop_transport_aborted") for transport in transports),
        return_exceptions=True,
    )


def register_connection(
    agent_url: str,
    ws: WebSocket,
    node_identity: NodeIdentityV1 | None = None,
    *,
    tunnel_handle: str | None = None,
    tunnel_auth_digest: str | None = None,
) -> None:
    """Record a newly-established WS connection and its node identity."""
    if bool(tunnel_handle) != bool(tunnel_auth_digest):
        raise ValueError("portal tunnel binding must include handle and digest")
_agent_task_terminal_results: dict[str, asyncio.Future[dict[str, Any]]] = {}
_dispatch_observer: ContextVar[Callable | None] = ContextVar(
    "agent_dispatch_observer", default=None
)
_RETAINED_TASK_LIMIT = 1024
_RETAINED_TASK_SECONDS = 3600
_STATUS_PROBE_LIMIT = 128
_TERMINAL_FRAME_HEADER = struct.Struct("!B36s36sQ")
_TERMINAL_INPUT = 1
_TERMINAL_OUTPUT = 2
_TERMINAL_BROADCAST_ATTACHMENT = "00000000-0000-0000-0000-000000000000"
_TERMINAL_MAX_PAYLOAD = 64 * 1024
_TERMINAL_CONTROL_TIMEOUT = 15.0


@dataclass
class _RetainedAgentTask:
    agent_url: str
    owner: WebSocket | None
    expires_at: float
    terminal: dict[str, Any] | None = None


_retained_agent_tasks: OrderedDict[str, _RetainedAgentTask] = OrderedDict()


@dataclass
class _AgentTaskStatusProbe:
    agent_url: str
    task_id: str
    owner: WebSocket
    future: asyncio.Future[dict[str, Any]]


_agent_task_status_probes: dict[str, _AgentTaskStatusProbe] = {}


@dataclass
class _TerminalControlRequest:
    agent_url: str
    owner: WebSocket
    future: asyncio.Future[dict[str, Any]]


@dataclass
class TerminalAttachment:
    agent_url: str
    session_id: str
    attachment_id: str
    queue: asyncio.Queue[dict[str, Any] | bytes]


_terminal_control_requests: dict[str, _TerminalControlRequest] = {}
_terminal_attachments: dict[str, TerminalAttachment] = {}
_terminal_routes: dict[str, str] = {}
_TERMINAL_ATTACHMENTS_PER_SESSION = 8


def agent_task_key(agent_url: str) -> str:
    return hashlib.sha256(agent_url.encode()).hexdigest()


def _canonical_uuid(value: str, field: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if parsed != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return parsed


def encode_terminal_frame(
    kind: int,
    session_id: str,
    attachment_id: str,
    sequence: int,
    payload: bytes,
) -> bytes:
    _canonical_uuid(session_id, "session_id")
    _canonical_uuid(attachment_id, "attachment_id")
    if kind not in {_TERMINAL_INPUT, _TERMINAL_OUTPUT}:
        raise ValueError("unsupported terminal frame kind")
    if not isinstance(payload, bytes) or not 0 < len(payload) <= _TERMINAL_MAX_PAYLOAD:
        raise ValueError("terminal payload must contain 1..65536 bytes")
    if not isinstance(sequence, int) or sequence < 0:
        raise ValueError("terminal sequence must be non-negative")
    return _TERMINAL_FRAME_HEADER.pack(
        kind,
        session_id.encode("ascii"),
        attachment_id.encode("ascii"),
        sequence,
    ) + payload


def decode_terminal_frame(frame: bytes) -> tuple[int, str, str, int, bytes]:
    if not isinstance(frame, bytes) or len(frame) <= _TERMINAL_FRAME_HEADER.size:
        raise ValueError("malformed terminal frame")
    kind, session_raw, attachment_raw, sequence = _TERMINAL_FRAME_HEADER.unpack_from(frame)
    session_id = _canonical_uuid(session_raw.decode("ascii"), "session_id")
    attachment_id = _canonical_uuid(attachment_raw.decode("ascii"), "attachment_id")
    payload = frame[_TERMINAL_FRAME_HEADER.size :]
    if kind not in {_TERMINAL_INPUT, _TERMINAL_OUTPUT} or len(payload) > _TERMINAL_MAX_PAYLOAD:
        raise ValueError("malformed terminal frame")
    return kind, session_id, attachment_id, sequence, payload


@contextmanager
def observe_agent_dispatch(observer: Callable):
    """Persist caller-owned correlation before a strict dispatch reaches the wire."""
    token = _dispatch_observer.set(observer)
    try:
        yield
    finally:
        _dispatch_observer.reset(token)


def _prune_retained_tasks() -> None:
    now = time.monotonic()
    for request_id, entry in list(_retained_agent_tasks.items()):
        if entry.expires_at <= now:
            _retained_agent_tasks.pop(request_id, None)
    while len(_retained_agent_tasks) > _RETAINED_TASK_LIMIT:
        _retained_agent_tasks.popitem(last=False)


def confirmed_agent_terminal(agent_key: str, request_id: str) -> dict[str, Any] | None:
    """Return only bounded cleanup evidence, never native output or credentials."""
    _prune_retained_tasks()
    entry = _retained_agent_tasks.get(request_id)
    if entry is None or agent_task_key(entry.agent_url) != agent_key or entry.terminal is None:
        return None
    return dict(entry.terminal)


def forget_agent_terminal(agent_key: str, request_id: str) -> None:
    entry = _retained_agent_tasks.get(request_id)
    if entry is not None and agent_task_key(entry.agent_url) == agent_key:
        _retained_agent_tasks.pop(request_id, None)


class AgentTaskUnresolvedError(RuntimeError):
    """Remote execution may still be running; callers must not persist cancellation."""

    code = "remote_execution_unconfirmed"

    def __init__(self, agent_url: str, request_id: str, reason: str):
        self.agent_url = agent_url
        self.request_id = request_id
        self.reason = reason
        super().__init__("Remote execution is unconfirmed; it may still be running.")


def register_connection(agent_url: str, ws: WebSocket, node_identity: NodeIdentityV1 | None = None, *, tunnel_handle: str | None = None, tunnel_auth_digest: str | None = None) -> None:
    """Record a newly-established WS connection for agent_url."""
    previous = _connections.get(agent_url)
    if previous is not None and previous is not ws:
        unregister_connection(agent_url, previous)
        asyncio.get_running_loop().create_task(previous.close(code=1012))
    _connections[agent_url] = ws
    if node_identity is not None:
        _connection_identities[agent_url] = node_identity
        if tunnel_handle and tunnel_auth_digest:
            _connection_tunnels[agent_url] = (tunnel_handle, tunnel_auth_digest)
        else:
            _connection_tunnels.pop(agent_url, None)
    else:
        _connection_identities.pop(agent_url, None)
        _connection_tunnels.pop(agent_url, None)
    logger.info("WS agent connected: %s (total=%d)", agent_url, len(_connections))


def unregister_connection(agent_url: str, source_ws: WebSocket | None = None) -> bool:
    """Fail this transport's pending work without unregistering a replacement."""
    current = _connections.get(agent_url)
    if current is None or (source_ws is not None and current is not source_ws):
        return False

    for request_id, owner in tuple(_collect_owners.items()):
        if owner is not current:
            continue
        future = _pending.get(request_id)
        if future is not None and not future.done():
            future.set_exception(RuntimeError("Agent disconnected before collection completed"))
        _collect_owners.pop(request_id, None)

    for probe in list(_agent_task_status_probes.values()):
        if probe.owner is current and not probe.future.done():
            probe.future.set_exception(
                AgentTaskUnresolvedError(probe.agent_url, probe.task_id, "agent_disconnected")
            )
    for request in list(_terminal_control_requests.values()):
        if request.owner is current and not request.future.done():
            request.future.set_exception(RuntimeError("Native terminal node disconnected"))
    for attachment_id, attachment in list(_terminal_attachments.items()):
        if attachment.agent_url != agent_url:
            continue
        reconnecting = {"type": "transport", "status": "reconnecting", "recoverable": True}
        try:
            attachment.queue.put_nowait(reconnecting)
        except asyncio.QueueFull:
            attachment.queue.get_nowait()
            attachment.queue.put_nowait(reconnecting)
        _terminal_attachments.pop(attachment_id, None)
    _connections.pop(agent_url, None)
    _connection_identities.pop(agent_url, None)
    _connection_tunnels.pop(agent_url, None)
    for portal_id, transport in tuple(_portal_transports.items()):
        if transport.websocket is not current:
            continue
        transport._finish()
        _portal_transports.pop(portal_id, None)
        ready = _portal_ready.pop(portal_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError("portal owner disconnected before opening"))
    for route_id, transport in tuple(_desktop_transports.items()):
        if transport.websocket is not current:
            continue
        transport._finish()
        _desktop_transports.pop(route_id, None)
        _desktop_sessions.pop(transport.route.session_id, None)
        ready = _desktop_ready.pop(route_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError("desktop owner disconnected before opening"))
    logger.info("WS agent disconnected: %s (remaining=%d)", agent_url, len(_connections))

    dead_request_ids = [
        request_id for request_id, owner in _task_owners.items() if owner is current
    ]
    for request_id in dead_request_ids:
        terminal = _agent_task_terminal_results.get(request_id)
        if terminal is not None and not terminal.done():
            terminal.set_exception(
                AgentTaskUnresolvedError(agent_url, request_id, "agent_disconnected")
            )
        fut = _pending_agent_tasks.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(
                {
                    "type": "error",
                    "task_id": request_id,
                    "message": f"WS agent {agent_url!r} disconnected before task completed",
                    "error_type": "AgentDisconnected",
                }
            )
        _task_owners.pop(request_id, None)
        _agent_task_callbacks.pop(request_id, None)
    for request_id, owner in tuple(_portal_prepare_owners.items()):
        if owner is not current:
            continue
        future = _pending_portal_prepares.get(request_id)
        if future is not None and not future.done():
            future.set_exception(RuntimeError("portal owner disconnected before preparation"))
        _portal_prepare_owners.pop(request_id, None)
    return True


async def prepare_portal_route(
    agent_url: str,
    session_envelope: SessionEnvelopeV1,
    *,
    session_revision: int,
    timeout: float = 15,
) -> PortalOwnerRouteV1:
    """Ask the authenticated edge owner to resolve its real portal session."""

    if (
        isinstance(session_revision, bool)
        or not isinstance(session_revision, int)
        or session_revision < 0
    ):
        raise ValueError("portal route requires an authorized session revision")
    if not 0 < timeout <= 30:
        raise ValueError("portal route timeout must be between zero and thirty seconds")
    session = SessionEnvelopeV1.model_validate(session_envelope)
    identity = _connection_identities.get(agent_url)
    websocket = _connections.get(agent_url)
    if websocket is None:
        raise RuntimeError("portal node is not connected")
    if identity is None or (
        identity.node_id != session.node_id or identity.boot_id != session.node_boot_id
    ):
        raise RuntimeError("portal node identity is not authorized for this session")
    if agent_url not in _connection_tunnels:
        raise RuntimeError("portal connection has no authenticated tunnel binding")
    request_id = str(uuid.uuid4())
    future: asyncio.Future[PortalOwnerRouteV1] = asyncio.get_running_loop().create_future()
    _pending_portal_prepares[request_id] = future
    _portal_prepare_owners[request_id] = websocket
    try:
        await websocket.send_json(
            {
                "type": "portal_prepare",
                "request_id": request_id,
                "session": session.model_dump(mode="json"),
                "agent_url": agent_url,
                "session_revision": session_revision,
                "timeout": timeout,
            }
        )
        return await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError("portal owner did not prepare a route before timeout") from exc
    finally:
        _pending_portal_prepares.pop(request_id, None)
        _portal_prepare_owners.pop(request_id, None)


async def open_portal_route(
    agent_url: str,
    owner_route: PortalOwnerRouteV1,
    *,
    timeout: float = 15.0,
) -> PortalTransport:
    """Open a server-resolved portal route on an authenticated node WS."""
    route = PortalOwnerRouteV1.model_validate(owner_route)
    if not route.owner_endpoint.startswith("https://"):
        raise ValueError("portal owner endpoint must use TLS")
    websocket = _connections.get(agent_url)
    if websocket is None:
        raise RuntimeError(f"No active WS connection for agent: {agent_url}")
    scope = getattr(websocket, "scope", None)
    if isinstance(scope, dict) and scope.get("scheme") not in {"https", "wss"}:
        raise RuntimeError("portal node transport requires TLS WebSocket")
    identity = _connection_identities.get(agent_url)
    if identity is None or identity != route.node_identity:
        raise RuntimeError("portal node identity is not authenticated on this connection")
    expected_tunnel = _connection_tunnels.get(agent_url)
    if expected_tunnel != (route.tunnel_handle, route.tunnel_auth_digest):
        raise RuntimeError("portal route is not bound to the authenticated connection")
    portal_id = route.binding.session_id
    existing = _portal_transports.get(portal_id)
    if existing is not None:
        raise RuntimeError("portal session already has an active transport")
    transport = PortalTransport(agent_url=agent_url, websocket=websocket, route=route)
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    _portal_transports[portal_id] = transport
    _portal_ready[portal_id] = ready
    try:
        await websocket.send_json(
            {
                "type": "portal_open",
                "portal_id": portal_id,
                "route": route.model_dump(mode="json"),
            }
        )
        await asyncio.wait_for(ready, timeout=timeout)
        return transport
    except BaseException:
        if _portal_transports.get(portal_id) is transport:
            _portal_transports.pop(portal_id, None)
        if _portal_ready.get(portal_id) is ready:
            _portal_ready.pop(portal_id, None)
        transport._finish()
        raise


async def resolve_portal_ready(
    agent_url: str,
    msg: dict[str, Any],
    source_ws: WebSocket | None = None,
) -> None:
    """Resolve an edge portal_open acknowledgement from its owning transport."""
    portal_id = msg.get("portal_id", "")
    transport = _portal_transports.get(portal_id)
    if (
        transport is None
        or transport.agent_url != agent_url
        or (source_ws is not None and transport.websocket is not source_ws)
    ):
        logger.warning("WS: unexpected portal_ready for portal_id=%s", portal_id)
        return
    ready = _portal_ready.get(portal_id)
    if msg.get("type") == "portal_error":
        error = RuntimeError(str(msg.get("error") or "portal open failed"))
        transport._finish()
        _portal_transports.pop(portal_id, None)
        _portal_ready.pop(portal_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(error)
        return
    if ready is None or ready.done():
        return
    if msg.get("type") != "portal_ready":
        ready.set_exception(RuntimeError("portal open failed"))
    else:
        ready.set_result(None)


def _abort_portals_for_agent(
    agent_url: str,
    source_ws: WebSocket | None = None,
) -> None:
    for portal_id, transport in tuple(_portal_transports.items()):
        if transport.agent_url != agent_url:
            continue
        if source_ws is not None and transport.websocket is not source_ws:
            continue
        transport._finish()
        _portal_transports.pop(portal_id, None)
        ready = _portal_ready.pop(portal_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError("portal transport was aborted"))


async def resolve_portal_binary(
    agent_url: str,
    data: bytes,
    source_ws: WebSocket | None = None,
) -> None:
    """Decode, route-check, and enqueue one transient binary portal frame."""
    if source_ws is not None and _connections.get(agent_url) is not source_ws:
        logger.warning("WS: portal frame came from a stale connection for %s", agent_url)
        return
    if len(data) > _PORTAL_MAX_WIRE_BYTES:
        logger.warning("WS: oversized portal frame from %s", agent_url)
        _abort_portals_for_agent(agent_url, source_ws)
        return
    try:
        frame = decode_portal_wire_frame(data)
    except ValueError:
        logger.warning("WS: invalid portal wire frame from %s", agent_url)
        _abort_portals_for_agent(agent_url, source_ws)
        return
    transport = _portal_transports.get(frame.transient.binding.session_id)
    if (
        transport is None
        or transport.agent_url != agent_url
        or (source_ws is not None and transport.websocket is not source_ws)
    ):
        logger.warning(
            "WS: portal frame has no active route session=%s",
            frame.transient.binding.session_id,
        )
        return
    try:
        validate_portal_frame_binding(transport.route, frame)
        if frame.encoding != "pixel-binary":
            raise ValueError("portal edge may only send pixel frames")
        transport._enqueue(frame)
    except (ValueError, RuntimeError) as exc:
        logger.warning("WS: portal frame rejected: %s", exc)
        transport._finish()
        _portal_transports.pop(transport.portal_id, None)
        ready = _portal_ready.pop(transport.portal_id, None)
        if ready is not None and not ready.done():
            ready.set_exception(exc)


async def _send_terminal_control(
    agent_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = _TERMINAL_CONTROL_TIMEOUT,
) -> dict[str, Any]:
    ws = _connections.get(agent_url)
    if ws is None:
        raise RuntimeError("Native terminal node is unavailable")
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    _terminal_control_requests[request_id] = _TerminalControlRequest(
        agent_url=agent_url,
        owner=ws,
        future=future,
    )
    session_id = payload.get("session_id")
    if isinstance(session_id, str):
        _terminal_routes[_canonical_uuid(session_id, "session_id")] = agent_url
    try:
        await ws.send_json({**payload, "request_id": request_id})
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        _terminal_control_requests.pop(request_id, None)


async def start_native_terminal(
    agent_url: str,
    *,
    session_id: str,
    runtime: str,
    cwd: str,
    initial_input: str,
    cols: int,
    rows: int,
    native_chat_authorized: bool = False,
) -> dict[str, Any]:
    if not native_chat_authorized or runtime not in {"codex", "omp"}:
        raise PermissionError("Native terminal dispatch requires an authorized binding")
    return await _send_terminal_control(
        agent_url,
        {
            "type": "terminal_start",
            "session_id": session_id,
            "runtime": runtime,
            "cwd": cwd,
            "initial_input": initial_input,
            "cols": cols,
            "rows": rows,
        },
    )


async def query_native_terminal(
    agent_url: str, session_id: str, *, native_chat_authorized: bool = False
) -> dict[str, Any]:
    if not native_chat_authorized:
        raise PermissionError("Native terminal status requires an authorized binding")
    result = await _send_terminal_control(
        agent_url,
        {"type": "terminal_status", "session_id": session_id},
    )
    if result.get("status") == "exited" and result.get("cleanup_complete") is True:
        _terminal_routes.pop(session_id, None)
    return result


async def stop_native_terminal(
    agent_url: str, session_id: str, *, native_chat_authorized: bool = False
) -> dict[str, Any]:
    if not native_chat_authorized:
        raise PermissionError("Native terminal stop requires an authorized binding")
    result = await _send_terminal_control(
        agent_url,
        {"type": "terminal_stop", "session_id": session_id},
        timeout=45.0,
    )
    if result.get("status") == "exited" and result.get("cleanup_complete") is True:
        _terminal_routes.pop(session_id, None)
    return result


async def attach_native_terminal(
    agent_url: str,
    session_id: str,
    controller_id: str,
    *,
    takeover: bool = False,
    native_chat_authorized: bool = False,
) -> TerminalAttachment:
    if not native_chat_authorized:
        raise PermissionError("Native terminal attach requires an authorized binding")
    if sum(
        attachment.session_id == session_id for attachment in _terminal_attachments.values()
    ) >= _TERMINAL_ATTACHMENTS_PER_SESSION:
        raise RuntimeError("Native terminal attachment limit reached")
    attachment_id = str(uuid.uuid4())
    _canonical_uuid(controller_id, "controller_id")
    attachment = TerminalAttachment(
        agent_url=agent_url,
        session_id=_canonical_uuid(session_id, "session_id"),
        attachment_id=attachment_id,
        queue=asyncio.Queue(maxsize=256),
    )
    _terminal_attachments[attachment_id] = attachment
    try:
        result = await _send_terminal_control(
            agent_url,
            {
                "type": "terminal_attach",
                "session_id": session_id,
                "attachment_id": attachment_id,
                "controller_id": controller_id,
                "takeover": takeover,
            },
        )
        if result.get("status") not in {"active", "stopping", "exited"}:
            raise RuntimeError("Native terminal session is not attachable")
        return attachment
    except BaseException:
        _terminal_attachments.pop(attachment_id, None)
        raise


async def detach_native_terminal(attachment: TerminalAttachment) -> None:
    _terminal_attachments.pop(attachment.attachment_id, None)
    ws = _connections.get(attachment.agent_url)
    if ws is not None:
        await ws.send_json(
            {
                "type": "terminal_detach",
                "session_id": attachment.session_id,
                "attachment_id": attachment.attachment_id,
            }
        )


async def send_native_terminal_input(attachment: TerminalAttachment, payload: bytes) -> None:
    current = _terminal_attachments.get(attachment.attachment_id)
    ws = _connections.get(attachment.agent_url)
    if current is not attachment or ws is None:
        raise RuntimeError("Native terminal attachment is unavailable")
    await ws.send_bytes(
        encode_terminal_frame(
            _TERMINAL_INPUT,
            attachment.session_id,
            attachment.attachment_id,
            0,
            payload,
        )
    )


async def resize_native_terminal(
    attachment: TerminalAttachment, *, cols: int, rows: int
) -> None:
    if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
        raise ValueError("terminal dimensions must be between 1 and 1000")
    ws = _connections.get(attachment.agent_url)
    if _terminal_attachments.get(attachment.attachment_id) is not attachment or ws is None:
        raise RuntimeError("Native terminal attachment is unavailable")
    await ws.send_json(
        {
            "type": "terminal_resize",
            "session_id": attachment.session_id,
            "attachment_id": attachment.attachment_id,
            "cols": cols,
            "rows": rows,
        }
    )


async def takeover_native_terminal(
    attachment: TerminalAttachment, controller_id: str
) -> dict[str, Any]:
    return await _send_terminal_control(
        attachment.agent_url,
        {
            "type": "terminal_takeover",
            "session_id": attachment.session_id,
            "attachment_id": attachment.attachment_id,
            "controller_id": _canonical_uuid(controller_id, "controller_id"),
        },
    )


def resolve_terminal_response(msg: dict[str, Any], source_ws: WebSocket) -> None:
    request_id = msg.get("request_id")
    request = _terminal_control_requests.get(request_id) if isinstance(request_id, str) else None
    if request is None or request.owner is not source_ws or request.future.done():
        return
    response = msg.get("response")
    if not isinstance(response, dict):
        request.future.set_exception(RuntimeError("Malformed native terminal response"))
        return
    request.future.set_result(response)


async def resolve_terminal_event(msg: dict[str, Any], source_ws: WebSocket) -> None:
    session_id = msg.get("session_id")
    event = msg.get("event")
    if not isinstance(session_id, str) or not isinstance(event, dict):
        return
    if _connections.get(_terminal_routes.get(session_id, "")) is not source_ws:
        return
    attachment_id = msg.get("attachment_id")
    targets = (
        [_terminal_attachments.get(attachment_id)]
        if isinstance(attachment_id, str)
        else [
            attachment
            for attachment in _terminal_attachments.values()
            if attachment.session_id == session_id
        ]
    )
    for attachment in targets:
        if attachment is not None and attachment.session_id == session_id:
            await _enqueue_terminal_item(attachment, event, source_ws)
    if event.get("type") == "exit" and event.get("cleanup_complete") is True:
        _terminal_routes.pop(session_id, None)


async def _enqueue_terminal_item(
    attachment: TerminalAttachment, item: bytes | dict[str, Any], source_ws: WebSocket
) -> None:
    try:
        attachment.queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        _terminal_attachments.pop(attachment.attachment_id, None)
    while not attachment.queue.empty():
        attachment.queue.get_nowait()
    attachment.queue.put_nowait(
        {"type": "transport", "status": "reconnecting", "recoverable": True}
    )
    try:
        await source_ws.send_json(
            {
                "type": "terminal_detach",
                "session_id": attachment.session_id,
                "attachment_id": attachment.attachment_id,
            }
        )
    except Exception:
        return


async def resolve_terminal_binary(frame: bytes, source_ws: WebSocket) -> None:
    kind, session_id, attachment_id, _sequence, payload = decode_terminal_frame(frame)
    if kind != _TERMINAL_OUTPUT:
        return
    agent_url = _terminal_routes.get(session_id)
    if agent_url is None or _connections.get(agent_url) is not source_ws:
        return
    targets = (
        [
            attachment
            for attachment in _terminal_attachments.values()
            if attachment.session_id == session_id
        ]
        if attachment_id == _TERMINAL_BROADCAST_ATTACHMENT
        else [_terminal_attachments.get(attachment_id)]
    )
    for attachment in targets:
        if attachment is not None and attachment.session_id == session_id:
            await _enqueue_terminal_item(attachment, payload, source_ws)


def is_connected(agent_url: str) -> bool:
    return agent_url in _connections


def owns_connection(agent_url: str, source_ws: WebSocket) -> bool:
    """Return whether *source_ws* is the currently registered transport."""
    return _connections.get(agent_url) is source_ws


def list_connected() -> list[str]:
    return list(_connections.keys())


def account_connection_identity(agent_url: str) -> NodeIdentityV1 | None:
    return _connection_identities.get(agent_url) if agent_url in _connections else None


async def send_account_lease_renewal(
    agent_url: str, claim: NodeClaimV1, session: SessionEnvelopeV1,
) -> None:
    identity = NodeIdentityV1(node_id=claim.node_id, boot_id=claim.boot_id)
    ws = _connections.get(agent_url)
    if ws is None or _connection_identities.get(agent_url) != identity:
        raise RuntimeError("account connection generation changed")
    if (session.node_id, session.node_boot_id, session.command_id) != (
        claim.node_id, claim.boot_id, claim.command_id,
    ):
        raise ValueError("lease renewal session mismatch")
    await ws.send_json({
        "type": "account_lease_renewed", "claim": claim.to_wire(),
        "session": session.to_wire(), "node_identity": identity.to_wire(),
    })
def _native_chat_binding_matches(agent_url: str) -> list[Any]:
    from backend.config import get_settings
    from backend.services.agent_native_chat import NativeChatBinding

    try:
        bindings = [
            NativeChatBinding.model_validate(entry) for entry in get_settings().native_chat_bindings
        ]
    except (ValueError, TypeError) as exc:
        raise PermissionError("Native chat node reservation configuration is invalid") from exc
    return [entry for entry in bindings if entry.agent_url == agent_url.strip().rstrip("/")]


async def dispatch_collect(
    agent_url: str,
    site: str,
    command: str,
    args: dict[str, Any],
    positional_args: list[str],
    output_format: str,
    mode: str,
    timeout: float | None = None,
    request_id: str | None = None,
    account_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a collect task to a WS agent and await the result dict.

    Raises:
        RuntimeError: agent is not connected.
        TimeoutError: agent did not respond within *timeout* seconds.
    """
    if _native_chat_binding_matches(agent_url):
        raise PermissionError("This node is reserved for authorized workspace native chat")
    if timeout is None:
        from backend.config import get_settings

        timeout = float(get_settings().agent_ws_timeout)

    ws = _connections.get(agent_url)
    if ws is None:
        raise RuntimeError(f"No active WS connection for agent: {agent_url}")
    if account_session is not None:
        session = SessionEnvelopeV1.model_validate(account_session)
        identity = _connection_identities.get(agent_url)
        if identity is None or (
            identity.node_id != session.node_id or identity.boot_id != session.node_boot_id
        ):
            raise RuntimeError("account collection session is not owned by this node")
        account_session = session.to_wire()

    request_id = request_id or str(uuid.uuid4())
    if request_id in _pending or request_id in _pending_agent_tasks:
        raise ValueError("request_id is already active")
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    _pending[request_id] = fut
    _collect_owners[request_id] = ws

    try:
        await ws.send_json(
            {
                "type": "collect",
                "request_id": request_id,
                "site": site,
                "command": command,
                "args": args,
                "positional_args": positional_args,
                "format": output_format,
                "mode": mode,
                **({"account_session": account_session} if account_session is not None else {}),
            }
        )
        logger.debug(
            "WS dispatch | agent=%s request_id=%s site=%s cmd=%s",
            agent_url,
            request_id,
            site,
            command,
        )
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"WS agent {agent_url!r} did not respond in {timeout}s")
    except asyncio.CancelledError:
        await ws.send_json({"type": "cancel", "request_id": request_id})
        raise
    finally:
        _pending.pop(request_id, None)
        _collect_owners.pop(request_id, None)


def resolve_response(
    request_id: str,
    result: dict[str, Any],
    source_ws: WebSocket | None = None,
) -> None:
    """Called from the WS receive loop when an agent returns a 'result' message."""
    fut = _pending.get(request_id)
    if source_ws is not None and _collect_owners.get(request_id) is not source_ws:
        return
    if fut is None or fut.done():
        logger.warning("WS: unexpected result for request_id=%s (no waiting future)", request_id)
        return
    fut.set_result(result)


async def send_agent_task(
    agent_url: str,
    task: dict[str, Any],
    on_event: Callable[[dict[str, Any]], Any],
    timeout: float = 600.0,
    *,
    require_cancel_ack: bool = False,
    cancel_ack_timeout: float = 30.0,
    native_chat_authorized: bool = False,
) -> dict[str, Any]:
    """Send one server-resolved runtime envelope over the reverse channel.

    *on_event* is called once per ``agent_event`` frame with that frame's
    ``event`` payload. It may be a plain sync callable or an async callable
    (coroutine function) — both are supported, matching the flexibility the
    edge side (adapters) already assumes for callers.

    With ``require_cancel_ack``, every remote terminal must include the matching
    ``task_id`` and ``cleanup_complete: true``. Cancellation requires an error
    terminal with ``error_type: CancelledError`` after process-tree cleanup.
    A completion racing cancellation is returned instead. The additional cleanup
    deadline includes sending the cancellation request; repeated local cancellation
    cannot interrupt it. Unconfirmed outcomes carry the dispatch identity for
    caller-owned durable reconciliation, not a claim that execution has stopped.

    Raises:
        RuntimeError: agent is not connected.
        AgentTaskUnresolvedError: strict dispatch could not confirm remote cleanup.
        TimeoutError: agent did not respond within *timeout* seconds. Pending
            bookkeeping (the callback registration) is cleaned up either way.
    """
    reserved = _native_chat_binding_matches(agent_url)
    if reserved or task.get("workflow") == "operator_chat" or native_chat_authorized:
        if not (
            native_chat_authorized
            and require_cancel_ack
            and task.get("workflow") == "operator_chat"
            and any(entry.runtime_id == task.get("runtime") for entry in reserved)
        ):
            raise PermissionError("This node is reserved for authorized workspace native chat")
    if require_cancel_ack:
        for name, duration in (("timeout", timeout), ("cancel_ack_timeout", cancel_ack_timeout)):
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
    ws = _connections.get(agent_url)
    if ws is None:
        raise RuntimeError(f"No active WS connection for agent: {agent_url}")

    payload = dict(task)
    account_fields = ("command", "claim", "session", "node_identity")
    if any(payload.get(field) is not None for field in account_fields):
        try:
            command = DurableCommandV1.model_validate(payload.get("command"))
            claim = NodeClaimV1.model_validate(payload.get("claim"))
            session = SessionEnvelopeV1.model_validate(payload.get("session"))
            identity = NodeIdentityV1.model_validate(payload.get("node_identity"))
            if identity.node_id != claim.node_id or identity.boot_id != claim.boot_id:
                raise ValueError("runtime node identity does not match claim")
            if _connection_identities.get(agent_url) != identity:
                raise ValueError("runtime node identity does not match authenticated socket")
            CommandExecutionGuardV1(command=command, claim=claim, session=session)
        except ValueError as exc:
            raise ValueError("invalid account runtime envelope") from exc
        config = payload.get("config") or {}
        forbidden = {
            "remote",
            "binary",
            "cdp_endpoint",
            "endpoint",
            "daemon_endpoint",
            "profile_dir",
            "home_dir",
            "cache_dir",
            "display",
        }
        if forbidden.intersection(config):
            raise ValueError("client runtime routing override is forbidden")
        payload.update(
            {
                "command": command.model_dump(mode="json"),
                "claim": claim.model_dump(mode="json"),
                "session": session.model_dump(mode="json"),
                "node_identity": identity.model_dump(mode="json"),
            }
        )

    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    _pending_agent_tasks[request_id] = fut
    _task_owners[request_id] = ws
    _agent_task_callbacks[request_id] = (on_event, agent_url)
    terminal = loop.create_future() if require_cancel_ack else None
    dispatched = False
    if terminal is not None:
        _agent_task_terminal_results[request_id] = terminal

    try:
        if terminal is not None:
            observer = _dispatch_observer.get()
            if observer is not None:
                await observer(agent_url, request_id)
            _retained_agent_tasks[request_id] = _RetainedAgentTask(
                agent_url, ws, time.monotonic() + _RETAINED_TASK_SECONDS
            )
            _prune_retained_tasks()
            async with asyncio.timeout(timeout):
                dispatched = True
                await ws.send_json(
                    {
                        **payload,
                        "type": "agent_task",
                        "request_id": request_id,
                        "require_cancel_ack": True,
                    }
                )
                result = await asyncio.shield(fut)
            if not _confirmed_agent_terminal(result, request_id):
                raise AgentTaskUnresolvedError(agent_url, request_id, "unconfirmed_terminal")
            return result
        await ws.send_json({"type": "agent_task", "request_id": request_id, **payload})
        logger.debug(
            "WS agent_task dispatch | agent=%s request_id=%s runtime=%s",
            agent_url,
            request_id,
            task.get("runtime"),
        )
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        if terminal is not None and dispatched:
            result = await _cancel_agent_task_confirmed(
                ws, agent_url, request_id, terminal, cancel_ack_timeout
            )
            if result["type"] == "done":
                return result
        elif terminal is None:
            await _cancel_agent_task(ws, request_id)
        raise TimeoutError(f"WS agent {agent_url!r} did not complete agent_task in {timeout}s")
    except asyncio.CancelledError:
        if terminal is not None and dispatched:
            result = await _cancel_agent_task_confirmed(
                ws, agent_url, request_id, terminal, cancel_ack_timeout
            )
            if result.get("error_type") != "CancelledError" or result["type"] != "error":
                return result
        elif terminal is None:
            await _cancel_agent_task(ws, request_id)
        raise
    except Exception:
        # A streaming callback can fail after the edge has emitted evidence.
        # Stop the remote task so it cannot continue into a destructive step.
        if terminal is not None and dispatched:
            await _cancel_agent_task_confirmed(
                ws, agent_url, request_id, terminal, cancel_ack_timeout
            )
        elif terminal is None:
            await _cancel_agent_task(ws, request_id)
        raise
    finally:
        _pending_agent_tasks.pop(request_id, None)
        _task_owners.pop(request_id, None)
        _agent_task_callbacks.pop(request_id, None)
        _agent_task_terminal_results.pop(request_id, None)
        if terminal is not None:
            for future in (fut, terminal):
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()


def _confirmed_agent_terminal(result: Any, request_id: str) -> bool:
    return (
        isinstance(result, dict)
        and result.get("task_id") == request_id
        and result.get("type") in ("done", "error")
        and result.get("cleanup_complete") is True
    )


def _cleanup_proof(result: dict[str, Any], task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "type": result["type"],
        "cleanup_complete": True,
        "error_type": "CancelledError"
        if result.get("error_type") == "CancelledError"
        else "RemoteError",
    }


async def probe_agent_task(
    agent_url: str, task_id: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Read the exact dispatch's cleanup evidence from its current authenticated peer.

    A missing/expired edge record is unknown, never proof that execution stopped.
    This read-only probe does not issue cancellation or start native execution.
    """
    if not isinstance(task_id, str) or not 1 <= len(task_id) <= 64:
        raise ValueError("task_id must be 1..64 characters")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    ws = _connections.get(agent_url)
    if ws is None:
        raise AgentTaskUnresolvedError(agent_url, task_id, "agent_disconnected")
    if len(_agent_task_status_probes) >= _STATUS_PROBE_LIMIT:
        raise AgentTaskUnresolvedError(agent_url, task_id, "status_probe_capacity")
    request_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    _agent_task_status_probes[request_id] = _AgentTaskStatusProbe(agent_url, task_id, ws, future)
    try:
        async with asyncio.timeout(timeout):
            await ws.send_json(
                {
                    "type": "agent_task_status",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            )
            result = await asyncio.shield(future)
        if _connections.get(agent_url) is not ws:
            raise AgentTaskUnresolvedError(agent_url, task_id, "agent_disconnected")
        return result
    except AgentTaskUnresolvedError:
        raise
    except TimeoutError as exc:
        raise AgentTaskUnresolvedError(agent_url, task_id, "status_probe_timeout") from exc
    except Exception as exc:
        raise AgentTaskUnresolvedError(agent_url, task_id, "status_probe_failed") from exc
    finally:
        _agent_task_status_probes.pop(request_id, None)
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            future.exception()


def resolve_agent_task_status(request_id: str, msg: dict[str, Any], source_ws: WebSocket) -> None:
    if not isinstance(request_id, str):
        return
    probe = _agent_task_status_probes.get(request_id)
    if (
        probe is None
        or probe.future.done()
        or source_ws is not probe.owner
        or _connections.get(probe.agent_url) is not source_ws
        or msg.get("type") != "agent_task_status_result"
        or msg.get("request_id") != request_id
        or msg.get("task_id") != probe.task_id
    ):
        return
    result = msg.get("result")
    if _confirmed_agent_terminal(result, probe.task_id):
        probe.future.set_result(_cleanup_proof(result, probe.task_id))
    elif isinstance(result, dict):
        if result.get("status") in ("running", "unknown"):
            probe.future.set_result({"status": result["status"]})
        elif result.get("task_id") == probe.task_id and result.get("type") in ("done", "error"):
            probe.future.set_result({"status": "unknown"})


async def _cancel_agent_task_confirmed(
    ws: WebSocket,
    agent_url: str,
    request_id: str,
    terminal: asyncio.Future[dict[str, Any]],
    timeout: float,
) -> dict[str, Any]:
    async def cancel_and_wait() -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout):
                if terminal.done():
                    return terminal.result()
                if _connections.get(agent_url) is not ws:
                    raise AgentTaskUnresolvedError(agent_url, request_id, "agent_disconnected")
                await ws.send_json({"type": "cancel", "request_id": request_id})
                return await asyncio.shield(terminal)
        except AgentTaskUnresolvedError:
            raise
        except TimeoutError as exc:
            raise AgentTaskUnresolvedError(agent_url, request_id, "cancel_ack_timeout") from exc
        except (Exception, asyncio.CancelledError) as exc:
            raise AgentTaskUnresolvedError(
                agent_url, request_id, "cancel_transport_failed"
            ) from exc

    cleanup = asyncio.create_task(cancel_and_wait())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    return cleanup.result()


async def _cancel_agent_task(ws: WebSocket, request_id: str) -> None:
    try:
        await ws.send_json({"type": "cancel", "request_id": request_id})
    except Exception:
        logger.warning("WS: failed to cancel agent_task request_id=%s", request_id, exc_info=True)


async def _invoke_on_event(
    on_event: Callable[[dict[str, Any]], Any],
    event: dict[str, Any],
) -> None:
    """Call *on_event*, awaiting it if it returned an awaitable (async callable)."""
    result = on_event(event)
    if inspect.isawaitable(result):
        await result


async def resolve_agent_event(
    request_id: str,
    msg: dict[str, Any],
    source_ws: WebSocket | None = None,
) -> None:
    """Called from the WS receive loop when an agent sends an 'agent_event' frame."""
    entry = _agent_task_callbacks.get(request_id)
    if entry is None:
        logger.warning("WS: unexpected agent_event for request_id=%s (no waiting task)", request_id)
        return
    if source_ws is not None and _task_owners.get(request_id) is not source_ws:
        return
    on_event, owner = entry
    event = msg.get("event", {})
    try:
        await _invoke_on_event(on_event, event)
        if msg.get("ack_required") is True:
            event_id = msg.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise RuntimeError("ack-required agent_event is missing event_id")
            ws = source_ws or _connections.get(owner)
            if ws is None:
                raise RuntimeError(
                    f"WS agent {owner!r} disconnected before evidence acknowledgement"
                )
            await ws.send_json(
                {
                    "type": "agent_event_ack",
                    "request_id": request_id,
                    "event_id": event_id,
                    "status": "persisted",
                }
            )
    except Exception as exc:
        logger.exception("WS: on_event callback raised for request_id=%s", request_id)
        fut = _pending_agent_tasks.get(request_id)
        if fut is not None and not fut.done():
            fut.set_exception(exc)


def resolve_agent_result(
    request_id: str,
    msg: dict[str, Any],
    source_ws: WebSocket | None = None,
) -> None:
    """Called from the WS receive loop when an agent sends the terminal 'agent_result' frame."""
    _prune_retained_tasks()
    fut = _pending_agent_tasks.get(request_id)
    result = msg.get("result", {})
    retained = _retained_agent_tasks.get(request_id)
    if (
        retained is not None
        and source_ws is not None
        and retained.owner is source_ws
        and _connections.get(retained.agent_url) is source_ws
        and _confirmed_agent_terminal(result, request_id)
        and retained.terminal is None
    ):
        retained.terminal = _cleanup_proof(result, request_id)
        retained.owner = None
    if source_ws is not None and _task_owners.get(request_id) is not source_ws:
        return
    terminal = _agent_task_terminal_results.get(request_id)
    if (
        terminal is not None
        and not terminal.done()
        and _confirmed_agent_terminal(result, request_id)
    ):
        terminal.set_result(result)
    if fut is None or fut.done():
        logger.warning(
            "WS: unexpected agent_result for request_id=%s (no waiting future)",
            request_id,
        )
        return
    fut.set_result(msg.get("result", {}))


def resolve_portal_prepared(
    agent_url: str,
    msg: dict[str, Any],
    source_ws: WebSocket | None = None,
) -> None:
    """Resolve an edge-produced route only for its originating authenticated socket."""

    request_id = msg.get("request_id")
    owner = _portal_prepare_owners.get(request_id) if isinstance(request_id, str) else None
    if (
        owner is None
        or _connections.get(agent_url) is not owner
        or (source_ws is not None and owner is not source_ws)
    ):
        logger.warning("WS: unexpected portal_prepared response")
        return
    future = _pending_portal_prepares.get(request_id)
    if future is None or future.done():
        return
    try:
        if msg.get("type") == "portal_prepare_error":
            raise RuntimeError("portal owner rejected route preparation")
        route = PortalOwnerRouteV1.model_validate(msg.get("route"))
        identity = _connection_identities.get(agent_url)
        if identity is None or route.node_identity != identity:
            raise ValueError("portal route identity does not match authenticated node")
        expected_tunnel = _connection_tunnels.get(agent_url)
        if expected_tunnel != (route.tunnel_handle, route.tunnel_auth_digest):
            raise ValueError("portal route tunnel does not match authenticated connection")
        future.set_result(route)
    except (ValueError, RuntimeError) as exc:
        future.set_exception(exc)
