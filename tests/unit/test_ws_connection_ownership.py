"""A reverse-channel peer may answer only work dispatched to its own socket."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect

from backend import ws_agent_manager as manager
from backend.schemas.browser_account import PortalOwnerRouteV1


@pytest.fixture(autouse=True)
def isolated_connections():
    registries = (
        manager._connections,
        manager._connection_identities,
        manager._connection_tunnels,
        manager._pending,
        manager._collect_owners,
        manager._pending_agent_tasks,
        manager._task_owners,
        manager._agent_task_callbacks,
        manager._pending_portal_prepares,
        manager._portal_prepare_owners,
        manager._portal_transports,
        manager._portal_ready,
    )
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()


def portal_route():
    target = {
        "tab_id": "tab",
        "frame_id": "frame",
        "document_id": "doc",
        "origin": "https://fixture.test",
    }
    return PortalOwnerRouteV1.model_validate(
        {
            "binding": {
                "account_ref": {"workspace_id": "workspace", "account_id": "account"},
                "session_id": "portal",
                "epoch": 1,
                "target": target,
                "view_generation": 1,
                "record_session_id": "record",
            },
            "node_identity": {"node_id": "node", "boot_id": "boot"},
            "owner_endpoint": "https://node.test",
            "tunnel_handle": "tunnel",
            "tunnel_auth_digest": "a" * 64,
            "region_focus": {
                "target": target,
                "view_generation": 1,
                "region_kind": "form",
                "approved_regions": [{"x": 0, "y": 0, "width": 8, "height": 8}],
                "focused_field_ref": "password",
            },
            "session_revision": 1,
            "route_expires_at": datetime.now(UTC) + timedelta(minutes=1),
            "max_frame_bytes": 4096,
            "max_input_bytes": 4096,
        }
    )


@pytest.mark.parametrize("operation", ["open", "close"])
async def test_delayed_portal_cleanup_preserves_replacement(operation):
    route = portal_route()
    old, replacement = AsyncMock(), AsyncMock()
    old.scope = replacement.scope = {"scheme": "wss"}
    blocked, resume = asyncio.Event(), asyncio.Event()

    def register(socket):
        manager.register_connection(
            "agent",
            socket,
            route.node_identity,
            tunnel_handle=route.tunnel_handle,
            tunnel_auth_digest=route.tunnel_auth_digest,
        )

    async def old_send(payload):
        if payload["type"] == f"portal_{operation}":
            blocked.set()
            await resume.wait()
        elif payload["type"] == "portal_open":
            await manager.resolve_portal_ready(
                "agent", {"type": "portal_ready", "portal_id": "portal"}, old
            )

    async def replacement_send(payload):
        await manager.resolve_portal_ready(
            "agent", {"type": "portal_ready", "portal_id": "portal"}, replacement
        )

    old.send_json.side_effect = old_send
    replacement.send_json.side_effect = replacement_send
    register(old)
    if operation == "open":
        pending = asyncio.create_task(manager.open_portal_route("agent", route))
    else:
        transport = await manager.open_portal_route("agent", route)
        pending = asyncio.create_task(transport.close())
    await asyncio.wait_for(blocked.wait(), 1)
    register(replacement)
    new_transport = await manager.open_portal_route("agent", route)
    resume.set()
    if operation == "open":
        with pytest.raises(RuntimeError, match="disconnected"):
            await asyncio.wait_for(pending, 1)
    else:
        await asyncio.wait_for(pending, 1)
    assert manager._portal_transports["portal"] is new_transport
    assert manager._portal_ready["portal"].result() is None
    assert not new_transport._closed


async def test_foreign_collection_result_cannot_complete_owned_work():
    owner, foreign = AsyncMock(), AsyncMock()
    sent = asyncio.Event()
    manager.register_connection("agent", owner)

    async def dispatch(payload):
        manager.resolve_response(payload["request_id"], {"identity": "foreign"}, foreign)
        sent.set()

    owner.send_json.side_effect = dispatch
    work = asyncio.create_task(
        manager.dispatch_collect(
            "agent", "site", "read", {}, [], "json", "bridge", request_id="owned"
        )
    )
    await asyncio.wait_for(sent.wait(), 1)
    assert not work.done()
    manager.resolve_response("owned", {"identity": "owner"}, owner)
    assert await work == {"identity": "owner"}
    assert not manager._collect_owners


async def test_foreign_streaming_events_and_terminal_results_are_ignored():
    owner, foreign, event_handler = AsyncMock(), AsyncMock(), AsyncMock()
    sent = asyncio.Event()
    request_ids = []
    manager.register_connection("agent", owner)

    async def dispatch(payload):
        request_ids.append(payload["request_id"])
        await manager.resolve_agent_event(
            payload["request_id"], {"event": {"text": "foreign"}}, foreign
        )
        manager.resolve_agent_result(
            payload["request_id"], {"result": {"identity": "foreign"}}, foreign
        )
        sent.set()

    owner.send_json.side_effect = dispatch
    work = asyncio.create_task(manager.send_agent_task("agent", {"runtime": "pi"}, event_handler))
    await asyncio.wait_for(sent.wait(), 1)
    assert not work.done()
    event_handler.assert_not_awaited()
    await manager.resolve_agent_event(request_ids[0], {"event": {"text": "owner"}}, owner)
    manager.resolve_agent_result(request_ids[0], {"result": {"identity": "owner"}}, owner)
    assert await work == {"identity": "owner"}
    event_handler.assert_awaited_once_with({"text": "owner"})
    assert not manager._task_owners


async def test_replacement_fails_old_work_and_stale_disconnect_cannot_remove_it():
    old, replacement = AsyncMock(), AsyncMock()
    sent = asyncio.Event()
    old.send_json.side_effect = lambda payload: sent.set()
    manager.register_connection("agent", old)
    work = asyncio.create_task(
        manager.dispatch_collect("agent", "site", "read", {}, [], "json", "bridge", timeout=60)
    )
    await asyncio.wait_for(sent.wait(), 1)

    manager.register_connection("agent", replacement)
    with pytest.raises(RuntimeError, match="disconnected"):
        await asyncio.wait_for(work, 1)
    assert not manager.unregister_connection("agent", old)
    assert manager.owns_connection("agent", replacement)
    await asyncio.sleep(0)
    old.close.assert_awaited_once_with(code=1012)


async def test_duplicate_request_does_not_replace_the_original_waiter():
    owner = AsyncMock()
    sent = asyncio.Event()
    owner.send_json.side_effect = lambda payload: sent.set()
    manager.register_connection("agent", owner)
    work = asyncio.create_task(
        manager.dispatch_collect(
            "agent", "site", "read", {}, [], "json", "bridge", request_id="same"
        )
    )
    await asyncio.wait_for(sent.wait(), 1)
    with pytest.raises(ValueError, match="already active"):
        await manager.dispatch_collect(
            "agent", "site", "read", {}, [], "json", "bridge", request_id="same"
        )
    manager.resolve_response("same", {"identity": "original"}, owner)
    assert await work == {"identity": "original"}


async def test_owner_maps_are_cleaned_after_timeouts_and_callback_errors():
    owner = AsyncMock()
    manager.register_connection("agent", owner)

    with pytest.raises(TimeoutError, match="did not respond"):
        await manager.dispatch_collect(
            "agent", "site", "read", {}, [], "json", "bridge", timeout=0.01
        )
    assert not manager._pending
    assert not manager._collect_owners

    async def dispatch(payload):
        if payload["type"] == "agent_task":
            await manager.resolve_agent_event(
                payload["request_id"], {"event": {"text": "boom"}}, owner
            )

    def failing_callback(_event):
        raise RuntimeError("callback failed")

    owner.send_json.side_effect = dispatch
    with pytest.raises(RuntimeError, match="callback failed"):
        await manager.send_agent_task("agent", {"runtime": "pi"}, failing_callback)
    assert not manager._pending_agent_tasks
    assert not manager._task_owners
    assert not manager._agent_task_callbacks

    owner.send_json.side_effect = None
    with pytest.raises(TimeoutError, match="did not complete agent_task"):
        await manager.send_agent_task("agent", {"runtime": "pi"}, lambda _event: None, timeout=0.01)
    assert not manager._pending_agent_tasks
    assert not manager._task_owners
    assert not manager._agent_task_callbacks


async def test_stale_portal_frames_cannot_complete_or_abort_owner_state():
    owner, stale = AsyncMock(), AsyncMock()
    manager.register_connection("agent", owner)
    ready = asyncio.get_running_loop().create_future()
    transport = MagicMock(
        agent_url="agent",
        websocket=owner,
        portal_id="portal",
        route=object(),
    )
    manager._portal_transports["portal"] = transport
    manager._portal_ready["portal"] = ready

    await manager.resolve_portal_ready(
        "agent", {"type": "portal_ready", "portal_id": "portal"}, stale
    )
    await manager.resolve_portal_binary("agent", b"invalid", stale)
    assert not ready.done()
    transport._finish.assert_not_called()

    await manager.resolve_portal_ready(
        "agent", {"type": "portal_ready", "portal_id": "portal"}, owner
    )
    assert ready.result() is None


async def test_stale_portal_prepare_response_cannot_complete_owner_future():
    owner, stale = AsyncMock(), AsyncMock()
    manager.register_connection("agent", owner)
    future = asyncio.get_running_loop().create_future()
    manager._pending_portal_prepares["prepare"] = future
    manager._portal_prepare_owners["prepare"] = owner

    manager.resolve_portal_prepared(
        "agent",
        {"type": "portal_prepare_error", "request_id": "prepare"},
        stale,
    )
    assert not future.done()
    manager.resolve_portal_prepared(
        "agent",
        {"type": "portal_prepare_error", "request_id": "prepare"},
        owner,
    )
    with pytest.raises(RuntimeError, match="rejected"):
        await future


async def test_disconnect_fails_and_cleans_pending_portal_work():
    owner = AsyncMock()
    manager.register_connection("agent", owner)
    prepare = asyncio.get_running_loop().create_future()
    ready = asyncio.get_running_loop().create_future()
    transport = MagicMock(agent_url="agent", websocket=owner, portal_id="portal")
    manager._pending_portal_prepares["prepare"] = prepare
    manager._portal_prepare_owners["prepare"] = owner
    manager._portal_transports["portal"] = transport
    manager._portal_ready["portal"] = ready

    assert manager.unregister_connection("agent", owner)
    with pytest.raises(RuntimeError, match="before preparation"):
        await prepare
    with pytest.raises(RuntimeError, match="before opening"):
        await ready
    transport._finish.assert_called_once_with()
    assert not manager._portal_prepare_owners
    assert not manager._portal_transports
    assert not manager._portal_ready


@pytest.mark.parametrize("entrypoint", ["nodes", "browsers"])
async def test_websocket_entrypoints_reject_foreign_frames(entrypoint, monkeypatch):
    from backend import browser_pool, database
    from backend.api.v1 import browsers, nodes

    def unavailable_database():
        raise RuntimeError("Controlled unavailable metadata store")

    monkeypatch.setattr(database, "AsyncSessionLocal", unavailable_database)
    monkeypatch.setattr(browser_pool, "get_pool", MagicMock(return_value=object()))
    owner, peer = AsyncMock(), AsyncMock()
    manager.register_connection("http://owner", owner)
    collect = asyncio.get_running_loop().create_future()
    terminal = asyncio.get_running_loop().create_future()
    event_handler = AsyncMock()
    manager._pending["collect"] = collect
    manager._collect_owners["collect"] = owner
    manager._pending_agent_tasks["stream"] = terminal
    manager._task_owners["stream"] = owner
    manager._agent_task_callbacks["stream"] = (event_handler, "http://owner")
    registration = {
        "type": "register",
        "agent_url": "http://peer",
        "mode": "bridge",
        "node_type": "shell",
    }
    frames = [
        {"type": "result", "request_id": "collect", "success": True},
        {"type": "agent_event", "request_id": "stream", "event": {"text": "forged"}},
        {"type": "agent_result", "request_id": "stream", "result": {"text": "forged"}},
    ]

    if entrypoint == "nodes":
        peer.receive_json.return_value = registration
        peer.receive.side_effect = [
            *({"type": "websocket.receive", "text": json.dumps(frame)} for frame in frames),
            {"type": "websocket.disconnect"},
        ]
        handler = nodes.node_ws_endpoint
    else:
        peer.receive_json.side_effect = [registration, *frames, WebSocketDisconnect()]
        handler = browsers.agent_ws_endpoint

    await handler(peer)
    assert not collect.done()
    assert not terminal.done()
    event_handler.assert_not_awaited()
    assert manager.owns_connection("http://owner", owner)


async def test_stale_node_login_observation_is_fenced_before_validation(monkeypatch):
    from backend import browser_pool, database
    from backend.api.v1 import nodes
    from backend.services import browser_account_service

    def unavailable_database():
        raise RuntimeError("Controlled unavailable metadata store")

    monkeypatch.setattr(database, "AsyncSessionLocal", unavailable_database)
    monkeypatch.setattr(browser_pool, "get_pool", MagicMock(return_value=object()))
    apply_observation = AsyncMock()
    monkeypatch.setattr(browser_account_service, "apply_login_observation", apply_observation)

    old, replacement = AsyncMock(), AsyncMock()
    old.receive_json.return_value = {
        "type": "register",
        "agent_url": "http://node",
        "mode": "bridge",
        "node_type": "shell",
    }

    async def receive_stale_frame():
        manager.register_connection("http://node", replacement)
        return {
            "type": "websocket.receive",
            "text": json.dumps({"type": "login_observation", "observation": {"forged": True}}),
        }

    old.receive.side_effect = receive_stale_frame
    await nodes.node_ws_endpoint(old)
    await asyncio.sleep(0)

    apply_observation.assert_not_awaited()
    assert manager.owns_connection("http://node", replacement)
    old.close.assert_awaited_once_with(code=1012)
