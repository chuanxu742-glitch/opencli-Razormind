"""Account runtime process isolation and shutdown primitives."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.browser_account_errors import BrowserRuntimeError


@dataclass(frozen=True)
class StackIsolation:
    """Explicit per-session process and filesystem boundaries."""

    display: str
    cdp_port: int
    bbx_port: int
    daemon_port: int
    home_dir: Path
    cache_dir: Path
    profile_dir: Path

    def validate(self) -> None:
        if not self.display.startswith(":") or not self.display[1:].isdigit():
            raise BrowserRuntimeError(
                "runtime_isolation_invalid", "DISPLAY must be a numeric display"
            )
        for name, value in (
            ("cdp_port", self.cdp_port),
            ("bbx_port", self.bbx_port),
            ("daemon_port", self.daemon_port),
        ):
            if not 1 <= value <= 65535:
                raise BrowserRuntimeError(
                    "runtime_isolation_invalid", f"{name} is outside the port range"
                )
        for path in (self.home_dir, self.cache_dir, self.profile_dir):
            if not path.is_absolute() or path == Path("/"):
                raise BrowserRuntimeError(
                    "runtime_isolation_invalid", "runtime paths must be absolute"
                )
        resolved_paths = [
            path.resolve() for path in (self.home_dir, self.cache_dir, self.profile_dir)
        ]
        if len(set(resolved_paths)) != len(resolved_paths):
            raise BrowserRuntimeError("runtime_isolation_invalid", "runtime paths must be distinct")
        if self.cache_dir == self.profile_dir or self.profile_dir in self.cache_dir.parents:
            raise BrowserRuntimeError(
                "runtime_isolation_invalid", "cache and profile paths overlap"
            )

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        self.validate()
        env = dict(base or os.environ)
        env.pop("OPENCLI_DAEMON_PORT", None)
        env.update(
            {
                "DISPLAY": self.display,
                "PROFILE_DIR": str(self.profile_dir),
                "HOME": str(self.home_dir),
                "XDG_CONFIG_HOME": str(self.home_dir / ".config"),
                "CLOAKBROWSER_CACHE_DIR": str(self.cache_dir),
                "OPENCLI_CDP_ENDPOINT": f"http://127.0.0.1:{self.cdp_port}",
                "OPENCLI_DAEMON_HOST": "127.0.0.1",
                "BBX_TCP_HOST": "127.0.0.1",
                "BBX_TCP_BIND_HOST": "127.0.0.1",
                "BBX_TCP_PORT": str(self.bbx_port),
                "OPENCLI_BROWSER_PROFILE_KIND": "authenticated",
            }
        )
        return env


def validate_parallel_isolation(stacks: Iterable[StackIsolation]) -> None:
    """Reject any active sessions that would share an isolation boundary."""

    seen: dict[str, set[str]] = {
        key: set() for key in ("display", "cdp", "bbx", "daemon", "home", "cache", "profile")
    }
    for stack in stacks:
        stack.validate()
        values = {
            "display": stack.display,
            "cdp": str(stack.cdp_port),
            "bbx": str(stack.bbx_port),
            "daemon": str(stack.daemon_port),
            "home": str(stack.home_dir.resolve()),
            "cache": str(stack.cache_dir.resolve()),
            "profile": str(stack.profile_dir.resolve()),
        }
        for key, value in values.items():
            if value in seen[key]:
                raise BrowserRuntimeError(
                    "runtime_isolation_conflict", f"active sessions share {key}"
                )
            seen[key].add(value)


def spawn_isolated_process(
    argv: Sequence[str],
    *,
    isolation: StackIsolation,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn an argv-only process in the session's isolated environment."""

    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise BrowserRuntimeError("process_command_invalid", "process argv is invalid")
    isolation.validate()
    isolation.home_dir.mkdir(parents=True, exist_ok=True)
    isolation.cache_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(cwd) if cwd is not None else None,
        "env": isolation.environment(env),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(list(argv), **kwargs)
    except OSError as exc:
        raise BrowserRuntimeError(
            "process_start_failed", "runtime process could not start"
        ) from exc


@dataclass(frozen=True)
class ShutdownEvidence:
    """Metadata proving that registered process groups were observed stopped."""

    requested_pids: tuple[int, ...]
    orphaned_pids: tuple[int, ...]
    stopped_pids: tuple[int, ...]
    forced: bool
    confirmed: bool
    completed_at: str
    descendant_pids: tuple[int, ...] = ()


