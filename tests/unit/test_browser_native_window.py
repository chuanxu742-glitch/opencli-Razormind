from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import backend.services.browser_native_window as native
from backend.schemas.browser_account import SessionEnvelopeV1
from backend.services.browser_desktop_service import BrowserDesktopAuthorization


def _authorization(*, epoch: int = 3) -> BrowserDesktopAuthorization:
    return BrowserDesktopAuthorization(
        endpoint="https://node.test",
        envelope=SessionEnvelopeV1(
            workspace_id="workspace",
            account_id="account",
            session_id="session",
            profile_id="profile",
            node_id="node",
            node_boot_id="boot",
            lease_id="lease",
            epoch=epoch,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            runtime_bundle_id="bundle",
            runtime_bundle_version="1",
            view_generation=0,
            purpose="browser",
            command_id="command",
            profile_state="uncommitted",
        ),
        role="operator",
        session_revision=4,
        session_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


class _Route:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        name = b"upstream desktop"
        server_init = (
            (1280).to_bytes(2, "big")
            + (900).to_bytes(2, "big")
            + bytes(16)
            + len(name).to_bytes(4, "big")
            + name
        )
        self.frames.put_nowait(
            native._RFB_VERSION
            + b"\x01\x01"
            + native._RFB_SECURITY_OK
            + server_init
        )

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def receive(self) -> bytes | None:
        return await self.frames.get()

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True
        self.frames.put_nowait(None)


class _Process:
    def __init__(self, close_viewer: asyncio.Event | None = None) -> None:
        self.pid = 4242
        self.returncode = None
        self.exited = asyncio.Event()
        self.terminated = False
        self.close_viewer = close_viewer

    async def wait(self) -> int:
        await self.exited.wait()
        return int(self.returncode or 0)

    def terminate(self) -> None:
        self.terminated = True
        if self.close_viewer is not None:
            self.close_viewer.set()
        self.returncode = 0
        self.exited.set()

    def kill(self) -> None:
        self.returncode = -9
        self.exited.set()


class _DbContext:
    def __init__(self) -> None:
        self.db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return None


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _viewer_factory(
    monkeypatch,
    *,
    valid_password: bool = True,
    connect: bool = True,
    exit_immediately: bool = False,
):
    launches: list[dict] = []
    viewers: list[dict] = []

    async def launch(*args, **kwargs):
        close_viewer = asyncio.Event()
        process = _Process(close_viewer)
        record = {"args": args, "kwargs": kwargs, "process": process}
        launches.append(record)
        if exit_immediately:
            process.returncode = 1
            process.exited.set()
        if connect:
            endpoint = args[1]
            port = int(endpoint.rsplit("::", 1)[1])

            async def viewer() -> None:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                record["writer"] = writer
                assert await reader.readexactly(12) == native._RFB_VERSION
                writer.write(native._RFB_VERSION)
                await writer.drain()
                assert await reader.readexactly(2) == b"\x01\x02"
                writer.write(b"\x02")
                await writer.drain()
                challenge = await reader.readexactly(16)
                response = (
                    native.vnc_auth_response(kwargs["env"]["VNC_PASSWORD"], challenge)
                    if valid_password
                    else bytes(16)
                )
                writer.write(response)
                await writer.drain()
                status = await reader.readexactly(4)
                record["security_status"] = status
                if status != native._RFB_SECURITY_OK:
                    size = int.from_bytes(await reader.readexactly(4), "big")
                    record["failure_reason"] = await reader.readexactly(size)
                    writer.close()
                    await writer.wait_closed()
                    return
                writer.write(b"\x01")
                await writer.drain()
                try:
                    header = await reader.readexactly(24)
                except asyncio.IncompleteReadError:
                    record["server_init_received"] = False
                    return
                record["server_init_received"] = True
                name_size = int.from_bytes(header[20:24], "big")
                record["desktop_name"] = await reader.readexactly(name_size)
                record["ready"].set()
                await record["close"].wait()
                writer.close()
                await writer.wait_closed()

            record["ready"] = asyncio.Event()
            record["close"] = close_viewer
            viewers.append(record)
            record["viewer_task"] = asyncio.create_task(viewer())
        return process

    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(native, "_focus_window_for_pid", lambda _pid: None)
    return launches, viewers


def _manager(tmp_path, monkeypatch, *, current=None, close_session=None, route=None):
    executable = tmp_path / "vncviewer.exe"
    executable.write_bytes(b"MZ")
    authorization = current or _authorization()
    current_box = {"value": authorization}
    route = route or _Route()

    async def authorize(*_args, **_kwargs):
        return current_box["value"]

    async def open_route(*_args, **_kwargs):
        return route

    manager = native.BrowserNativeWindowManager(
        viewer_executable=str(executable),
        platform="win32",
        session_factory=_DbContext,
        authorize=authorize,
        close_session=close_session or AsyncMock(),
        route_opener=open_route,
    )
    return manager, authorization, current_box, route


def test_vnc_auth_matches_known_des_vector() -> None:
    assert native.vnc_auth_response("password", bytes(range(16))).hex() == (
        "b866924125c8eebb9debc1db61c538e2"
    )


def test_native_support_is_explicit_windows_executable(tmp_path) -> None:
    assert not native.BrowserNativeWindowManager(
        viewer_executable="", platform="win32"
    ).support().available
    viewer = tmp_path / "viewer.exe"
    viewer.write_bytes(b"MZ")
    assert not native.BrowserNativeWindowManager(
        viewer_executable=str(viewer), platform="linux"
    ).support().available
    assert native.BrowserNativeWindowManager(
        viewer_executable=str(viewer), platform="win32"
    ).support().available


@pytest.mark.asyncio
async def test_real_rfb_handshake_rewrites_name_and_reuses_one_window(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "must-not-reach-viewer")
    monkeypatch.setenv("AGENT_NODE_CREDENTIAL", "must-not-reach-viewer")
    launches, viewers = _viewer_factory(monkeypatch)
    manager, authorization, _current, route = _manager(tmp_path, monkeypatch)

    first, second = await asyncio.gather(
        manager.open(authorization, subject="operator", account_label="测试账号"),
        manager.open(authorization, subject="operator", account_label="测试账号"),
    )

    assert {first.status, second.status} == {"opened", "already_open"}
    assert len(launches) == 1
    assert viewers[0]["security_status"] == native._RFB_SECURITY_OK
    assert viewers[0]["desktop_name"] == "OpenCLI - 测试账号".encode()
    assert route.sent[:3] == [native._RFB_VERSION, b"\x01", b"\x01"]
    argv = launches[0]["args"]
    assert argv[1].startswith("127.0.0.1::")
    assert "-SecurityTypes=VncAuth" in argv
    assert "-SendClipboard=0" in argv and "-AcceptClipboard=0" in argv
    assert "-ReconnectOnError=0" in argv and "-RemoteResize=0" in argv
    assert launches[0]["kwargs"]["shell"] is False
    assert "API_AUTH_TOKEN" not in launches[0]["kwargs"]["env"]
    assert "AGENT_NODE_CREDENTIAL" not in launches[0]["kwargs"]["env"]
    password = launches[0]["kwargs"]["env"]["VNC_PASSWORD"]
    assert len(password) == 8
    assert password not in repr(argv)

    await manager._request_stop(
        manager._windows[authorization.envelope.session_id],
        reason="test",
        save=False,
    )
    await asyncio.gather(
        *(record["viewer_task"] for record in viewers), return_exceptions=True
    )


@pytest.mark.asyncio
async def test_wrong_vnc_response_gets_no_server_init(tmp_path, monkeypatch) -> None:
    launches, viewers = _viewer_factory(monkeypatch, valid_password=False)
    manager, authorization, _current, route = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(native, "_STARTUP_TIMEOUT_SECONDS", 1.0)

    with pytest.raises(native.BrowserNativeWindowError, match="口令认证失败"):
        await manager.open(authorization, subject="operator", account_label="账号")

    await _wait_until(lambda: not manager._windows)
    assert viewers[0]["security_status"] == native._RFB_SECURITY_FAILED
    assert "desktop_name" not in viewers[0]
    assert launches[0]["process"].terminated
    assert route.closed


@pytest.mark.asyncio
async def test_revoked_during_startup_gets_no_server_init(tmp_path, monkeypatch) -> None:
    launches, viewers = _viewer_factory(monkeypatch)
    manager, authorization, current, route = _manager(tmp_path, monkeypatch)
    async def revoke_while_opening(*_args, **_kwargs):
        current["value"] = None
        return route
    manager._route_opener = revoke_while_opening
    monkeypatch.setattr(native, "_STARTUP_TIMEOUT_SECONDS", 1.0)

    with pytest.raises(native.BrowserNativeWindowError, match="打开前已失效"):
        await manager.open(authorization, subject="operator", account_label="账号")

    await _wait_until(lambda: not manager._windows)
    assert viewers[0]["security_status"] == native._RFB_SECURITY_OK
    assert viewers[0]["server_init_received"] is False
    assert launches[0]["process"].terminated
    assert route.closed


@pytest.mark.asyncio
async def test_authorization_revocation_closes_without_saving(tmp_path, monkeypatch) -> None:
    launches, viewers = _viewer_factory(monkeypatch)
    close_session = AsyncMock()
    manager, authorization, current, route = _manager(
        tmp_path, monkeypatch, close_session=close_session
    )
    await manager.open(authorization, subject="operator", account_label="账号")

    current["value"] = replace(
        authorization,
        envelope=authorization.envelope.model_copy(update={"epoch": 99}),
    )
    await _wait_until(lambda: not manager._windows)

    assert launches[0]["process"].terminated
    assert route.closed
    close_session.assert_not_awaited()
    await asyncio.gather(viewers[0]["viewer_task"], return_exceptions=True)


@pytest.mark.asyncio
async def test_viewer_exit_closes_bridge_then_requests_fenced_save(
    tmp_path, monkeypatch
) -> None:
    launches, viewers = _viewer_factory(monkeypatch)
    route = _Route()
    ordering: list[str] = []
    original_close = route.close

    async def close_route(*, reason="closed"):
        ordering.append("bridge_closed")
        await original_close(reason=reason)

    route.close = close_route

    async def close_session(_db, _workspace, _account, _session, **kwargs):
        ordering.append("save_requested")
        assert kwargs == {"reason": "completed", "expected_revision": 4}

    manager, authorization, _current, _route_value = _manager(
        tmp_path, monkeypatch, close_session=close_session, route=route
    )
    await manager.open(authorization, subject="operator", account_label="账号")
    viewers[0]["close"].set()

    await _wait_until(lambda: ordering == ["bridge_closed", "save_requested"])
    assert not manager._windows
    assert launches[0]["process"].returncode == 0


@pytest.mark.asyncio
async def test_waits_for_same_session_desktop_binding(tmp_path, monkeypatch):
    launches, _viewers = _viewer_factory(monkeypatch)
    manager, authorization, _current, route = _manager(tmp_path, monkeypatch)
    attempts = 0

    async def opening_route(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("desktop route admission failed")
        return route

    manager._route_opener = opening_route
    monkeypatch.setattr(native, "_ROUTE_RETRY_INTERVAL_SECONDS", 0.01)
    result = await manager.open(authorization, subject="operator", account_label="账号")
    assert result.session_id == authorization.envelope.session_id
    assert attempts == 3 and len(launches) == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_viewer_connection_reset_still_requests_save(tmp_path, monkeypatch):
    _launches, _viewers = _viewer_factory(monkeypatch)
    save = AsyncMock()
    manager, authorization, _current, route = _manager(tmp_path, monkeypatch, close_session=save)
    await manager.open(authorization, subject="operator", account_label="账号")

    async def reset_connection(*_args):
        raise ConnectionResetError("viewer exited")

    monkeypatch.setattr(native, "_write", reset_connection)
    route.frames.put_nowait(b"frame")
    await _wait_until(lambda: not manager._windows)
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_deadline_does_not_cancel_profile_save(tmp_path, monkeypatch):
    launches, _viewers = _viewer_factory(monkeypatch)
    route = _Route()
    original_close = route.close
    saved = asyncio.Event()

    async def delayed_close(*, reason="closed"):
        await asyncio.sleep(0.06)
        await original_close(reason=reason)

    async def delayed_save(*_args, **_kwargs):
        await asyncio.sleep(0.06)
        saved.set()

    route.close = delayed_close
    manager, authorization, _current, _route = _manager(
        tmp_path, monkeypatch, route=route, close_session=delayed_save
    )
    monkeypatch.setattr(native, "_PROCESS_STOP_TIMEOUT_SECONDS", 0.03)
    await manager.open(authorization, subject="operator", account_label="账号")
    await manager.shutdown()
    assert saved.is_set()
    assert not manager._windows
    assert launches[0]["process"].terminated


@pytest.mark.asyncio
async def test_save_retries_one_observer_revision_race(tmp_path, monkeypatch) -> None:
    _launches, viewers = _viewer_factory(monkeypatch)
    manager, authorization, current, _route = _manager(tmp_path, monkeypatch)
    expected_revisions: list[int] = []

    async def close_session(_db, _workspace, _account, _session, **kwargs):
        expected_revisions.append(kwargs["expected_revision"])
        if len(expected_revisions) == 1:
            current["value"] = replace(authorization, session_revision=5)
            raise native.browser_account_service.BrowserAccountError(
                "stale_generation", "observer changed the session revision"
            )

    manager._close_session = close_session
    await manager.open(authorization, subject="operator", account_label="账号")
    viewers[0]["close"].set()

    await _wait_until(lambda: expected_revisions == [4, 5])
    assert not manager._windows


@pytest.mark.asyncio
async def test_startup_timeout_releases_listener_route_and_owned_process(
    tmp_path, monkeypatch
) -> None:
    launches, _viewers = _viewer_factory(monkeypatch, connect=False)
    manager, authorization, _current, route = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(native, "_STARTUP_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(native.BrowserNativeWindowError, match="启动失败"):
        await manager.open(authorization, subject="operator", account_label="账号")

    await _wait_until(lambda: not manager._windows)
    assert launches[0]["process"].terminated
    assert route.closed


@pytest.mark.asyncio
async def test_viewer_process_exit_fails_startup_without_waiting_for_timeout(
    tmp_path, monkeypatch
) -> None:
    launches, _viewers = _viewer_factory(
        monkeypatch, connect=False, exit_immediately=True
    )
    manager, authorization, _current, route = _manager(tmp_path, monkeypatch)

    with pytest.raises(native.BrowserNativeWindowError, match="TigerVNC"):
        await asyncio.wait_for(
            manager.open(authorization, subject="operator", account_label="账号"),
            timeout=0.5,
        )

    assert launches[0]["process"].returncode == 1
    assert route.closed


@pytest.mark.asyncio
async def test_stale_generation_cannot_reuse_existing_window(tmp_path, monkeypatch) -> None:
    _launches, _viewers = _viewer_factory(monkeypatch)
    manager, authorization, _current, _route = _manager(tmp_path, monkeypatch)
    await manager.open(authorization, subject="operator", account_label="账号")
    stale = replace(
        authorization,
        envelope=authorization.envelope.model_copy(update={"epoch": 7}),
    )

    with pytest.raises(native.BrowserNativeWindowError, match="授权已失效"):
        await manager.open(stale, subject="operator", account_label="账号")

    await manager.shutdown()
