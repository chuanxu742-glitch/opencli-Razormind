import json
"""A reverse-channel peer may answer only work dispatched to its own socket."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend import ws_agent_manager as manager


@pytest.fixture(autouse=True)
def isolated_connections():
    registries = (
        manager._connections,
        manager._pending,
        manager._collect_owners,
        manager._pending_agent_tasks,
        manager._task_owners,
        manager._agent_task_callbacks,
        manager._agent_task_status_probes,
    )
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()






async def test_disconnect_fails_pending_collect_without_waiting_for_timeout():
    owner = AsyncMock()
    sent = asyncio.Event()
    owner.send_json.side_effect = lambda payload: sent.set()
    manager.register_connection("agent", owner)
    work = asyncio.create_task(
        manager.dispatch_collect("agent", "site", "read", {}, [], "json", "bridge", timeout=60)
    )
    await asyncio.wait_for(sent.wait(), 1)
    assert manager.unregister_connection("agent", owner)
    with pytest.raises(RuntimeError, match="disconnected"):
        await asyncio.wait_for(work, 1)
    assert not manager._collect_owners


async def test_replaced_socket_cannot_unregister_or_answer_replacement_work():
    old, replacement = AsyncMock(), AsyncMock()
    manager.register_connection("agent", old)
    manager.register_connection("agent", replacement)
    assert not manager.unregister_connection("agent", old)
    assert manager.is_connected("agent")

    async def dispatch(payload):
        manager.resolve_response(payload["request_id"], {"identity": "old"}, old)
        manager.resolve_response(payload["request_id"], {"identity": "replacement"}, replacement)

    replacement.send_json.side_effect = dispatch
    result = await manager.dispatch_collect("agent", "site", "read", {}, [], "json", "bridge")
    assert result == {"identity": "replacement"}
    await asyncio.sleep(0)
    old.close.assert_awaited_once_with(code=1012)






async def test_node_receive_loop_routes_status_reply_to_exact_probe(monkeypatch):
    from unittest.mock import Mock

    from starlette.websockets import WebSocketDisconnect

    from backend import database
    from backend.api.v1 import nodes

    def unavailable_database():
        raise RuntimeError("Controlled unavailable metadata store")

    monkeypatch.setattr(database, "AsyncSessionLocal", unavailable_database)
    monkeypatch.setattr(nodes, "_pool_add", Mock())
    messages = asyncio.Queue()
    registered = asyncio.Event()
    peer = AsyncMock()
    await messages.put(
        {
            "type": "register",
            "agent_url": "http://peer",
            "mode": "bridge",
            "node_type": "shell",
        }
    )

    async def receive():
        message = await messages.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def send(frame):
        if frame["type"] == "registered":
            registered.set()
        elif frame["type"] == "agent_task_status":
            await messages.put(
                {
                    "type": "agent_task_status_result",
                    "request_id": frame["request_id"],
                    "task_id": frame["task_id"],
                    "result": {
                        "type": "error",
                        "error_type": "CancelledError",
                        "task_id": frame["task_id"],
                        "cleanup_complete": True,
                    },
                }
            )

    peer.receive_json.return_value = {"type": "register", "agent_url": "http://peer", "mode": "bridge", "node_type": "shell"}
    async def receive_packet():
        message = await receive()
        return {"type": "websocket.receive", "text": json.dumps(message)}
    await messages.get()
    peer.receive.side_effect = receive_packet
    peer.send_json.side_effect = send
    connection = asyncio.create_task(nodes.node_ws_endpoint(peer))
    try:
        await asyncio.wait_for(registered.wait(), 1)
        result = await manager.probe_agent_task("http://peer", "original", timeout=1)
        assert result == {
            "type": "error",
            "error_type": "CancelledError",
            "task_id": "original",
            "cleanup_complete": True,
        }
        assert not manager._agent_task_status_probes
    finally:
        await messages.put(WebSocketDisconnect())
        await asyncio.wait_for(connection, 1)
