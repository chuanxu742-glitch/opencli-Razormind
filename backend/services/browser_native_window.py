"""Authenticated loopback bridge for native TigerVNC account windows."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import string
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from backend import ws_agent_manager
from backend.browser_desktop_protocol import DESKTOP_MAX_PAYLOAD_BYTES
from backend.config import get_settings
from backend.database import AsyncSessionLocal
from backend.services import browser_account_service
from backend.services.browser_desktop_service import (
    DESKTOP_HARD_TTL,
    BrowserDesktopAuthorization,
    authorize_browser_desktop,
)

logger = logging.getLogger(__name__)

_RFB_VERSION = b"RFB 003.008\n"
_RFB_SECURITY_NONE = 1
_RFB_SECURITY_VNC_AUTH = 2
_RFB_SECURITY_OK = b"\x00\x00\x00\x00"
_RFB_SECURITY_FAILED = b"\x00\x00\x00\x01"
_RFB_MAX_NAME_BYTES = 255
_RFB_MAX_UPSTREAM_NAME_BYTES = 4096
_RFB_IO_CHUNK_BYTES = min(64 * 1024, DESKTOP_MAX_PAYLOAD_BYTES)
_HANDSHAKE_TIMEOUT_SECONDS = 10.0
_WRITE_TIMEOUT_SECONDS = 5.0
_STARTUP_TIMEOUT_SECONDS = 20.0
_PROCESS_STOP_TIMEOUT_SECONDS = 3.0
_AUTHORIZATION_INTERVAL_SECONDS = 0.5
_AUTHORIZATION_TIMEOUT_SECONDS = 0.5
_MAX_NATIVE_WINDOWS = 8
_ROUTE_READY_TIMEOUT_SECONDS = 10.0
_ROUTE_RETRY_INTERVAL_SECONDS = 0.5
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_MINIMUM_VIEWER_ENV = (
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
)


class BrowserNativeWindowError(RuntimeError):
    """Safe native-window failure suitable for an API response."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class NativeWindowSupport:
    available: bool
    message: str


@dataclass(frozen=True)
class NativeWindowOpenResult:
    status: str
    session_id: str
    message: str


@dataclass
class _NativeWindow:
    authorization: BrowserDesktopAuthorization
    subject: str
    title: str
    ready: asyncio.Future[None]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | Any | None = None
    server: asyncio.AbstractServer | None = None
    writer: asyncio.StreamWriter | None = None
    transport: ws_agent_manager.BrowserDesktopTransport | None = None
    close_reason: str = ""
    save_on_close: bool = False

    @property
    def session_id(self) -> str:
        return self.authorization.envelope.session_id


class _DesktopByteStream:
    """Read exact byte counts from the framed BDS1 desktop transport."""

    def __init__(self, transport: ws_agent_manager.BrowserDesktopTransport) -> None:
        self.transport = transport
        self.buffer = bytearray()

    async def readexactly(self, size: int) -> bytes:
        if size < 0 or size > _RFB_MAX_UPSTREAM_NAME_BYTES + 24:
            raise BrowserNativeWindowError("浏览器画面握手长度无效")
        while len(self.buffer) < size:
            chunk = await asyncio.wait_for(
                self.transport.receive(), timeout=_HANDSHAKE_TIMEOUT_SECONDS
            )
            if chunk is None:
                raise BrowserNativeWindowError("浏览器画面连接已关闭")
            if len(self.buffer) + len(chunk) > DESKTOP_MAX_PAYLOAD_BYTES:
                raise BrowserNativeWindowError("浏览器画面握手超过长度限制")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def take_buffered(self) -> bytes:
        result = bytes(self.buffer)
        self.buffer.clear()
        return result


