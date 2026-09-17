from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.agent_server as agent_server
import backend.ws_agent_manager as manager
from backend.browser_desktop_protocol import (
    DESKTOP_MAGIC,
    DESKTOP_MAX_BUFFERED_FRAMES,
    DESKTOP_MAX_PAYLOAD_BYTES,
    BrowserDesktopRouteV1,
    decode_desktop_frame,
    encode_desktop_frame,
)
from backend.schemas.browser_account import NodeIdentityV1, SessionEnvelopeV1
from backend.services.browser_desktop_service import (
    BrowserDesktopAuthorization,
    BrowserDesktopGrantStore,
)


def _envelope() -> SessionEnvelopeV1:
    return SessionEnvelopeV1(
        workspace_id="workspace",
        account_id="account",
        session_id="session",
        profile_id="profile",
        node_id="node",
        node_boot_id="boot",
        lease_id="lease",
        epoch=3,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        runtime_bundle_id="bundle",
        runtime_bundle_version="1",
        view_generation=0,
        purpose="browser",
        command_id="command",
        profile_state="uncommitted",
    )


def _route() -> BrowserDesktopRouteV1:
    return BrowserDesktopRouteV1(
        route_id="f09df5a9-2138-444f-8022-208f30b08a48",
        workspace_id="workspace",
        account_id="account",
        session_id="session",
        profile_id="profile",
        node_id="node",
        boot_id="boot",
        lease_id="lease",
        command_id="command",
        epoch=3,
        tunnel_handle="handle",
        tunnel_auth_digest="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def test_desktop_wire_frame_round_trip_and_hard_limits() -> None:
    route_id = "f09df5a9-2138-444f-8022-208f30b08a48"
    payload = b"RFB 003.008\n"
    wire = encode_desktop_frame(route_id, 1, 7, payload)
    assert wire.startswith(DESKTOP_MAGIC)
    assert decode_desktop_frame(wire) == (route_id, 1, 7, payload)
    with pytest.raises(ValueError, match="limit"):
        encode_desktop_frame(route_id, 1, 1, b"x" * (DESKTOP_MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="length"):
        decode_desktop_frame(wire + b"padding")
    with pytest.raises(ValueError, match="magic"):
        decode_desktop_frame(b"Q2P1" + wire[4:])


def test_desktop_grants_store_only_digest_and_bind_generation() -> None:
    store = BrowserDesktopGrantStore()
    now = datetime.now(UTC)
    authorization = BrowserDesktopAuthorization(
        endpoint="https://node.test",
        envelope=_envelope(),
        role="operator",
        session_revision=8,
        session_expires_at=now + timedelta(minutes=30),
    )
    token, grant = store.issue(authorization, subject="subject", now=now)
    assert token not in repr(store._grants)
    assert grant.digest == hashlib.sha256(token.encode()).hexdigest()
    assert store.resolve(token, now=now + timedelta(minutes=9)) == grant
    assert store.resolve(token, now=now + timedelta(minutes=11)) is None


@pytest.fixture(autouse=True)
def clear_desktop_manager_state():
    manager._connections.clear()
    manager._connection_identities.clear()
    manager._connection_tunnels.clear()
    manager._desktop_transports.clear()
    manager._desktop_sessions.clear()
    manager._desktop_ready.clear()
    agent_server._ACTIVE_BROWSER_DESKTOPS.clear()
    agent_server._STOPPING_BROWSER_DESKTOPS.clear()
    yield
    manager._connections.clear()
    manager._connection_identities.clear()
    manager._connection_tunnels.clear()
    manager._desktop_transports.clear()
    manager._desktop_sessions.clear()
    manager._desktop_ready.clear()
    agent_server._ACTIVE_BROWSER_DESKTOPS.clear()
    agent_server._STOPPING_BROWSER_DESKTOPS.clear()


@pytest.mark.asyncio
async def test_center_route_is_bound_to_current_socket_and_relays_binary() -> None:
    websocket = MagicMock()
    websocket.scope = {"scheme": "wss"}
    websocket.send_bytes = AsyncMock()

    async def acknowledge(message):
        route = message["route"]
        manager.resolve_browser_desktop_ready(
            "https://node.test",
            {"type": "browser_desktop_ready", "route_id": route["route_id"]},
            source_ws=websocket,
        )

    websocket.send_json = AsyncMock(side_effect=acknowledge)
    manager.register_connection(
        "https://node.test",
        websocket,
        NodeIdentityV1(node_id="node", boot_id="boot"),
        tunnel_handle="handle",
        tunnel_auth_digest="a" * 64,
    )
    transport = await manager.open_browser_desktop_route(
        "https://node.test",
        _envelope(),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    foreign = MagicMock()
    await manager.resolve_browser_desktop_binary(
        "https://node.test",
        encode_desktop_frame(transport.route.route_id, 1, 1, b"ignored"),
        source_ws=foreign,
    )
    assert transport._frames.empty()

    await manager.resolve_browser_desktop_binary(
        "https://node.test",
        encode_desktop_frame(transport.route.route_id, 1, 1, b"pixels"),
        source_ws=websocket,
    )
    assert await transport.receive() == b"pixels"
    await transport.send(b"keys")
    _, direction, sequence, payload = decode_desktop_frame(
        websocket.send_bytes.await_args.args[0]
    )
    assert (direction, sequence, payload) == (0, 1, b"keys")
    await transport.close(reason="test")
    assert not manager._desktop_transports and not manager._desktop_sessions


@pytest.mark.asyncio
async def test_center_overflow_revokes_route_and_allows_reopen() -> None:
    websocket = MagicMock()
    websocket.send_json = AsyncMock()
    transport = manager.BrowserDesktopTransport(
        agent_url="https://node.test", websocket=websocket, route=_route()
    )
    manager._desktop_transports[transport.route.route_id] = transport
    manager._desktop_sessions[transport.route.session_id] = transport.route.route_id
    for sequence in range(1, DESKTOP_MAX_BUFFERED_FRAMES + 1):
        transport._enqueue(sequence, b"x")
    with pytest.raises(RuntimeError, match="buffer"):
        transport._enqueue(DESKTOP_MAX_BUFFERED_FRAMES + 1, b"x")
    await transport.close(reason="overflow")
    assert not manager._desktop_transports and not manager._desktop_sessions


@pytest.mark.asyncio
async def test_malformed_frame_from_current_agent_aborts_its_desktop_route() -> None:
    websocket = MagicMock()
    websocket.send_json = AsyncMock()
    manager._connections["https://node.test"] = websocket
    transport = manager.BrowserDesktopTransport(
        agent_url="https://node.test", websocket=websocket, route=_route()
    )
    manager._desktop_transports[transport.route.route_id] = transport
    manager._desktop_sessions[transport.route.session_id] = transport.route.route_id

    await manager.resolve_browser_desktop_binary(
        "https://node.test", DESKTOP_MAGIC + b"truncated", source_ws=websocket
    )

    assert not manager._desktop_transports
    assert not manager._desktop_sessions
    assert websocket.send_json.await_args.args[0]["type"] == "browser_desktop_close"


class _Reader:
    def __init__(self) -> None:
        self.values = [b"RFB 003.008\n", b""]

    async def read(self, _size):
        return self.values.pop(0)


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, payload):
        self.writes.append(payload)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted_purpose", ["login", "browser"])
async def test_agent_resolves_only_binding_vnc_and_relays_rfb(
    monkeypatch, admitted_purpose: str
) -> None:
    route = _route()
    identity = NodeIdentityV1(node_id="node", boot_id="boot")
    admitted_session = _envelope().model_copy(
        update={"lease_id": "lease", "purpose": admitted_purpose}
    )
    admission = SimpleNamespace(
        claim=SimpleNamespace(
            workspace_id="workspace",
            account_id="account",
            session_id="session",
            node_id="node",
            boot_id="boot",
            epoch=3,
        ),
        session=admitted_session,
        node_identity=identity,
    )
    binding = SimpleNamespace(node_id="node", boot_id="boot", epoch=3, vnc_port=31999)
    running = SimpleNamespace(
        binding=binding,
        profile_id="profile",
        lease=SimpleNamespace(is_valid=lambda: True),
    )
    lease_book = SimpleNamespace(
        current=lambda _id: admission,
        renew=lambda **kwargs: SimpleNamespace(
            claim=admission.claim,
            session=kwargs["session"],
            node_identity=identity,
        ),
    )
    monkeypatch.setattr(agent_server, "runtime_lease_book", lambda: lease_book)
    monkeypatch.setattr(
        agent_server,
        "account_runtime_allocator",
        lambda: SimpleNamespace(get=lambda _id: running),
    )
    monkeypatch.setattr(
        agent_server,
        "session_runtime_registry",
        lambda: SimpleNamespace(resolve=lambda **_kwargs: binding),
    )
    reader = _Reader()
    writer = _Writer()
    opened: list[tuple[str, int]] = []

    async def open_connection(host, port):
        opened.append((host, port))
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    websocket = SimpleNamespace(send=AsyncMock())
    await agent_server._handle_ws_browser_desktop(
        websocket,
        {"route": route.model_dump(mode="json")},
        identity,
        tunnel_handle="handle",
        tunnel_auth_digest="a" * 64,
    )

    assert opened == [("127.0.0.1", 31999)]
    sent = websocket.send.await_args_list
    assert any(
        isinstance(call.args[0], str) and "browser_desktop_ready" in call.args[0]
        for call in sent
    )
    assert any(
        isinstance(call.args[0], str) and "browser_desktop_closed" in call.args[0]
        for call in sent
    )
    binary = next(call.args[0] for call in sent if isinstance(call.args[0], bytes))
    assert decode_desktop_frame(binary)[1:] == (1, 1, b"RFB 003.008\n")
    assert writer.closed


@pytest.mark.asyncio
async def test_center_end_notice_finishes_ready_route_and_stale_socket_cannot_end_it():
    websocket = MagicMock()
    transport = manager.BrowserDesktopTransport(
        agent_url="https://node.test", websocket=websocket, route=_route()
    )
    manager._desktop_transports[transport.route.route_id] = transport
    manager._desktop_sessions[transport.route.session_id] = transport.route.route_id
    ready = asyncio.get_running_loop().create_future()
    ready.set_result(None)
    manager._desktop_ready[transport.route.route_id] = ready

    manager.resolve_browser_desktop_ready(
        "https://node.test",
        {"type": "browser_desktop_closed", "route_id": transport.route.route_id},
        source_ws=MagicMock(),
    )
    assert manager._desktop_transports

    manager.resolve_browser_desktop_ready(
        "https://node.test",
        {"type": "browser_desktop_closed", "route_id": transport.route.route_id},
        source_ws=websocket,
    )
    assert not manager._desktop_transports
    assert not manager._desktop_sessions
    assert await transport.receive() is None


@pytest.mark.asyncio
async def test_agent_close_is_bounded_when_drain_and_wait_closed_never_finish(
    monkeypatch,
):
    route = _route()
    never = asyncio.Event()
    wrote = asyncio.Event()

    class Transport:
        aborted = False

        def abort(self):
            self.aborted = True

    class BlockingWriter(_Writer):
        def __init__(self):
            super().__init__()
            self.transport = Transport()

        async def drain(self):
            await never.wait()

        def write(self, payload):
            super().write(payload)
            wrote.set()

        async def wait_closed(self):
            await never.wait()

    writer = BlockingWriter()
    runtime = agent_server._BrowserDesktopRuntime(
        route, _Reader(), writer, owner_ws=object()
    )
    agent_server._ACTIVE_BROWSER_DESKTOPS[route.route_id] = runtime
    monkeypatch.setattr(agent_server, "_DESKTOP_IO_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(agent_server, "_desktop_route_is_current", lambda _runtime: True)
    runtime.writer_task = asyncio.create_task(agent_server._desktop_write_loop(runtime))
    runtime.input_queue.put_nowait(b"complete-rfb-frame")
    await asyncio.wait_for(wrote.wait(), timeout=0.1)

    await asyncio.wait_for(
        agent_server._close_browser_desktop(
            route.route_id, expected_runtime=runtime
        ),
        timeout=0.2,
    )

    assert writer.writes == [b"complete-rfb-frame"]
    assert writer.transport.aborted
    assert route.route_id not in agent_server._ACTIVE_BROWSER_DESKTOPS


@pytest.mark.asyncio
async def test_old_owner_socket_cannot_close_new_agent_route(monkeypatch):
    route = _route()
    writer = _Writer()
    old_ws = object()
    new_ws = object()
    runtime = agent_server._BrowserDesktopRuntime(route, _Reader(), writer, new_ws)
    agent_server._ACTIVE_BROWSER_DESKTOPS[route.route_id] = runtime
    monkeypatch.setattr(agent_server, "_desktop_route_is_current", lambda _runtime: True)

    await agent_server._close_browser_desktop(route.route_id, owner_ws=old_ws)
    assert agent_server._ACTIVE_BROWSER_DESKTOPS[route.route_id] is runtime
    await agent_server._handle_ws_browser_desktop_binary(
        old_ws,
        encode_desktop_frame(route.route_id, 0, 1, b"stale"),
    )
    assert agent_server._ACTIVE_BROWSER_DESKTOPS[route.route_id] is runtime

    await agent_server._close_browser_desktop(route.route_id, owner_ws=new_ws)
    assert route.route_id not in agent_server._ACTIVE_BROWSER_DESKTOPS


@pytest.mark.asyncio
async def test_stop_fence_drops_queued_input_before_fresh_observation():
    route = _route()
    writer = _Writer()
    runtime = agent_server._BrowserDesktopRuntime(route, _Reader(), writer, object())
    runtime.input_queue.put_nowait(b"queued-after-close")
    agent_server._ACTIVE_BROWSER_DESKTOPS[route.route_id] = runtime
    session = SimpleNamespace(session_id=route.session_id)
    claim = SimpleNamespace(
        node_id=route.node_id,
        boot_id=route.boot_id,
        epoch=route.epoch,
    )

    generation = await agent_server._block_browser_desktop_for_stop(session, claim)
    try:
        assert generation in agent_server._STOPPING_BROWSER_DESKTOPS
        assert route.route_id not in agent_server._ACTIVE_BROWSER_DESKTOPS
        assert writer.writes == []
        assert await runtime.input_queue.get() is None
        assert not agent_server._desktop_route_is_current(runtime)
    finally:
        agent_server._STOPPING_BROWSER_DESKTOPS.discard(generation)
