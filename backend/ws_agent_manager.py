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
                                "event": dict}
                                # one RuntimeEvent; 0..N per task
  agent_result  agent→center  {"type": "agent_result", "request_id": uuid,
                                "result": dict}
                                # terminal done/error RuntimeEvent; exactly 1
  cancel        center→agent  {"type": "cancel", "request_id": uuid}
                                # stops collect or agent_task with the same id

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
import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

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

# request_id → Future awaiting agent result (collect/result path)
_pending: dict[str, asyncio.Future] = {}

# request_id → Future awaiting the terminal agent_result (agent_task path)
_pending_agent_tasks: dict[str, asyncio.Future] = {}

# request_id → (on_event callback, owning agent_url) for streaming agent_event dispatch
_agent_task_callbacks: dict[str, tuple[Callable[[dict[str, Any]], Any], str]] = {}

# request_id → Future awaiting a typed portal descriptor from its edge owner.
_pending_portal_prepares: dict[str, asyncio.Future[PortalOwnerRouteV1]] = {}
# request_id → owning edge connection for disconnect cleanup.
_portal_prepare_owners: dict[str, str] = {}


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
        """Await one validated pixel frame; None means closed or timed out."""
        if self._closed and self._frames.empty():
            return None
        try:
            item = (
                await asyncio.wait_for(self._frames.get(), timeout=timeout)
                if timeout is not None
                else await self._frames.get()
            )
        except TimeoutError:
            return None
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
        _portal_transports.pop(self.portal_id, None)
        _portal_ready.pop(self.portal_id, None)
        self._finish()


_portal_transports: dict[str, PortalTransport] = {}
_portal_ready: dict[str, asyncio.Future[None]] = {}


def register_connection(
    agent_url: str,
    ws: WebSocket,
    node_identity: NodeIdentityV1 | None = None,
) -> None:
    """Record a newly-established WS connection and its node identity."""
    _connections[agent_url] = ws
    if node_identity is not None:
        _connection_identities[agent_url] = node_identity
    else:
        _connection_identities.pop(agent_url, None)
    logger.info("WS agent connected: %s (total=%d)", agent_url, len(_connections))


def unregister_connection(agent_url: str) -> None:
    """Remove a WS connection and fail all its pending futures."""
    _connections.pop(agent_url, None)
    _connection_identities.pop(agent_url, None)
    for portal_id, transport in tuple(_portal_transports.items()):
        if transport.agent_url == agent_url:
            transport._finish()
            _portal_transports.pop(portal_id, None)
            _portal_ready.pop(portal_id, None)
    logger.info("WS agent disconnected: %s (remaining=%d)", agent_url, len(_connections))


    dead_request_ids = [
        request_id
        for request_id, (_, owner) in _agent_task_callbacks.items()
        if owner == agent_url
    ]
    for request_id in dead_request_ids:
        fut = _pending_agent_tasks.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result({
                "type": "error",
                "task_id": request_id,
                "message": f"WS agent {agent_url!r} disconnected before task completed",
                "error_type": "AgentDisconnected",
            })
        _agent_task_callbacks.pop(request_id, None)
    for request_id, owner in tuple(_portal_prepare_owners.items()):
        if owner != agent_url:
            continue
        future = _pending_portal_prepares.get(request_id)
        if future is not None and not future.done():
            future.set_exception(RuntimeError("portal owner disconnected before preparation"))
        _portal_prepare_owners.pop(request_id, None)


async def prepare_portal_route(
    agent_url: str,
    session_envelope: SessionEnvelopeV1,
    *,
    session_revision: int,
    timeout: float = 15,
) -> PortalOwnerRouteV1:
    """Ask the authenticated edge owner to resolve its real portal session."""

    if isinstance(session_revision, bool) or not isinstance(session_revision, int) or session_revision < 0:
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
    request_id = str(uuid.uuid4())
    future: asyncio.Future[PortalOwnerRouteV1] = asyncio.get_running_loop().create_future()
    _pending_portal_prepares[request_id] = future
    _portal_prepare_owners[request_id] = agent_url
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
        _portal_transports.pop(portal_id, None)
        _portal_ready.pop(portal_id, None)
        transport._finish()
        raise


async def resolve_portal_ready(agent_url: str, msg: dict[str, Any]) -> None:
    """Resolve an edge portal_open acknowledgement."""
    portal_id = msg.get("portal_id", "")
    transport = _portal_transports.get(portal_id)
    if transport is None or transport.agent_url != agent_url:
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


def _abort_portals_for_agent(agent_url: str) -> None:
    for portal_id, transport in tuple(_portal_transports.items()):
        if transport.agent_url != agent_url:
            continue
        transport._finish()
        _portal_transports.pop(portal_id, None)
        _portal_ready.pop(portal_id, None)