class ProcessTreeSupervisor:
    """Stops every registered child before a lease can be released."""

    def __init__(self, processes: Iterable[Any] = ()) -> None:
        self._processes: list[Any] = list(processes)
        self._lock = threading.Lock()
        self._stopping = False
        self._last_shutdown: ShutdownEvidence | None = None

    def register(self, process: Any) -> None:
        with self._lock:
            if self._stopping:
                raise BrowserRuntimeError(
                    "runtime_stopping", "cannot register a process while stopping"
                )
            self._processes.append(process)

    @staticmethod
    def _reap_registered_parent(process: Any) -> int | None:
        """Poll once so an exited registered child cannot remain a zombie."""

        value = getattr(process, "returncode", None)
        if value is not None:
            return value
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return None
        try:
            return poll()
        except (ChildProcessError, ProcessLookupError):
            return getattr(process, "returncode", None)
        except OSError:
            return None

    @property
    def stopped(self) -> bool:
        with self._lock:
            return all(not self._tree_alive(process) for process in self._processes)

    @property
    def last_shutdown(self) -> ShutdownEvidence | None:
        with self._lock:
            return self._last_shutdown

    @staticmethod
    def _pid(process: Any) -> int | None:
        value = getattr(process, "pid", None)
        return value if isinstance(value, int) and value > 0 else None

    @classmethod
    def _tree_alive(cls, process: Any) -> bool:
        pid = cls._pid(process)
        parent_returncode = cls._reap_registered_parent(process)
        if pid is None:
            return parent_returncode is None
        if os.name == "nt":
            if parent_returncode is None:
                return True
            return bool(_windows_descendant_pids(pid))
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return parent_returncode is None
        except PermissionError:
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return parent_returncode is None
            return True

    @classmethod
    def _send_signal(cls, process: Any, sig: int) -> None:
        pid = cls._pid(process)
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        if pid is None:
            terminator = getattr(process, "kill" if sig == kill_signal else "terminate", None)
            if callable(terminator):
                try:
                    terminator()
                except (OSError, ProcessLookupError):
                    pass
            return
        try:
            if os.name == "nt":
                descendants = _windows_descendant_pids(pid)
                for descendant in descendants:
                    subprocess.run(
                        ["taskkill", "/PID", str(descendant), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(pid, sig)
        except (OSError, ProcessLookupError):
            try:
                terminator = getattr(process, "kill" if sig == kill_signal else "terminate", None)
                if callable(terminator):
                    terminator()
            except (OSError, ProcessLookupError):
                pass

    def _shutdown_evidence(
        self,
        processes: list[Any],
        requested_pids: tuple[int, ...],
        orphaned_pids: tuple[int, ...],
        descendant_pids: tuple[int, ...],
        forced: bool,
        confirmed: bool,
    ) -> ShutdownEvidence:
        return ShutdownEvidence(
            requested_pids=requested_pids,
            orphaned_pids=orphaned_pids,
            stopped_pids=tuple(
                pid
                for pid in requested_pids
                if not any(self._pid(p) == pid and self._tree_alive(p) for p in processes)
            ),
            forced=forced,
            confirmed=confirmed,
            completed_at=datetime.now(UTC).isoformat(),
            descendant_pids=descendant_pids,
        )

    def _begin_stop(self) -> tuple[list[Any], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        requested_pids = tuple(pid for pid in (self._pid(p) for p in processes) if pid is not None)
        descendant_pids = tuple(
            sorted({child for pid in requested_pids for child in _windows_descendant_pids(pid)})
        )
        orphaned_pids = tuple(
            pid
            for process, pid in ((p, self._pid(p)) for p in processes)
            if pid is not None
            and self._reap_registered_parent(process) is not None
            and self._tree_alive(process)
        )
        for process in processes:
            if self._pid(process) is not None or self._reap_registered_parent(process) is None:
                self._send_signal(process, signal.SIGTERM)
        return processes, requested_pids, orphaned_pids, descendant_pids

    def _record_shutdown(self, evidence: ShutdownEvidence) -> None:
        with self._lock:
            self._last_shutdown = evidence

    def stop(self, *, grace_seconds: float = 10.0) -> bool:
        processes, requested_pids, orphaned_pids, descendant_pids = self._begin_stop()
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and not all(not self._tree_alive(p) for p in processes):
            time.sleep(0.05)
        forced = any(self._tree_alive(process) for process in processes)
        for process in processes:
            if self._tree_alive(process):
                self._send_signal(process, kill_signal)
        deadline = time.monotonic() + max(1.0, grace_seconds)
        while time.monotonic() < deadline and not all(not self._tree_alive(p) for p in processes):
            time.sleep(0.05)
        confirmed = all(not self._tree_alive(process) for process in processes)
        self._record_shutdown(
            self._shutdown_evidence(
                processes, requested_pids, orphaned_pids, descendant_pids, forced, confirmed
            )
        )
        return confirmed

    async def stop_async(self, *, grace_seconds: float = 10.0) -> bool:
        processes, requested_pids, orphaned_pids, descendant_pids = self._begin_stop()
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline and not all(
            not self._tree_alive(p) for p in processes
        ):
            await asyncio.sleep(0.05)
        forced = any(self._tree_alive(process) for process in processes)
        for process in processes:
            if self._tree_alive(process):
                self._send_signal(process, kill_signal)
        deadline = asyncio.get_running_loop().time() + max(1.0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline and not all(
            not self._tree_alive(p) for p in processes
        ):
            await asyncio.sleep(0.05)
        confirmed = all(not self._tree_alive(process) for process in processes)
        self._record_shutdown(
            self._shutdown_evidence(
                processes, requested_pids, orphaned_pids, descendant_pids, forced, confirmed
            )
        )
        return confirmed


def _windows_descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Return live descendants using the Windows process parent table."""

    if os.name != "nt":
        return ()
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot == invalid_handle:
            return ()
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            parent_by_pid: dict[int, int] = {}
            first = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while first:
                parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                first = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            descendants: list[int] = []
            frontier = [root_pid]
            visited: set[int] = set()
            while frontier:
                parent = frontier.pop()
                if parent in visited:
                    continue
                visited.add(parent)
                children = [pid for pid, parent_id in parent_by_pid.items() if parent_id == parent]
                descendants.extend(children)
                frontier.extend(children)
            return tuple(sorted(set(descendants)))
        finally:
            kernel32.CloseHandle(snapshot)
    except (AttributeError, OSError, TypeError, ValueError):
        return ()
