import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_portal_serializes_initial_and_requested_capture_through_binary_send(monkeypatch):
    from backend import agent_server as agent

    first_capture, release_capture = asyncio.Event(), asyncio.Event()
    first_send, release_send = asyncio.Event(), asyncio.Event()
    second_started = asyncio.Event()
    captured, sent = [], []

    async def capture(*, sequence, **_kwargs):
        captured.append(sequence)
        if sequence == 1:
            first_capture.set()
            await release_capture.wait()
        return str(sequence).encode()

    async def send(frame):
        if frame == b"1":
            first_send.set()
            await release_send.wait()
        sent.append(frame)

    monkeypatch.setattr(agent, "capture_portal_frame", capture)
    monkeypatch.setattr(agent, "encode_portal_wire_frame", lambda frame: frame)
    runtime = agent._PortalRuntime(
        portal_id="portal", route=None, cdp_endpoint="http://local", websocket_url="ws://local"
    )
    ws = SimpleNamespace(send_bytes=send)
    first = asyncio.create_task(agent._send_portal_pixel(ws, runtime))

    async def request_view():
        second_started.set()
        await agent._send_portal_pixel(ws, runtime)

    try:
        await asyncio.wait_for(first_capture.wait(), 2)
        second = asyncio.create_task(request_view())
        await asyncio.wait_for(second_started.wait(), 2)
        assert captured == [1]
        release_capture.set()
        await asyncio.wait_for(first_send.wait(), 2)
        assert captured == [1], "lock must remain held during binary send"
        release_send.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert captured == [1, 2]
        assert sent == [b"1", b"2"]
    finally:
        release_capture.set()
        release_send.set()
        await first