def _reverse_byte_bits(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """Return the standard RFB VncAuth DES response for one challenge."""

    if len(challenge) != 16:
        raise ValueError("VNC challenge must be 16 bytes")
    raw_password = password.encode("ascii", "strict")
    if len(raw_password) != 8:
        raise ValueError("VNC password must be exactly 8 ASCII bytes")
    des_key = bytes(_reverse_byte_bits(value) for value in raw_password)
    # Equal K1/K2/K3 is exactly single DES while using cryptography's retained
    # TripleDES primitive; the password exists only in this process.
    encryptor = Cipher(TripleDES(des_key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


def _viewer_environment(password: str) -> dict[str, str]:
    environment = {
        key: value for key in _MINIMUM_VIEWER_ENV if (value := os.environ.get(key))
    }
    environment["VNC_PASSWORD"] = password
    return environment


def _viewer_startupinfo() -> Any:
    if sys.platform != "win32":
        return None
    # The API normally runs hidden; its desktop viewer must open visibly.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1  # Win32 SW_SHOWNORMAL
    return startup


def _bounded_title(label: str) -> str:
    raw = f"OpenCLI - {label.strip() or '账号浏览器'}".encode("utf-8")
    return raw[:_RFB_MAX_NAME_BYTES].decode("utf-8", "ignore")


def _generation(authorization: BrowserDesktopAuthorization) -> tuple[str | int, ...]:
    envelope = authorization.envelope
    return (
        envelope.workspace_id,
        envelope.account_id,
        envelope.session_id,
        envelope.profile_id or "",
        envelope.node_id,
        envelope.node_boot_id,
        envelope.lease_id,
        envelope.epoch,
    )


async def _write(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT_SECONDS)


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=_WRITE_TIMEOUT_SECONDS)
    except Exception:
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.abort()


async def _authenticate_viewer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    password: str,
    *,
    challenge: bytes | None = None,
) -> None:
    await _write(writer, _RFB_VERSION)
    version = await asyncio.wait_for(
        reader.readexactly(len(_RFB_VERSION)), timeout=_HANDSHAKE_TIMEOUT_SECONDS
    )
    if version != _RFB_VERSION:
        raise BrowserNativeWindowError("本机查看器不支持要求的 RFB 3.8 协议")
    await _write(writer, bytes((1, _RFB_SECURITY_VNC_AUTH)))
    selected = await asyncio.wait_for(
        reader.readexactly(1), timeout=_HANDSHAKE_TIMEOUT_SECONDS
    )
    if selected != bytes((_RFB_SECURITY_VNC_AUTH,)):
        raise BrowserNativeWindowError("本机查看器未选择口令认证")
    auth_challenge = challenge or secrets.token_bytes(16)
    await _write(writer, auth_challenge)
    supplied = await asyncio.wait_for(
        reader.readexactly(16), timeout=_HANDSHAKE_TIMEOUT_SECONDS
    )
    if not hmac.compare_digest(supplied, vnc_auth_response(password, auth_challenge)):
        reason = "VNC authentication failed".encode("ascii")
        await _write(writer, _RFB_SECURITY_FAILED + len(reason).to_bytes(4, "big") + reason)
        raise BrowserNativeWindowError("本机查看器口令认证失败")
    await _write(writer, _RFB_SECURITY_OK)


async def _initialize_upstream(
    reader: asyncio.StreamReader,
    stream: _DesktopByteStream,
    transport: ws_agent_manager.BrowserDesktopTransport,
    *,
    title: str,
) -> bytes:
    client_init = await asyncio.wait_for(
        reader.readexactly(1), timeout=_HANDSHAKE_TIMEOUT_SECONDS
    )
    if client_init not in {b"\x00", b"\x01"}:
        raise BrowserNativeWindowError("本机查看器初始化消息无效")
    upstream_version = await stream.readexactly(len(_RFB_VERSION))
    if upstream_version != _RFB_VERSION:
        raise BrowserNativeWindowError("节点浏览器不支持要求的 RFB 3.8 协议")
    await asyncio.wait_for(
        transport.send(_RFB_VERSION), timeout=_WRITE_TIMEOUT_SECONDS
    )
    security_count = (await stream.readexactly(1))[0]
    if security_count == 0 or security_count > 32:
        raise BrowserNativeWindowError("节点浏览器安全类型无效")
    security_types = await stream.readexactly(security_count)
    if _RFB_SECURITY_NONE not in security_types:
        raise BrowserNativeWindowError("节点浏览器未提供内部无认证通道")
    await asyncio.wait_for(
        transport.send(bytes((_RFB_SECURITY_NONE,))), timeout=_WRITE_TIMEOUT_SECONDS
    )
    if await stream.readexactly(4) != _RFB_SECURITY_OK:
        raise BrowserNativeWindowError("节点浏览器拒绝内部连接")
    await asyncio.wait_for(
        transport.send(client_init), timeout=_WRITE_TIMEOUT_SECONDS
    )
    server_init = await stream.readexactly(24)
    upstream_name_size = int.from_bytes(server_init[20:24], "big")
    if upstream_name_size > _RFB_MAX_UPSTREAM_NAME_BYTES:
        raise BrowserNativeWindowError("节点浏览器桌面名称超过长度限制")
    await stream.readexactly(upstream_name_size)
    name = title.encode("utf-8")
    return server_init[:20] + len(name).to_bytes(4, "big") + name


class BrowserNativeWindowManager:
    def __init__(
        self,
        *,
        viewer_executable: str | None = None,
        platform: str | None = None,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
        authorize: Callable[..., Coroutine[Any, Any, BrowserDesktopAuthorization | None]] = (
            authorize_browser_desktop
        ),
        close_session: Callable[..., Coroutine[Any, Any, Any]] = (
            browser_account_service.close_login_session
        ),
        route_opener: Callable[..., Coroutine[Any, Any, ws_agent_manager.BrowserDesktopTransport]] = (
            ws_agent_manager.open_browser_desktop_route
        ),
    ) -> None:
        self._viewer_executable = viewer_executable
        self._platform = platform or sys.platform
        self._session_factory = session_factory
        self._authorize = authorize
        self._close_session = close_session
        self._route_opener = route_opener
        self._windows: dict[str, _NativeWindow] = {}
        self._lock = asyncio.Lock()

    def support(self) -> NativeWindowSupport:
        if self._platform != "win32":
            return NativeWindowSupport(False, "原生浏览器窗口仅支持本机 Windows API 服务")
        configured = (
            self._viewer_executable
            if self._viewer_executable is not None
            else get_settings().browser_native_viewer_executable
        ).strip()
        if not configured:
            return NativeWindowSupport(False, "未配置本机 TigerVNC 查看器")
        path = Path(configured)
        if not path.is_absolute() or path.suffix.lower() != ".exe" or not path.is_file():
            return NativeWindowSupport(False, "配置的本机 TigerVNC 查看器不可用")
        return NativeWindowSupport(True, "可在 Windows 桌面打开独立浏览器窗口")

    def _executable(self) -> str:
        support = self.support()
        if not support.available:
            raise BrowserNativeWindowError(support.message)
        configured = (
            self._viewer_executable
            if self._viewer_executable is not None
            else get_settings().browser_native_viewer_executable
        )
        return str(Path(configured).resolve())

    async def open(
        self,
        authorization: BrowserDesktopAuthorization,
        *,
        subject: str,
        account_label: str,
    ) -> NativeWindowOpenResult:
        executable = self._executable()
        session_id = authorization.envelope.session_id
        async with self._lock:
            existing = self._windows.get(session_id)
            if existing is not None and existing.task is not None and existing.task.done():
                self._windows.pop(session_id, None)
                existing = None
            if existing is not None:
                if _generation(existing.authorization) != _generation(authorization):
                    raise BrowserNativeWindowError(
                        "原生窗口对应的浏览器授权已失效", status_code=409
                    )
                ready = existing.ready
                process = existing.process
                status = "already_open"
            else:
                if len(self._windows) >= _MAX_NATIVE_WINDOWS:
                    raise BrowserNativeWindowError(
                        "原生浏览器窗口数量已达到上限", status_code=409
                    )
                ready = asyncio.get_running_loop().create_future()
                existing = _NativeWindow(
                    authorization=authorization,
                    subject=subject,
                    title=_bounded_title(account_label),
                    ready=ready,
                )
                self._windows[session_id] = existing
                existing.task = asyncio.create_task(self._run(existing, executable))
                process = None
                status = "opened"
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=_STARTUP_TIMEOUT_SECONDS)
        except Exception as exc:
            if status == "opened":
                await self._request_stop(existing, reason="startup_failed", save=False)
            if ready.done() and not ready.cancelled():
                ready.exception()
            if isinstance(exc, BrowserNativeWindowError):
                raise
            raise BrowserNativeWindowError("原生浏览器窗口启动失败，请重试") from exc
        if status == "already_open":
            process = existing.process or process
            if process is not None:
                await asyncio.to_thread(_focus_window_for_pid, int(process.pid))
        return NativeWindowOpenResult(
            status=status,
            session_id=session_id,
            message=(
                "浏览器窗口已打开"
                if status == "opened"
                else "浏览器窗口已在运行，已尝试切换到该窗口"
            ),
        )

    async def _run(self, entry: _NativeWindow, executable: str) -> None:
        client: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            asyncio.get_running_loop().create_future()
        )

        def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if client.done():
                writer.close()
                return
            client.set_result((reader, writer))

        save = False
        relay_tasks: set[asyncio.Task[Any]] = set()
        try:
            entry.server = await asyncio.wait_for(
                asyncio.start_server(accepted, host="127.0.0.1", port=0, limit=131_072),
                timeout=_HANDSHAKE_TIMEOUT_SECONDS,
            )
            socket = entry.server.sockets[0]
            port = int(socket.getsockname()[1])
            entry.transport = await self._open_ready_route(entry)
            password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(8))
            entry.process = await asyncio.create_subprocess_exec(
                executable,
                f"127.0.0.1::{port}",
                "-SecurityTypes=VncAuth",
                "-SendClipboard=0",
                "-AcceptClipboard=0",
                "-ReconnectOnError=0",
                "-RemoteResize=0",
                "-Shared",
                env=_viewer_environment(password),
                startupinfo=_viewer_startupinfo(),
                shell=False,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            process_exit_before_connect = asyncio.create_task(entry.process.wait())
            done, _pending = await asyncio.wait(
                {client, process_exit_before_connect},
                timeout=_STARTUP_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if client not in done:
                if not client.done():
                    client.cancel()
                if process_exit_before_connect not in done:
                    process_exit_before_connect.cancel()
                await asyncio.gather(
                    process_exit_before_connect, return_exceptions=True
                )
                if entry.process.returncode is not None:
                    raise BrowserNativeWindowError("本机 TigerVNC 查看器启动失败")
                raise TimeoutError("native viewer did not connect")
            process_exit_before_connect.cancel()
            await asyncio.gather(process_exit_before_connect, return_exceptions=True)
            reader, entry.writer = client.result()
            entry.server.close()
            await _authenticate_viewer(reader, entry.writer, password)
            stream = _DesktopByteStream(entry.transport)
            server_init = await _initialize_upstream(
                reader,
                stream,
                entry.transport,
                title=entry.title,
            )
            current = await self._fresh_authorization(entry)
            if current is None or _generation(current) != _generation(
                entry.authorization
            ):
                raise BrowserNativeWindowError("浏览器授权在窗口打开前已失效")
            await _write(entry.writer, server_init)
            buffered = stream.take_buffered()
            if buffered:
                await _write(entry.writer, buffered)
            if not entry.ready.done():
                entry.ready.set_result(None)

            async def viewer_input() -> str:
                while True:
                    try:
                        payload = await reader.read(_RFB_IO_CHUNK_BYTES)
                    except ConnectionError:
                        return "viewer_closed"
                    if not payload:
                        return "viewer_closed"
                    await asyncio.wait_for(
                        entry.transport.send(payload), timeout=_WRITE_TIMEOUT_SECONDS
                    )

            async def browser_output() -> str:
                while True:
                    payload = await entry.transport.receive()
                    if payload is None:
                        return "browser_closed"
                    try:
                        await _write(entry.writer, payload)
                    except ConnectionError:
                        return "viewer_closed"

            async def process_exit() -> str:
                await entry.process.wait()
                return "viewer_closed"

            relay_tasks = {
                asyncio.create_task(viewer_input()),
                asyncio.create_task(browser_output()),
                asyncio.create_task(process_exit()),
                asyncio.create_task(self._monitor_authorization(entry)),
                asyncio.create_task(entry.stop.wait()),
            }
            done, pending = await asyncio.wait(
                relay_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            reasons = {
                task.result()
                for task in done
                if not task.cancelled() and task.exception() is None
            }
            save = "viewer_closed" in reasons or entry.save_on_close
            for task in pending:
                task.cancel()
            await asyncio.gather(*relay_tasks, return_exceptions=True)
        except BaseException as exc:
            if not entry.ready.done():
                if isinstance(exc, BrowserNativeWindowError):
                    entry.ready.set_exception(exc)
                else:
                    entry.ready.set_exception(
                        BrowserNativeWindowError("原生浏览器窗口启动失败，请重试")
                    )
            if not isinstance(exc, asyncio.CancelledError):
                logger.info(
                    "Native browser window ended: %s",
                    type(exc).__name__,
                    exc_info=True,
                )
        finally:
            for task in relay_tasks:
                if not task.done():
                    task.cancel()
            if relay_tasks:
                await asyncio.gather(*relay_tasks, return_exceptions=True)
            # A stop deadline must not cancel an in-flight STOP_AND_SAVE or
            # strand the owned process. Each finalization step is bounded.
            finalizer = asyncio.create_task(self._finalize_entry(entry, save=save))
            try:
                await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                await finalizer

    async def _finalize_entry(self, entry: _NativeWindow, *, save: bool) -> None:
        try:
            await self._cleanup_entry(entry)
            if save or entry.save_on_close:
                await self._save_after_viewer_close(entry)
        finally:
            async with self._lock:
                if self._windows.get(entry.session_id) is entry:
                    self._windows.pop(entry.session_id, None)

    async def _open_ready_route(self, entry: _NativeWindow):
        # Login observations can arrive before the node registers its desktop
        # binding. Wait for that same authorized session, never start another.
        async with asyncio.timeout(_ROUTE_READY_TIMEOUT_SECONDS):
            while not entry.stop.is_set():
                current = await self._fresh_authorization(entry)
                if current is None or _generation(current) != _generation(entry.authorization):
                    raise BrowserNativeWindowError("浏览器授权在窗口打开前已失效")
                try:
                    return await self._route_opener(
                        current.endpoint,
                        current.envelope,
                        expires_at=min(current.session_expires_at, datetime.now(UTC) + DESKTOP_HARD_TTL),
                    )
                except RuntimeError as exc:
                    if str(exc) != "desktop route admission failed":
                        raise
                    try:
                        await asyncio.wait_for(entry.stop.wait(), _ROUTE_RETRY_INTERVAL_SECONDS)
                    except TimeoutError:
                        continue
        raise BrowserNativeWindowError("浏览器窗口启动已取消")

    async def _monitor_authorization(self, entry: _NativeWindow) -> str:
        hard_expires_at = min(
            entry.authorization.session_expires_at,
            datetime.now(UTC) + DESKTOP_HARD_TTL,
        )
        while not entry.stop.is_set():
            remaining = (hard_expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                entry.close_reason = "session_expired"
                return "authorization_revoked"
            try:
                await asyncio.wait_for(
                    entry.stop.wait(),
                    timeout=min(_AUTHORIZATION_INTERVAL_SECONDS, remaining),
                )
                return "stopped"
            except TimeoutError:
                pass
            current = await self._fresh_authorization(entry)
            if current is None or _generation(current) != _generation(entry.authorization):
                entry.close_reason = "authorization_revoked"
                return "authorization_revoked"
        return "stopped"

    async def _fresh_authorization(
        self, entry: _NativeWindow
    ) -> BrowserDesktopAuthorization | None:
        try:
            async with asyncio.timeout(_AUTHORIZATION_TIMEOUT_SECONDS):
                async with self._session_factory() as db:
                    return await self._authorize(
                        db,
                        entry.authorization.envelope.workspace_id,
                        entry.authorization.envelope.account_id,
                        entry.session_id,
                        subject=entry.subject,
                    )
        except Exception:
            return None

    async def _save_after_viewer_close(self, entry: _NativeWindow) -> None:
        try:
            async with asyncio.timeout(5.0):
                for attempt in range(3):
                    async with self._session_factory() as db:
                        current = await self._authorize(
                            db,
                            entry.authorization.envelope.workspace_id,
                            entry.authorization.envelope.account_id,
                            entry.session_id,
                            subject=entry.subject,
                        )
                        if current is None or _generation(current) != _generation(
                            entry.authorization
                        ):
                            return
                        try:
                            await self._close_session(
                                db,
                                entry.authorization.envelope.workspace_id,
                                entry.authorization.envelope.account_id,
                                entry.session_id,
                                reason="completed",
                                expected_revision=current.session_revision,
                            )
                            await db.commit()
                            return
                        except browser_account_service.BrowserAccountError as exc:
                            if exc.code != "stale_generation" or attempt == 2:
                                raise
                            await db.rollback()
        except Exception:
            logger.exception("Native browser profile save request failed")

    async def _cleanup_entry(self, entry: _NativeWindow) -> None:
        if entry.server is not None:
            entry.server.close()
        if entry.transport is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(entry.transport.close(reason="native_viewer_closed")),
                    timeout=_WRITE_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        await _close_writer(entry.writer)
        if entry.server is not None:
            try:
                await asyncio.wait_for(
                    entry.server.wait_closed(), timeout=_WRITE_TIMEOUT_SECONDS
                )
            except Exception:
                pass
        process = entry.process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=_PROCESS_STOP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=_PROCESS_STOP_TIMEOUT_SECONDS
                    )
                except Exception:
                    pass

    async def _request_stop(
        self, entry: _NativeWindow, *, reason: str, save: bool
    ) -> None:
        entry.close_reason = reason
        entry.save_on_close = save
        entry.stop.set()
        if entry.task is not None and entry.task is not asyncio.current_task():
            try:
                await asyncio.wait_for(
                    asyncio.shield(entry.task), timeout=_PROCESS_STOP_TIMEOUT_SECONDS * 3
                )
            except Exception:
                entry.task.cancel()
                await asyncio.gather(entry.task, return_exceptions=True)

    async def shutdown(self) -> None:
        async with self._lock:
            entries = list(self._windows.values())
        await asyncio.gather(
            *(
                self._request_stop(entry, reason="api_shutdown", save=True)
                for entry in entries
            ),
            return_exceptions=True,
        )


def _focus_window_for_pid(pid: int) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd: int, _lparam: int) -> bool:
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(visit, 0)
        if found:
            user32.ShowWindow(found[0], 9)
            if not user32.SetForegroundWindow(found[0]):
                user32.FlashWindow(found[0], True)
    except Exception:
        logger.debug("Could not focus native browser viewer", exc_info=True)


native_window_manager = BrowserNativeWindowManager()


__all__ = [
    "BrowserNativeWindowError",
    "BrowserNativeWindowManager",
    "NativeWindowOpenResult",
    "NativeWindowSupport",
    "native_window_manager",
    "vnc_auth_response",
]
