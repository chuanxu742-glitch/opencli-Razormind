"""Account-scoped browser runtime primitives.

This module owns the runtime-side safety boundary for account profiles.  It is
intentionally independent of the API and scheduler: callers provide the
already-authorized account, node, lease and command identifiers, while this
module enforces fencing, process-tree shutdown, profile isolation and
stopped-state snapshots.

Persistent state contains metadata only.  It never stores credentials, QR
contents, page HTML, cookies, tokens, or raw process errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlparse


_RUNTIME_VERSION = "1.0"
_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_POINTER_NAMES = {"current", "previous"}
_SINGLETON_NAMES = {"SingletonLock", "SingletonCookie", "SingletonSocket"}
_ALLOWED_SNAPSHOT_PASSWORD_STATES = {"not_present", "verified"}
_PASSWORD_DATABASE_NAMES = {"Login Data", "Login Data For Account"}
_PASSWORD_DATABASE_SIDECARS = {"Login Data-wal", "Login Data-shm", "Login Data-journal", "Login Data For Account-wal", "Login Data For Account-shm", "Login Data For Account-journal"}
_MAX_PASSWORD_SCAN_ENTRIES = 100_000


class BrowserRuntimeError(RuntimeError):
    """Safe, structured runtime error; ``detail`` never contains secrets."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:255]
        message = code if not self.detail else f"{code}: {self.detail}"
        super().__init__(message)


@dataclass(frozen=True)
class PasswordInventory:
    """Metadata-only password credential inventory."""

    status: str
    credential_count: int
    database_count: int
    database_paths: tuple[str, ...] = ()
    database_entries: tuple[dict[str, Any], ...] = ()
    enumeration_complete: bool = True
    directories_examined: int = 0
    files_examined: int = 0


@dataclass(frozen=True)
class ProfileRuntimePaths:
    """Live profile and its separate runtime/snapshot metadata directories."""

    profile_dir: Path
    state_dir: Path
    snapshots_dir: Path

    @classmethod
    def from_profile_dir(
        cls, profile_dir: str | os.PathLike[str], state_dir: str | os.PathLike[str] | None = None
    ) -> "ProfileRuntimePaths":
        profile = Path(profile_dir).expanduser()
        if not profile.is_absolute() or profile == Path("/"):
            raise BrowserRuntimeError("profile_path_invalid", "profile path must be absolute")
        configured_state_dir = state_dir if state_dir is not None else os.environ.get("RUNTIME_STATE_DIR")
        state = (
            Path(configured_state_dir).expanduser()
            if configured_state_dir is not None
            else profile.parent / f".{profile.name}.runtime"
        )
        if not state.is_absolute() or state == Path("/"):
            raise BrowserRuntimeError("state_path_invalid", "state path must be absolute")
        if state == profile or profile in state.parents or state in profile.parents:
            raise BrowserRuntimeError("profile_state_overlap", "profile and state paths must be separate")
        return cls(profile, state, state / "snapshots")

    def ensure(self) -> None:
        _ensure_private_directory(self.state_dir)
        _ensure_private_directory(self.snapshots_dir)
        if self.profile_dir.exists() and self.profile_dir.is_symlink():
            raise BrowserRuntimeError("profile_symlink", "profile path is a symlink")
        _ensure_private_directory(self.profile_dir)
        _reject_symlinks(self.profile_dir)
        self.assert_no_singleton_lock()

    def assert_no_singleton_lock(self) -> None:
        for name in _SINGLETON_NAMES:
            if (self.profile_dir / name).exists():
                raise BrowserRuntimeError("profile_locked", "profile singleton lock is present")

    @property
    def state_file(self) -> Path:
        return self.state_dir / "runtime-state.json"

    @property
    def dirty_marker(self) -> Path:
        return self.state_dir / "dirty.marker"

    @property
    def epoch_file(self) -> Path:
        return self.state_dir / "epoch.json"

    @property
    def epoch_lock_file(self) -> Path:
        return self.state_dir / "epoch.lock"

    @property
    def snapshot_lock_file(self) -> Path:
        return self.state_dir / "snapshot.lock"

    def read_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"state": "new", "runtime_version": _RUNTIME_VERSION}
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrowserRuntimeError("runtime_state_corrupt", "runtime state cannot be read") from exc
        if not isinstance(value, dict) or not isinstance(value.get("state"), str):
            raise BrowserRuntimeError("runtime_state_corrupt", "runtime state shape is invalid")
        return value

    def write_state(self, state: str, **metadata: Any) -> None:
        if state not in {"new", "running", "stopped", "quarantined", "recovery_required"}:
            raise BrowserRuntimeError("runtime_state_invalid", "unsupported runtime state")
        payload: dict[str, Any] = {
            "runtime_version": _RUNTIME_VERSION,
            "state": state,
            "updated_at": _now().isoformat(),
        }
        for key, value in metadata.items():
            if key not in {"workspace_id", "account_id", "profile_id", "node_id", "boot_id", "epoch", "reason", "manifest_ref"}:
                raise BrowserRuntimeError("runtime_state_invalid", "unsupported state metadata")
            if isinstance(value, (str, int, bool)) or value is None:
                payload[key] = value
        _atomic_write_json(self.state_file, payload)

    def mark_running(
        self,
        *,
        workspace_id: str,
        account_id: str,
        profile_id: str,
        node_id: str,
        boot_id: str,
        epoch: int,
        command_id: str,
    ) -> None:
        _validate_id(workspace_id, "workspace_id")
        _validate_id(account_id, "account_id")
        _validate_id(profile_id, "profile_id")
        _validate_id(node_id, "node_id")
        _validate_id(boot_id, "boot_id")
        _validate_id(command_id, "command_id")
        if epoch < 0:
            raise BrowserRuntimeError("epoch_invalid", "epoch must be non-negative")
        self.ensure()
        _atomic_write_json(
            self.dirty_marker,
            {"runtime_version": _RUNTIME_VERSION, "epoch": epoch, "boot_id": boot_id, "command_id": command_id},
        )
        self.write_state(
            "running",
            workspace_id=workspace_id,
            account_id=account_id,
            profile_id=profile_id,
            node_id=node_id,
            boot_id=boot_id,
            epoch=epoch,
        )

    def mark_stopped(self, *, clean: bool, reason: str = "") -> None:
        # Do not remove Chromium's singleton files here.  A stale singleton is
        # evidence that the writer did not stop cleanly and must keep the
        # profile quarantined rather than being stolen by a later session.
        _ensure_private_directory(self.state_dir)
        _ensure_private_directory(self.snapshots_dir)
        if self.profile_dir.exists() and self.profile_dir.is_symlink():
            raise BrowserRuntimeError("profile_symlink", "profile path is a symlink")
        _ensure_private_directory(self.profile_dir)
        if clean and any((self.profile_dir / name).exists() for name in _SINGLETON_NAMES):
            self.write_state("quarantined", reason="profile_locked")
            raise BrowserRuntimeError("profile_locked", "profile singleton lock is present")
        if clean:
            try:
                self.dirty_marker.unlink()
            except FileNotFoundError:
                pass
            self.write_state("stopped", reason=reason or None)
        else:
            self.write_state("quarantined", reason=reason or "unclean_stop")

    def mark_quarantined(self, reason: str) -> None:
        _ensure_private_directory(self.state_dir)
        _ensure_private_directory(self.snapshots_dir)
        self.write_state("quarantined", reason=reason)