async def resolve_portal_binary(agent_url: str, data: bytes) -> None:
    """Decode, route-check, and enqueue one transient binary portal frame."""
    if len(data) > _PORTAL_MAX_WIRE_BYTES:
        logger.warning("WS: oversized portal frame from %s", agent_url)
        _abort_portals_for_agent(agent_url)
        return
    try:
        frame = decode_portal_wire_frame(data)
    except ValueError:
        logger.warning("WS: invalid portal wire frame from %s", agent_url)
        _abort_portals_for_agent(agent_url)
        return
    transport = _portal_transports.get(frame.transient.binding.session_id)
    if transport is None or transport.agent_url != agent_url:
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



def is_connected(agent_url: str) -> bool:
    return agent_url in _connections


def list_connected() -> list[str]:
    return list(_connections.keys())


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
) -> dict[str, Any]:
    """Send a collect task to a WS agent and await the result dict.

    Raises:
        RuntimeError: agent is not connected.
        TimeoutError: agent did not respond within *timeout* seconds.
    """
    if timeout is None:
        from backend.config import get_settings
        timeout = float(get_settings().agent_ws_timeout)

    ws = _connections.get(agent_url)
    if ws is None:
        raise RuntimeError(f"No active WS connection for agent: {agent_url}")

    request_id = request_id or str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    _pending[request_id] = fut

    try:
        await ws.send_json({
            "type": "collect",
            "request_id": request_id,
            "site": site,
            "command": command,
            "args": args,
            "positional_args": positional_args,
            "format": output_format,
            "mode": mode,
        })
        logger.debug("WS dispatch | agent=%s request_id=%s site=%s cmd=%s",
                     agent_url, request_id, site, command)
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        raise TimeoutError(f"WS agent {agent_url!r} did not respond in {timeout}s")
    except asyncio.CancelledError:
        await ws.send_json({"type": "cancel", "request_id": request_id})
        raise
    finally:
        _pending.pop(request_id, None)


def resolve_response(request_id: str, result: dict[str, Any]) -> None:
    """Called from the WS receive loop when an agent returns a 'result' message."""
    fut = _pending.get(request_id)
    if fut is None or fut.done():
        logger.warning("WS: unexpected result for request_id=%s (no waiting future)", request_id)
        return
    fut.set_result(result)

async def send_agent_task(
    agent_url: str,
    task: dict[str, Any],
    on_event: Callable[[dict[str, Any]], Any],
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Send one server-resolved runtime envelope over the reverse channel."""

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
    _agent_task_callbacks[request_id] = (on_event, agent_url)

    try:
        await ws.send_json({"type": "agent_task", "request_id": request_id, **payload})
        logger.debug(
            "WS agent_task dispatch | agent=%s request_id=%s runtime=%s",
            agent_url,
            request_id,
            payload.get("runtime"),
        )
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        await _cancel_agent_task(ws, request_id)
        raise TimeoutError(f"WS agent {agent_url!r} did not complete agent_task in {timeout}s")
    except asyncio.CancelledError:
        await _cancel_agent_task(ws, request_id)
        raise
    finally:
        _pending_agent_tasks.pop(request_id, None)
        _agent_task_callbacks.pop(request_id, None)


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


async def resolve_agent_event(request_id: str, msg: dict[str, Any]) -> None:
    """Called from the WS receive loop when an agent sends an 'agent_event' frame."""
    entry = _agent_task_callbacks.get(request_id)
    if entry is None:
        logger.warning("WS: unexpected agent_event for request_id=%s (no waiting task)", request_id)
        return
    on_event, _owner = entry
    event = msg.get("event", {})
    try:
        await _invoke_on_event(on_event, event)
    except Exception as exc:
        logger.exception("WS: on_event callback raised for request_id=%s", request_id)
        fut = _pending_agent_tasks.get(request_id)
        if fut is not None and not fut.done():
            fut.set_exception(exc)

def resolve_agent_result(request_id: str, msg: dict[str, Any]) -> None:
    """Called from the WS receive loop when an agent sends the terminal 'agent_result' frame."""
    fut = _pending_agent_tasks.get(request_id)
    if fut is None or fut.done():
        logger.warning(
            "WS: unexpected agent_result for request_id=%s (no waiting future)",
            request_id,
        )
        return
    fut.set_result(msg.get("result", {}))


def resolve_portal_prepared(agent_url: str, msg: dict[str, Any]) -> None:
    """Resolve an edge-produced route only for its originating authenticated node."""

    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or _portal_prepare_owners.get(request_id) != agent_url:
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
        future.set_result(route)
    except (ValueError, RuntimeError) as exc:
        future.set_exception(exc)