@dataclass(frozen=True)
class EpochLease:
    """A lease generation accepted only for one node boot and one epoch."""

    node_id: str
    boot_id: str
    epoch: int
    expires_at: datetime
    owner_id: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("node_id", self.node_id),
            ("boot_id", self.boot_id),
            ("owner_id", self.owner_id),
        ):
            _validate_id(value, field_name)
        if self.epoch < 0:
            raise BrowserRuntimeError("epoch_invalid", "lease epoch must be non-negative")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise BrowserRuntimeError("lease_deadline_invalid", "lease deadline must include a timezone")

    def is_valid(self, now: datetime | None = None) -> bool:
        current = now or _now()
        return current < self.expires_at

    def assert_valid(self, now: datetime | None = None) -> None:
        if not self.is_valid(now):
            raise BrowserRuntimeError("lease_lost", "account lease is expired")


class EpochStore:
    """Durable monotonic epoch store with boot fencing."""

    def __init__(self, paths: ProfileRuntimePaths) -> None:
        self.paths = paths
        self.paths.ensure()
        self._lock = threading.Lock()

    @contextmanager
    def _critical_section(self) -> Iterable[None]:
        # The thread lock protects callers in one process; the durable lock
        # protects separate workers sharing this profile state directory.
        with self._lock:
            with _exclusive_file_lock(self.paths.epoch_lock_file):
                yield

    def _read(self) -> dict[str, Any]:
        if self.paths.epoch_file.is_symlink():
            raise BrowserRuntimeError("epoch_state_invalid", "epoch state path is a symlink")
        if not self.paths.epoch_file.exists():
            return {"max_epoch": 0, "boot_id": None}
        try:
            value = json.loads(self.paths.epoch_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrowserRuntimeError("epoch_state_corrupt", "epoch state cannot be read") from exc
        if not isinstance(value, dict) or type(value.get("max_epoch")) is not int:
            raise BrowserRuntimeError("epoch_state_corrupt", "epoch state shape is invalid")
        return value

    @staticmethod
    def _deadline(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise BrowserRuntimeError("epoch_state_corrupt", "lease deadline is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BrowserRuntimeError("epoch_state_corrupt", "lease deadline is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BrowserRuntimeError("epoch_state_corrupt", "lease deadline is invalid")
        return parsed

    @staticmethod
    def _lease_payload(
        *,
        max_epoch: int,
        boot_id: str | None,
        owner_id: str | None = None,
        node_id: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        if owner_id is not None:
            _validate_id(owner_id, "owner_id")
        if node_id is not None:
            _validate_id(node_id, "node_id")
        if lease_expires_at is not None:
            if lease_expires_at.tzinfo is None or lease_expires_at.utcoffset() is None:
                raise BrowserRuntimeError("lease_deadline_invalid", "lease deadline must include a timezone")
            if owner_id is None:
                raise BrowserRuntimeError("lease_owner_invalid", "lease owner is required for a deadline")
        return {
            "max_epoch": int(max_epoch),
            "boot_id": boot_id,
            "owner_id": owner_id,
            "node_id": node_id,
            "lease_expires_at": lease_expires_at.isoformat() if lease_expires_at is not None else None,
            "updated_at": _now().isoformat(),
        }

    def begin_boot(self, boot_id: str | None = None) -> str:
        with self._critical_section():
            value = self._read()
            current_boot = boot_id or uuid.uuid4().hex
            _validate_id(current_boot, "boot_id")
            _atomic_write_json(self.paths.epoch_file, self._lease_payload(max_epoch=max(0, int(value["max_epoch"])), boot_id=current_boot))
            return current_boot

    def allocate(
        self,
        requested_epoch: int | None = None,
        *,
        boot_id: str | None = None,
        owner_id: str | None = None,
        node_id: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> int:
        with self._critical_section():
            value = self._read()
            if boot_id is not None and value.get("boot_id") != boot_id:
                raise BrowserRuntimeError("stale_boot", "boot generation is no longer current")
            current = int(value["max_epoch"])
            if requested_epoch is not None and requested_epoch <= current:
                raise BrowserRuntimeError("stale_epoch", "epoch is not newer than persisted maximum")
            next_epoch = requested_epoch if requested_epoch is not None else current + 1
            if next_epoch < 0:
                raise BrowserRuntimeError("epoch_invalid", "epoch must be non-negative")
            _atomic_write_json(
                self.paths.epoch_file,
                self._lease_payload(
                    max_epoch=next_epoch,
                    boot_id=value.get("boot_id"),
                    owner_id=owner_id,
                    node_id=node_id,
                    lease_expires_at=lease_expires_at,
                ),
            )
            return next_epoch

    def assert_current(self, *, epoch: int, boot_id: str, owner_id: str | None = None) -> None:
        with self._critical_section():
            value = self._read()
            if value.get("boot_id") != boot_id or int(value.get("max_epoch", -1)) != epoch:
                raise BrowserRuntimeError("stale_epoch", "epoch or boot generation is no longer current")
            if owner_id is not None and value.get("owner_id") != owner_id:
                raise BrowserRuntimeError("stale_owner", "lease owner is no longer current")

    def renew(self, lease: EpochLease, *, expires_at: datetime) -> EpochLease:
        """Atomically renew one exact lease; stale renewers cannot overwrite it."""

        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise BrowserRuntimeError("lease_deadline_invalid", "lease deadline must include a timezone")
        with self._critical_section():
            value = self._read()
            persisted_deadline = self._deadline(value.get("lease_expires_at"))
            if (
                value.get("boot_id") != lease.boot_id
                or int(value.get("max_epoch", -1)) != lease.epoch
                or value.get("owner_id") != lease.owner_id
                or (value.get("node_id") not in (None, lease.node_id))
                or persisted_deadline != lease.expires_at
            ):
                raise BrowserRuntimeError("stale_lease", "lease generation or owner is no longer current")
            lease.assert_valid()
            if persisted_deadline is not None and expires_at <= persisted_deadline:
                raise BrowserRuntimeError("lease_deadline_invalid", "renewal deadline must advance")
            _atomic_write_json(
                self.paths.epoch_file,
                self._lease_payload(
                    max_epoch=lease.epoch,
                    boot_id=lease.boot_id,
                    owner_id=lease.owner_id,
                    node_id=lease.node_id,
                    lease_expires_at=expires_at,
                ),
            )
            return EpochLease(lease.node_id, lease.boot_id, lease.epoch, expires_at, lease.owner_id)

    def assert_lease_current(self, lease: EpochLease, *, now: datetime | None = None) -> None:
        lease.assert_valid(now)
        with self._critical_section():
            value = self._read()
            persisted_deadline = self._deadline(value.get("lease_expires_at"))
            if (
                value.get("boot_id") != lease.boot_id
                or int(value.get("max_epoch", -1)) != lease.epoch
                or value.get("owner_id") != lease.owner_id
                or (value.get("node_id") not in (None, lease.node_id))
                or persisted_deadline != lease.expires_at
            ):
                raise BrowserRuntimeError("stale_lease", "lease generation or owner is no longer current")


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
            raise BrowserRuntimeError("runtime_isolation_invalid", "DISPLAY must be a numeric display")
        for name, value in (
            ("cdp_port", self.cdp_port),
            ("bbx_port", self.bbx_port),
            ("daemon_port", self.daemon_port),
        ):
            if not 1 <= value <= 65535:
                raise BrowserRuntimeError("runtime_isolation_invalid", f"{name} is outside the port range")
        for path in (self.home_dir, self.cache_dir, self.profile_dir):
            if not path.is_absolute() or path == Path("/"):
                raise BrowserRuntimeError("runtime_isolation_invalid", "runtime paths must be absolute")
        resolved_paths = [path.resolve() for path in (self.home_dir, self.cache_dir, self.profile_dir)]
        if len(set(resolved_paths)) != len(resolved_paths):
            raise BrowserRuntimeError("runtime_isolation_invalid", "runtime paths must be distinct")
        if self.cache_dir == self.profile_dir or self.profile_dir in self.cache_dir.parents:
            raise BrowserRuntimeError("runtime_isolation_invalid", "cache and profile paths overlap")

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        self.validate()
        env = dict(base or os.environ)
        env.update(
            {
                "DISPLAY": self.display,
                "PROFILE_DIR": str(self.profile_dir),
                "HOME": str(self.home_dir),
                "XDG_CONFIG_HOME": str(self.home_dir / ".config"),
                "CLOAKBROWSER_CACHE_DIR": str(self.cache_dir),
                "OPENCLI_CDP_ENDPOINT": f"http://127.0.0.1:{self.cdp_port}",
                "OPENCLI_DAEMON_HOST": "127.0.0.1",
                "OPENCLI_DAEMON_PORT": str(self.daemon_port),
                "BBX_TCP_HOST": "127.0.0.1",
                "BBX_TCP_BIND_HOST": "127.0.0.1",
                "BBX_TCP_PORT": str(self.bbx_port),
                "OPENCLI_BROWSER_PROFILE_KIND": "authenticated",
            }
        )
        return env


def validate_parallel_isolation(stacks: Iterable[StackIsolation]) -> None:
    """Reject any active sessions that would share an isolation boundary."""

    seen: dict[str, set[str]] = {key: set() for key in ("display", "cdp", "bbx", "daemon", "home", "cache", "profile")}
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
                raise BrowserRuntimeError("runtime_isolation_conflict", f"active sessions share {key}")
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
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
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
        raise BrowserRuntimeError("process_start_failed", "runtime process could not start") from exc


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
                raise BrowserRuntimeError("runtime_stopping", "cannot register a process while stopping")
            self._processes.append(process)

    @staticmethod
    def _returncode(process: Any) -> int | None:
        value = getattr(process, "returncode", None)
        if value is not None:
            return value
        poll = getattr(process, "poll", None)
        return poll() if callable(poll) else None

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
        if pid is None:
            return cls._returncode(process) is None
        if os.name == "nt":
            # ``poll`` only observes the registered parent.  A descendant can
            # outlive it, so inspect parent PID links before deciding the tree
            # is gone; this is the orphan case taskkill /PID /T cannot detect
            # after the root exits.
            if cls._returncode(process) is None:
                return True
            if _windows_descendant_pids(pid):
                return True
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                return result.returncode == 0 and str(pid) in result.stdout
            except OSError:
                return False
        try:
            os.killpg(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return cls._returncode(process) is None

    @classmethod
    def _send_signal(cls, process: Any, sig: int) -> None:
        pid = cls._pid(process)
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        if pid is None:
            # Keep the supervisor useful with process adapters and test
            # doubles that expose terminate/kill but not a PID.  A registered
            # process without either capability is treated as still running
            # by ``stop`` and therefore cannot release its profile safely.
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

    def stop(self, *, grace_seconds: float = 10.0) -> bool:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        requested_pids = tuple(pid for pid in (self._pid(p) for p in processes) if pid is not None)
        descendant_pids = tuple(sorted({child for pid in requested_pids for child in _windows_descendant_pids(pid)}))
        orphaned_pids = tuple(pid for process, pid in ((p, self._pid(p)) for p in processes) if pid is not None and self._returncode(process) is not None and self._tree_alive(process))
        for process in processes:
            if self._pid(process) is not None or self._returncode(process) is None:
                self._send_signal(process, signal.SIGTERM)
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
        evidence = ShutdownEvidence(
            requested_pids=requested_pids,
            orphaned_pids=orphaned_pids,
            stopped_pids=tuple(pid for pid in requested_pids if not any(self._pid(p) == pid and self._tree_alive(p) for p in processes)),
            forced=forced,
            confirmed=confirmed,
            completed_at=_now().isoformat(),
            descendant_pids=descendant_pids,
        )
        with self._lock:
            self._last_shutdown = evidence
        return confirmed

    async def stop_async(self, *, grace_seconds: float = 10.0) -> bool:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        requested_pids = tuple(pid for pid in (self._pid(p) for p in processes) if pid is not None)
        descendant_pids = tuple(sorted({child for pid in requested_pids for child in _windows_descendant_pids(pid)}))
        orphaned_pids = tuple(pid for process, pid in ((p, self._pid(p)) for p in processes) if pid is not None and self._returncode(process) is not None and self._tree_alive(process))
        for process in processes:
            if self._pid(process) is not None or self._returncode(process) is None:
                self._send_signal(process, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline and not all(not self._tree_alive(p) for p in processes):
            await asyncio.sleep(0.05)
        forced = any(self._tree_alive(process) for process in processes)
        for process in processes:
            if self._tree_alive(process):
                self._send_signal(process, kill_signal)
        deadline = asyncio.get_running_loop().time() + max(1.0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline and not all(not self._tree_alive(p) for p in processes):
            await asyncio.sleep(0.05)
        confirmed = all(not self._tree_alive(process) for process in processes)
        evidence = ShutdownEvidence(
            requested_pids=requested_pids,
            orphaned_pids=orphaned_pids,
            stopped_pids=tuple(pid for pid in requested_pids if not any(self._pid(p) == pid and self._tree_alive(p) for p in processes)),
            forced=forced,
            confirmed=confirmed,
            completed_at=_now().isoformat(),
            descendant_pids=descendant_pids,
        )
        with self._lock:
            self._last_shutdown = evidence
        return confirmed


class LeaseSupervisor:
    """Watch a lease and stop the complete runtime when it is lost."""

    def __init__(
        self,
        lease: EpochLease,
        process_supervisor: ProcessTreeSupervisor,
        paths: ProfileRuntimePaths,
        *,
        lease_check: Callable[[EpochLease], bool] | None = None,
        poll_seconds: float = 0.5,
    ) -> None:
        self.lease = lease
        self.process_supervisor = process_supervisor
        self.paths = paths
        self.lease_check = lease_check
        self.poll_seconds = max(0.1, poll_seconds)
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise BrowserRuntimeError("runtime_supervisor_running", "supervisor is already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="browser-account-supervisor", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    @property
    def shutdown_evidence(self) -> ShutdownEvidence | None:
        return self.process_supervisor.last_shutdown

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            valid = self.lease.is_valid()
            if valid and self.lease_check is not None:
                try:
                    valid = bool(self.lease_check(self.lease))
                except Exception:
                    valid = False
            if valid:
                continue
            self.lost.set()
            stopped = self.process_supervisor.stop()
            if stopped:
                try:
                    self.paths.mark_stopped(clean=True, reason="lease_lost")
                except BrowserRuntimeError:
                    # A stopped process with a remaining singleton is not a
                    # cleanly reusable profile.  Preserve the evidence and
                    # fail closed instead of claiming the lease was released.
                    self.paths.mark_quarantined("lease_lost_profile_not_clean")
            else:
                self.paths.mark_stopped(clean=False, reason="lease_lost_stop_unconfirmed")
            return


def verify_password_manager_policy(policy_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Verify the managed Chromium policy without reading credential values."""

    path = Path(policy_path or os.environ.get("CHROMIUM_POLICY_FILE", "/etc/chromium/policies/managed/opencli-account-runtime.json"))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BrowserRuntimeError("credential_policy_unverified", "managed Chromium policy is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrowserRuntimeError("credential_policy_unverified", "managed Chromium policy is unavailable") from exc
    if not isinstance(payload, dict) or payload.get("PasswordManagerEnabled") is not False:
        raise BrowserRuntimeError("credential_policy_unverified", "password manager policy is not disabled")
    for key in ("AutofillAddressEnabled", "AutofillCreditCardEnabled"):
        if key in payload and payload[key] is not False:
            raise BrowserRuntimeError("credential_policy_unverified", "autofill policy is not disabled")
    return {"policy": "verified", "password_manager_enabled": False, "source": str(path)}


def inspect_password_inventory(profile_dir: str | os.PathLike[str]) -> PasswordInventory:
    """Count credential rows with a complete, bounded store enumeration.

    Only allowlisted Chromium password-store filenames are opened.  The
    enumeration records every matching database and sidecar path so a
    snapshot manifest can authorize the exact set rather than silently
    dropping a nested or account-specific ``Login Data`` store.
    """

    profile = Path(profile_dir)
    if not profile.is_absolute() or profile == Path("/") or profile.is_symlink() or not profile.is_dir():
        raise BrowserRuntimeError("profile_path_invalid", "profile path is invalid")
    stores, directories_examined, files_examined = _enumerate_password_store_paths(profile)
    databases = [path for path in stores if path.name in _PASSWORD_DATABASE_NAMES]
    total = 0
    unknown_databases = 0
    credential_counts: dict[Path, int] = {}
    for database in databases:
        safe_path = quote(str(database), safe="/\\:")
        # ``immutable=1`` guarantees SQLite cannot create a -shm sidecar while
        # inventorying a stopped profile.  A pre-existing WAL/SHM is still
        # enumerated below and makes the inventory unknown, so it cannot be
        # mistaken for a complete checkpoint.
        uri = f"file:{safe_path}?mode=ro&immutable=1"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            try:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logins' LIMIT 1"
                ).fetchone()
                if table is None:
                    unknown_databases += 1
                    continue
                # Counting rows is metadata-only.  Never select username or
                # password columns: even encrypted values must not leave the
                # browser's credential database through this path.
                row = connection.execute("SELECT COUNT(*) FROM logins").fetchone()
                count = int(row[0] if row else 0)
                credential_counts[database] = count
                total += count
            finally:
                connection.close()
                connection = None
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise BrowserRuntimeError("password_inventory_blocked", "password metadata cannot be verified") from exc
    entries = tuple(
        {
            "path": path.relative_to(profile).as_posix(),
            "role": "database" if path.name in _PASSWORD_DATABASE_NAMES else "sidecar",
            "credential_count": credential_counts.get(path, 0),
        }
        for path in stores
    )
    if unknown_databases or any(path.name not in _PASSWORD_DATABASE_NAMES for path in stores):
        status = "unknown"
    else:
        status = "present" if total else "not_present"
    # A database count is deliberately limited to primary stores; sidecars are
    # still included in ``database_paths`` and manifest metadata.
    return PasswordInventory(
        status,
        total,
        len(databases),
        tuple(path.relative_to(profile).as_posix() for path in stores),
        entries,
        True,
        directories_examined,
        files_examined,
    )


def snapshot_profile(
    paths: ProfileRuntimePaths,
    *,
    workspace_id: str,
    account_id: str,
    profile_id: str,
    node_id: str,
    command_id: str,
    writer_epoch: int,
    bundle: str,
    browser_version: str,
    require_password_policy: bool = True,
    policy_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create an immutable, checksum-verified stopped-state snapshot."""

    for name, value in (
        ("workspace_id", workspace_id),
        ("account_id", account_id),
        ("profile_id", profile_id),
        ("node_id", node_id),
        ("command_id", command_id),
    ):
        _validate_id(value, name, max_length=255)
    _validate_metadata_string(bundle, "bundle")
    _validate_metadata_string(browser_version, "browser_version")
    if writer_epoch < 0:
        raise BrowserRuntimeError("epoch_invalid", "writer epoch must be non-negative")
    paths.ensure()
    state = paths.read_state()
    if state.get("state") != "stopped" or paths.dirty_marker.exists():
        raise BrowserRuntimeError("profile_not_stopped", "profile is not in a clean stopped state")
    if require_password_policy:
        verify_password_manager_policy(policy_path)
    inventory = inspect_password_inventory(paths.profile_dir)
    if inventory.status not in _ALLOWED_SNAPSHOT_PASSWORD_STATES:
        raise BrowserRuntimeError("password_inventory_blocked", "profile contains unverifiable credentials")

    version = _next_snapshot_version(paths.snapshots_dir)
    staging = paths.snapshots_dir / f".staging-{uuid.uuid4().hex}"
    data_dir = staging / "profile"
    data_dir.mkdir(parents=True, exist_ok=False)
    checksums: list[dict[str, Any]] = []
    try:
        for source in _iter_profile_files(paths.profile_dir):
            relative = source.relative_to(paths.profile_dir).as_posix()
            destination = data_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            size, digest = _copy_and_hash(source, destination)
            checksums.append({"path": relative, "bytes": size, "sha256": digest})
        # A browser can create Login Data (or its WAL/SHM sidecars) while the
        # copy is in flight.  Require the exact pre/post inventory to match so
        # the manifest never authorizes a partial or unlisted password store.
        post_inventory = inspect_password_inventory(paths.profile_dir)
        _assert_password_inventory_stable(inventory, post_inventory)
        checksums_payload = json.dumps(checksums, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        checksums_ref = "checksums.json"
        _atomic_write_bytes(staging / checksums_ref, checksums_payload)
        checksum_digest = hashlib.sha256(checksums_payload).hexdigest()
        committed_at = _now()
        manifest: dict[str, Any] = {
            "runtime_version": _RUNTIME_VERSION,
            "workspace_id": workspace_id,
            "account_id": account_id,
            "profile_id": profile_id,
            "version": version,
            "node_id": node_id,
            "command_id": command_id,
            "writer_epoch": writer_epoch,
            "bundle": bundle,
            "browser_version": browser_version,
            "files_count": len(checksums),
            "total_bytes": sum(int(item["bytes"]) for item in checksums),
            "checksum_manifest_ref": checksums_ref,
            "complete_marker": "complete.marker",
            "committed_at": committed_at.isoformat(),
            "password_inventory_status": inventory.status,
            "password_credential_count": inventory.credential_count,
            "password_databases": [dict(entry) for entry in inventory.database_entries],
            "password_inventory": {
                "enumeration_complete": inventory.enumeration_complete,
                "directories_examined": inventory.directories_examined,
                "files_examined": inventory.files_examined,
                "database_count": inventory.database_count,
                "database_paths": list(inventory.database_paths),
            },
        }
        manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(staging / "manifest.json", manifest_payload)
        _atomic_write_bytes(staging / "complete.marker", f"sha256:{checksum_digest}\n".encode("ascii"))
        _fsync_tree(staging)
        # Serialize the final version/pointer decision.  Two stopped writers
        # may copy concurrently, but only one can claim each deterministic
        # version and advance current; the loser receives a fresh version.
        with _exclusive_file_lock(paths.snapshot_lock_file):
            version = _next_snapshot_version(paths.snapshots_dir)
            manifest["version"] = version
            _atomic_write_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            _fsync_tree(staging)
            previous = _read_pointer(paths.snapshots_dir / "current")
            previous_dir = paths.snapshots_dir / previous if previous is not None else None
            if previous is not None and (
                previous in _POINTER_NAMES
                or not re.fullmatch(r"v[1-9][0-9]*", previous)
                or previous_dir is None
                or previous_dir.is_symlink()
                or not previous_dir.is_dir()
            ):
                raise BrowserRuntimeError("profile_manifest_invalid", "current snapshot pointer is invalid")
            final_dir = paths.snapshots_dir / f"v{version}"
            os.replace(staging, final_dir)
            if previous:
                _atomic_write_text(paths.snapshots_dir / "previous", previous)
            _atomic_write_text(paths.snapshots_dir / "current", final_dir.name)
            _fsync_directory(paths.snapshots_dir)
            paths.write_state("stopped", manifest_ref=f"snapshots/{final_dir.name}/manifest.json")
            return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_current_snapshot(
    paths: ProfileRuntimePaths,
    *,
    expected_workspace_id: str | None = None,
    expected_account_id: str | None = None,
    expected_profile_id: str | None = None,
    expected_node_id: str | None = None,
    snapshot_version: int | None = None,
    expected_runtime_version: str = _RUNTIME_VERSION,
    expected_bundle: str | None = None,
    expected_browser_version: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Validate the current manifest, marker and every copied file.

    The optional identity arguments let a caller bind recovery to the
    already-authorized account/session.  Without that binding, a valid
    snapshot from another account could be copied into this profile path.
    """

    paths.ensure()
    root = paths.snapshots_dir.resolve()
    snapshot_dir = _select_snapshot_dir(root, snapshot_version=snapshot_version)
    try:
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        checksums = json.loads((snapshot_dir / "checksums.json").read_text(encoding="utf-8"))
        marker = (snapshot_dir / "complete.marker").read_text(encoding="ascii").strip()
    except (OSError, ValueError, UnicodeError) as exc:
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot metadata is incomplete") from exc
    if not isinstance(manifest, dict) or not isinstance(checksums, list) or not marker.startswith("sha256:"):
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot metadata shape is invalid")
    if manifest.get("runtime_version") != expected_runtime_version or expected_runtime_version != _RUNTIME_VERSION:
        raise BrowserRuntimeError("profile_manifest_incompatible", "snapshot runtime version is incompatible")
    expected_identity = {
        "workspace_id": expected_workspace_id,
        "account_id": expected_account_id,
        "profile_id": expected_profile_id,
        "node_id": expected_node_id,
    }
    for field, expected in expected_identity.items():
        if expected is not None and manifest.get(field) != expected:
            raise BrowserRuntimeError("profile_identity_mismatch", "snapshot identity does not match account")
    if expected_bundle is not None and manifest.get("bundle") != expected_bundle:
        raise BrowserRuntimeError("profile_manifest_incompatible", "snapshot bundle is incompatible")
    if expected_browser_version is not None and manifest.get("browser_version") != expected_browser_version:
        raise BrowserRuntimeError("profile_manifest_incompatible", "snapshot browser version is incompatible")
    checksums_payload = json.dumps(checksums, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if marker != f"sha256:{hashlib.sha256(checksums_payload).hexdigest()}":
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot marker does not match checksums")
    if manifest.get("password_inventory_status") not in _ALLOWED_SNAPSHOT_PASSWORD_STATES:
        raise BrowserRuntimeError("password_inventory_blocked", "snapshot credential metadata is not safe")
    data_dir = snapshot_dir / "profile"
    total_bytes = 0
    for item in checksums:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot checksum entry is invalid")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or not isinstance(item.get("sha256"), str):
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot path is invalid")
        source = (data_dir / relative).resolve()
        if data_dir.resolve() not in source.parents or not source.is_file() or source.is_symlink():
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot file is missing")
        size, digest = _hash_file(source)
        if size != item.get("bytes") or digest != item["sha256"]:
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot checksum mismatch")
        total_bytes += size
    if total_bytes != manifest.get("total_bytes") or len(checksums) != manifest.get("files_count"):
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot totals do not match")
    snapshot_inventory = inspect_password_inventory(data_dir)
    _validate_manifest_password_inventory(manifest, snapshot_inventory)
    return manifest, snapshot_dir


def restore_current_snapshot(
    paths: ProfileRuntimePaths,
    *,
    allow_empty: bool = False,
    expected_workspace_id: str | None = None,
    expected_account_id: str | None = None,
    expected_profile_id: str | None = None,
    expected_node_id: str | None = None,
    snapshot_version: int | None = None,
    expected_runtime_version: str = _RUNTIME_VERSION,
    expected_bundle: str | None = None,
    expected_browser_version: str | None = None,
) -> dict[str, Any] | None:
    """Restore only a complete committed snapshot; preserve dirty evidence."""

    paths.ensure()
    state = paths.read_state()
    if state.get("state") in {"running", "quarantined", "recovery_required"} or paths.dirty_marker.exists():
        paths.mark_quarantined("dirty_profile_requires_isolation")
        raise BrowserRuntimeError("profile_dirty", "dirty profile state is quarantined")
    try:
        manifest, snapshot_dir = validate_current_snapshot(
            paths,
            expected_workspace_id=expected_workspace_id,
            expected_account_id=expected_account_id,
            expected_profile_id=expected_profile_id,
            expected_node_id=expected_node_id,
            snapshot_version=snapshot_version,
            expected_runtime_version=expected_runtime_version,
            expected_bundle=expected_bundle,
            expected_browser_version=expected_browser_version,
        )
    except BrowserRuntimeError as exc:
        if allow_empty and exc.code == "profile_missing" and not any(paths.profile_dir.iterdir()):
            paths.write_state("stopped", reason="new_profile")
            return None
        paths.mark_quarantined(exc.code)
        raise

    staging = paths.profile_dir.parent / f".{paths.profile_dir.name}.restore-{uuid.uuid4().hex}"
    target_data = staging
    target_data.mkdir(parents=True, exist_ok=False)
    try:
        source_data = snapshot_dir / "profile"
        _copy_tree_streaming(source_data, target_data)
        _reject_symlinks(target_data)
        old_dir: Path | None = None
        if paths.profile_dir.exists():
            old_dir = paths.profile_dir.parent / f".{paths.profile_dir.name}.dirty-{uuid.uuid4().hex}"
            os.replace(paths.profile_dir, old_dir)
        os.replace(staging, paths.profile_dir)
        paths.write_state("stopped", manifest_ref=f"snapshots/{snapshot_dir.name}/manifest.json", reason="restored")
        return manifest
    except Exception:
        if paths.profile_dir.exists() and paths.profile_dir.is_dir() and not any(paths.profile_dir.iterdir()):
            paths.profile_dir.rmdir()
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_id(value: str, field: str, *, max_length: int = 128) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length or not _SAFE_COMPONENT.fullmatch(value):
        raise BrowserRuntimeError("identifier_invalid", f"{field} is invalid")


def _windows_descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Return live descendants using the Windows process parent table."""

    # This helper is intentionally stdlib-only; unlike a shell query it works
    # after the registered parent has exited and keeps process names/commands
    # out of runtime evidence.
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


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterable[None]:
    """Hold a small process-shared lock without changing global git/runtime config."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BrowserRuntimeError("epoch_lock_unavailable", "epoch fencing lock path is a symlink")
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise BrowserRuntimeError("epoch_lock_unavailable", "epoch fencing lock cannot be opened") from exc
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise BrowserRuntimeError("epoch_lock_unavailable", "epoch fencing lock cannot be acquired") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise BrowserRuntimeError("epoch_lock_unavailable", "epoch fencing lock cannot be acquired") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _validate_metadata_string(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+@\- ]*", value)
    ):
        raise BrowserRuntimeError("metadata_invalid", f"{field} is invalid")

def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_private_directory(path: Path) -> None:
    if not path.is_absolute() or path == Path("/"):
        raise BrowserRuntimeError("path_invalid", "runtime path must be absolute and non-root")
    if path.exists() and path.is_symlink():
        raise BrowserRuntimeError("path_symlink", "runtime path is a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise BrowserRuntimeError("path_permissions", "runtime path permissions cannot be restricted") from exc


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise BrowserRuntimeError("profile_symlink", "profile contains a symlink root")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BrowserRuntimeError("profile_symlink", "profile contains a symlink")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_text(path: Path, data: str) -> None:
    _atomic_write_bytes(path, data.encode("utf-8"))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            try:
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                pass
    for path in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as source_handle, destination.open("wb") as destination_handle:
        while True:
            chunk = source_handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            destination_handle.write(chunk)
            total += len(chunk)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    shutil.copystat(source, destination, follow_symlinks=False)
    return total, digest.hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _iter_profile_files(root: Path) -> Iterable[Path]:
    _reject_symlinks(root)
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.name in _SINGLETON_NAMES:
            raise BrowserRuntimeError("profile_locked", "profile singleton lock is present")
        if path.is_symlink():
            raise BrowserRuntimeError("profile_symlink", "profile contains a symlink")
        if not path.is_file():
            raise BrowserRuntimeError("profile_file_invalid", "profile contains a non-regular file")
        yield path


def _enumerate_password_store_paths(root: Path) -> tuple[list[Path], int, int]:
    """Enumerate password stores without unbounded globbing or symlink skips."""

    stores: list[Path] = []
    directories_examined = 0
    files_examined = 0

    def onerror(_: OSError) -> None:
        raise BrowserRuntimeError("password_inventory_blocked", "password metadata cannot be enumerated")

    try:
        walker = os.walk(root, topdown=True, followlinks=False, onerror=onerror)
        for current, dirnames, filenames in walker:
            directories_examined += 1
            if directories_examined + files_examined > _MAX_PASSWORD_SCAN_ENTRIES:
                raise BrowserRuntimeError("password_inventory_unbounded", "password store enumeration exceeded its bound")
            for dirname in sorted(dirnames):
                directory = Path(current) / dirname
                if directory.is_symlink():
                    raise BrowserRuntimeError("profile_symlink", "profile contains a symlink")
            for filename in sorted(filenames):
                files_examined += 1
                if directories_examined + files_examined > _MAX_PASSWORD_SCAN_ENTRIES:
                    raise BrowserRuntimeError("password_inventory_unbounded", "password store enumeration exceeded its bound")
                path = Path(current) / filename
                if path.is_symlink():
                    raise BrowserRuntimeError("profile_symlink", "profile contains a symlink")
                if filename not in _PASSWORD_DATABASE_NAMES and filename not in _PASSWORD_DATABASE_SIDECARS:
                    continue
                if not path.is_file():
                    raise BrowserRuntimeError("password_inventory_blocked", "password store is not a regular file")
                stores.append(path)
    except BrowserRuntimeError:
        raise
    except OSError as exc:
        raise BrowserRuntimeError("password_inventory_blocked", "password metadata cannot be enumerated") from exc
    stores.sort(key=lambda path: path.relative_to(root).as_posix())
    return stores, directories_examined, files_examined


def _assert_password_inventory_stable(before: PasswordInventory, after: PasswordInventory) -> None:
    if (
        before.status != after.status
        or before.credential_count != after.credential_count
        or before.database_count != after.database_count
        or before.database_paths != after.database_paths
        or before.database_entries != after.database_entries
        or not after.enumeration_complete
    ):
        raise BrowserRuntimeError("password_inventory_changed", "password store changed during snapshot")


def _validate_manifest_password_inventory(manifest: Mapping[str, Any], inventory: PasswordInventory) -> None:
    """Require snapshot metadata to authorize the exact observed store set."""

    listed = manifest.get("password_databases")
    inventory_metadata = manifest.get("password_inventory")
    if not isinstance(listed, list) or not isinstance(inventory_metadata, dict):
        raise BrowserRuntimeError("password_manifest_unverified", "password store authorization is missing")
    if inventory_metadata.get("enumeration_complete") is not True:
        raise BrowserRuntimeError("password_inventory_blocked", "password store enumeration was incomplete")
    if manifest.get("password_inventory_status") != inventory.status or manifest.get("password_credential_count") != inventory.credential_count:
        raise BrowserRuntimeError("password_manifest_mismatch", "password metadata does not match the store")
    expected_paths = list(inventory.database_paths)
    listed_paths: list[str] = []
    normalized_entries: list[dict[str, Any]] = []
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("role"), str):
            raise BrowserRuntimeError("password_manifest_unverified", "password store authorization is malformed")
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != entry["path"]:
            raise BrowserRuntimeError("password_manifest_unverified", "password store path is invalid")
        name = relative.name
        if name not in _PASSWORD_DATABASE_NAMES and name not in _PASSWORD_DATABASE_SIDECARS:
            raise BrowserRuntimeError("password_manifest_unverified", "password store is not allowlisted")
        role = "database" if name in _PASSWORD_DATABASE_NAMES else "sidecar"
        if entry["role"] != role or not isinstance(entry.get("credential_count"), int) or entry["credential_count"] < 0:
            raise BrowserRuntimeError("password_manifest_unverified", "password store metadata is invalid")
        listed_paths.append(entry["path"])
        normalized_entries.append({"path": entry["path"], "role": role, "credential_count": entry["credential_count"]})
    if listed_paths != expected_paths or len(set(listed_paths)) != len(listed_paths):
        raise BrowserRuntimeError("password_database_unlisted", "snapshot contains an unlisted password store")
    if normalized_entries != [dict(entry) for entry in inventory.database_entries]:
        raise BrowserRuntimeError("password_manifest_mismatch", "password store metadata does not match the store")
    if inventory_metadata.get("database_count") != inventory.database_count or inventory_metadata.get("database_paths") != expected_paths:
        raise BrowserRuntimeError("password_manifest_mismatch", "password inventory evidence does not match the store")


def _copy_tree_streaming(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise BrowserRuntimeError("profile_symlink", "snapshot contains a symlink")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_and_hash(path, target)
        else:
            raise BrowserRuntimeError("profile_file_invalid", "snapshot contains a non-regular file")


def _next_snapshot_version(root: Path) -> int:
    versions = []
    for path in root.glob("v*"):
        if path.is_symlink():
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot version path is a symlink")
        if path.is_dir() and re.fullmatch(r"v[1-9][0-9]*", path.name):
            versions.append(int(path.name[1:]))
    return max(versions, default=0) + 1


def _select_snapshot_dir(root: Path, *, snapshot_version: int | None = None) -> Path:
    """Select one version only after rejecting malformed/ambiguous candidates."""

    candidates: dict[int, tuple[int, Path]] = {}
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot versions cannot be enumerated") from exc
    for child in children:
        if child.is_symlink() and child.name.startswith("v"):
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot version path is a symlink")
        if not child.is_dir() or not child.name.startswith("v"):
            continue
        if not re.fullmatch(r"v[1-9][0-9]*", child.name):
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot version path is invalid")
        directory_version = int(child.name[1:])
        try:
            manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot manifest cannot be read") from exc
        manifest_version = manifest.get("version") if isinstance(manifest, dict) else None
        if type(manifest_version) is not int or manifest_version < 1 or manifest_version != directory_version:
            raise BrowserRuntimeError("profile_manifest_invalid", "snapshot version is inconsistent")
        if manifest_version in candidates:
            raise BrowserRuntimeError("profile_version_ambiguous", "multiple snapshots claim the same version")
        candidates[manifest_version] = (directory_version, child)
    if snapshot_version is not None:
        if type(snapshot_version) is not int or snapshot_version < 1:
            raise BrowserRuntimeError("profile_version_invalid", "requested snapshot version is invalid")
        selected = candidates.get(snapshot_version)
        if selected is None:
            raise BrowserRuntimeError("profile_version_missing", "requested snapshot version does not exist")
        return selected[1]
    pointer = _read_pointer(root / "current")
    if pointer is None:
        if len(candidates) > 1:
            raise BrowserRuntimeError("profile_version_ambiguous", "snapshot selection has no current version")
        raise BrowserRuntimeError("profile_missing", "no committed profile snapshot exists")
    if pointer in _POINTER_NAMES or not re.fullmatch(r"v[1-9][0-9]*", pointer):
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot pointer is invalid")
    directory_version = int(pointer[1:])
    selected = candidates.get(directory_version)
    if selected is None or selected[1].name != pointer:
        raise BrowserRuntimeError("profile_manifest_invalid", "current snapshot pointer is invalid")
    return selected[1]


def _read_pointer(path: Path) -> str | None:
    if path.is_symlink():
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot pointer is a symlink")
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot pointer cannot be read") from exc
    return value or None


@dataclass(frozen=True)
class SessionRuntimeBinding:
    """Server-owned endpoints for one isolated account session.

    A binding is deliberately not part of the wire request.  The center/runtime
    supervisor installs it after allocating the isolated stack; dispatch only
    resolves an exact session/node/epoch tuple from this registry.
    """

    session_id: str
    node_id: str
    boot_id: str
    epoch: int
    cdp_endpoint: str
    bbx_remote: str
    daemon_endpoint: str
    profile_dir: Path
    home_dir: Path
    cache_dir: Path
    display: str
    cdp_port: int
    bbx_port: int
    daemon_port: int

    def validate(self) -> None:
        for field_name, value in (
            ("session_id", self.session_id),
            ("node_id", self.node_id),
            ("boot_id", self.boot_id),
        ):
            _validate_id(value, field_name, max_length=255)
        if self.epoch < 0:
            raise BrowserRuntimeError("epoch_invalid", "session epoch must be non-negative")
        stack = StackIsolation(
            display=self.display,
            cdp_port=self.cdp_port,
            bbx_port=self.bbx_port,
            daemon_port=self.daemon_port,
            home_dir=self.home_dir,
            cache_dir=self.cache_dir,
            profile_dir=self.profile_dir,
        )
        stack.validate()
        for name, endpoint in (
            ("cdp_endpoint", self.cdp_endpoint),
            ("bbx_remote", self.bbx_remote),
            ("daemon_endpoint", self.daemon_endpoint),
        ):
            parsed = urlparse(endpoint)
            if parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise BrowserRuntimeError(
                    "runtime_endpoint_invalid",
                    f"{name} must resolve to the isolated local runtime",
                )
        expected_cdp = f"http://127.0.0.1:{self.cdp_port}"
        if self.cdp_endpoint.rstrip("/") != expected_cdp:
            raise BrowserRuntimeError("runtime_endpoint_invalid", "CDP endpoint does not match session port")

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        self.validate()
        isolation = StackIsolation(
            display=self.display,
            cdp_port=self.cdp_port,
            bbx_port=self.bbx_port,
            daemon_port=self.daemon_port,
            home_dir=self.home_dir,
            cache_dir=self.cache_dir,
            profile_dir=self.profile_dir,
        )
        env = isolation.environment(base)
        env["BBX_REMOTE"] = self.bbx_remote
        env["OPENCLI_DAEMON_ENDPOINT"] = self.daemon_endpoint
        return env


class SessionRuntimeRegistry:
    """In-memory server-side binding registry with exact-generation fencing."""

    def __init__(self) -> None:
        self._bindings: dict[str, SessionRuntimeBinding] = {}
        self._lock = threading.RLock()

    def register(self, binding: SessionRuntimeBinding) -> None:
        binding.validate()
        with self._lock:
            existing = self._bindings.get(binding.session_id)
            if existing is not None and (
                existing.node_id != binding.node_id
                or existing.boot_id != binding.boot_id
                or existing.epoch != binding.epoch
            ):
                raise BrowserRuntimeError("stale_runtime_binding", "session runtime generation is already fenced")
            self._bindings[binding.session_id] = binding

    def resolve(
        self,
        *,
        session_id: str,
        node_id: str,
        boot_id: str,
        epoch: int,
    ) -> SessionRuntimeBinding:
        with self._lock:
            binding = self._bindings.get(session_id)
        if binding is None:
            raise BrowserRuntimeError("capability_missing", "session runtime binding is unavailable")
        if (
            binding.node_id != node_id
            or binding.boot_id != boot_id
            or binding.epoch != epoch
        ):
            raise BrowserRuntimeError("stale_runtime_binding", "session runtime generation is no longer current")
        binding.validate()
        return binding

    def revoke(self, *, session_id: str, node_id: str, boot_id: str, epoch: int) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            if (
                binding.node_id != node_id
                or binding.boot_id != boot_id
                or binding.epoch != epoch
            ):
                raise BrowserRuntimeError("stale_runtime_binding", "cannot revoke a newer runtime generation")
            self._bindings.pop(session_id, None)


@dataclass(frozen=True)
class RuntimeLeaseAdmission:
    """Local authenticated claim/renew/result admission, never a scheduler."""

    claim: Any
    session: Any
    node_identity: Any
    result: Any | None = None


class AuthenticatedRuntimeLeaseBook:
    """Fail-closed local fencing for commands accepted by an edge node."""

    def __init__(self) -> None:
        self._claims: dict[str, RuntimeLeaseAdmission] = {}
        self._lock = threading.RLock()

    def claim(self, *, claim: Any, session: Any, node_identity: Any, now: datetime | None = None) -> RuntimeLeaseAdmission:
        current = now or _now()
        if node_identity.node_id != claim.node_id or node_identity.boot_id != claim.boot_id:
            raise BrowserRuntimeError("node_identity_mismatch", "claim identity does not match authenticated node")
        if current >= claim.expires_at:
            raise BrowserRuntimeError("claim_expired", "claim deadline has passed")
        if session.session_id != claim.session_id or session.node_id != claim.node_id:
            raise BrowserRuntimeError("session_claim_mismatch", "session is not owned by claim")
        if session.node_boot_id != claim.boot_id or session.epoch != claim.epoch:
            raise BrowserRuntimeError("session_claim_mismatch", "session generation is not owned by claim")
        admission = RuntimeLeaseAdmission(claim, session, node_identity)
        with self._lock:
            existing = self._claims.get(claim.command_id)
            if existing is not None and existing.claim != claim:
                raise BrowserRuntimeError("duplicate_claim", "command is already claimed by another generation")
            self._claims[claim.command_id] = admission
        return admission

    def renew(self, *, claim: Any, node_identity: Any, now: datetime | None = None) -> RuntimeLeaseAdmission:
        current = now or _now()
        if node_identity.node_id != claim.node_id or node_identity.boot_id != claim.boot_id:
            raise BrowserRuntimeError("node_identity_mismatch", "renewal identity does not match authenticated node")
        with self._lock:
            admission = self._claims.get(claim.command_id)
        if admission is None or admission.claim != claim:
            raise BrowserRuntimeError("stale_claim", "renewal does not match the admitted claim")
        if current >= claim.expires_at:
            raise BrowserRuntimeError("claim_expired", "claim deadline has passed")
        return admission

    def result(self, *, result: Any, node_identity: Any, now: datetime | None = None) -> RuntimeLeaseAdmission:
        if node_identity.node_id != result.node_id or node_identity.boot_id != result.boot_id:
            raise BrowserRuntimeError("node_identity_mismatch", "result identity does not match authenticated node")
        with self._lock:
            admission = self._claims.get(result.command_id)
            if admission is None:
                raise BrowserRuntimeError("stale_claim", "result has no admitted claim")
            claim = admission.claim
            if (
                result.workspace_id != claim.workspace_id
                or result.account_id != claim.account_id
                or result.session_id != claim.session_id
                or result.epoch != claim.epoch
                or result.expected_revision != claim.expected_revision
            ):
                raise BrowserRuntimeError("result_claim_mismatch", "result does not match its claim")
            updated = RuntimeLeaseAdmission(admission.claim, admission.session, admission.node_identity, result)
            self._claims[result.command_id] = updated
            return updated

    def revoke(self, command_id: str) -> None:
        with self._lock:
            self._claims.pop(command_id, None)


_SESSION_RUNTIME_REGISTRY = SessionRuntimeRegistry()
_RUNTIME_LEASE_BOOK = AuthenticatedRuntimeLeaseBook()


def session_runtime_registry() -> SessionRuntimeRegistry:
    return _SESSION_RUNTIME_REGISTRY


def runtime_lease_book() -> AuthenticatedRuntimeLeaseBook:
    return _RUNTIME_LEASE_BOOK


def _cli(argv: Sequence[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print("用法: python -m backend.browser_account_runtime verify-password-policy [path]")
        return 0
    if argv[0] == "verify-password-policy":
        result = verify_password_manager_policy(argv[1] if len(argv) > 1 else None)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    print("不支持的运行时命令", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
