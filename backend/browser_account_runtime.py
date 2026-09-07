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
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.request import urlopen

_RUNTIME_VERSION = "1.0"
_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_POINTER_NAMES = {"current", "previous"}
_SINGLETON_NAMES = {"SingletonLock", "SingletonCookie", "SingletonSocket"}
_ALLOWED_SNAPSHOT_PASSWORD_STATES = {"not_present", "verified"}
_PASSWORD_DATABASE_NAMES = {"Login Data", "Login Data For Account"}
_PASSWORD_DATABASE_SIDECARS = {
    "Login Data-wal",
    "Login Data-shm",
    "Login Data-journal",
    "Login Data For Account-wal",
    "Login Data For Account-shm",
    "Login Data For Account-journal",
}
_MAX_PASSWORD_SCAN_ENTRIES = 100_000
# OpenCLI 1.8.7 and its Chrome extension intentionally share this fixed port.
# Per-session daemon ports split the extension from its daemon and are unsupported.
_OPENCLI_DAEMON_PORT = 19_825


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
    ) -> ProfileRuntimePaths:
        profile = Path(profile_dir).expanduser()
        if not profile.is_absolute() or profile == Path("/"):
            raise BrowserRuntimeError("profile_path_invalid", "profile path must be absolute")
        configured_state_dir = (
            state_dir if state_dir is not None else os.environ.get("RUNTIME_STATE_DIR")
        )
        state = (
            Path(configured_state_dir).expanduser()
            if configured_state_dir is not None
            else profile.parent / f".{profile.name}.runtime"
        )
        if not state.is_absolute() or state == Path("/"):
            raise BrowserRuntimeError("state_path_invalid", "state path must be absolute")
        if state == profile or profile in state.parents or state in profile.parents:
            raise BrowserRuntimeError(
                "profile_state_overlap", "profile and state paths must be separate"
            )
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
            raise BrowserRuntimeError(
                "runtime_state_corrupt", "runtime state cannot be read"
            ) from exc
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
            if key not in {
                "workspace_id",
                "account_id",
                "profile_id",
                "node_id",
                "boot_id",
                "epoch",
                "reason",
                "manifest_ref",
            }:
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
            {
                "runtime_version": _RUNTIME_VERSION,
                "epoch": epoch,
                "boot_id": boot_id,
                "command_id": command_id,
            },
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
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise BrowserRuntimeError(
                "lease_deadline_invalid", "lease deadline must include a timezone"
            )

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
                raise BrowserRuntimeError(
                    "lease_deadline_invalid", "lease deadline must include a timezone"
                )
            if owner_id is None:
                raise BrowserRuntimeError(
                    "lease_owner_invalid", "lease owner is required for a deadline"
                )
        return {
            "max_epoch": int(max_epoch),
            "boot_id": boot_id,
            "owner_id": owner_id,
            "node_id": node_id,
            "lease_expires_at": lease_expires_at.isoformat()
            if lease_expires_at is not None
            else None,
            "updated_at": _now().isoformat(),
        }

    def begin_boot(self, boot_id: str | None = None) -> str:
        with self._critical_section():
            value = self._read()
            current_boot = boot_id or uuid.uuid4().hex
            _validate_id(current_boot, "boot_id")
            _atomic_write_json(
                self.paths.epoch_file,
                self._lease_payload(
                    max_epoch=max(0, int(value["max_epoch"])), boot_id=current_boot
                ),
            )
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
                raise BrowserRuntimeError(
                    "stale_epoch", "epoch is not newer than persisted maximum"
                )
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
                raise BrowserRuntimeError(
                    "stale_epoch", "epoch or boot generation is no longer current"
                )
            if owner_id is not None and value.get("owner_id") != owner_id:
                raise BrowserRuntimeError("stale_owner", "lease owner is no longer current")

    def renew(self, lease: EpochLease, *, expires_at: datetime) -> EpochLease:
        """Atomically renew one exact lease; stale renewers cannot overwrite it."""

        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise BrowserRuntimeError(
                "lease_deadline_invalid", "lease deadline must include a timezone"
            )
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
                raise BrowserRuntimeError(
                    "stale_lease", "lease generation or owner is no longer current"
                )
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
                raise BrowserRuntimeError(
                    "stale_lease", "lease generation or owner is no longer current"
                )


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
            # An unknown local wait failure cannot prove the parent stopped.
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
            # ``poll`` only observes the registered parent.  A descendant can
            # outlive it, so inspect parent PID links before deciding the tree
            # is gone; this is the orphan case taskkill /PID /T cannot detect
            # after the root exits.
            if parent_returncode is None:
                return True
            return bool(_windows_descendant_pids(pid))
        try:
            # Probe after polling: waitpid(WNOHANG) reaps a dead Popen group
            # leader, while killpg still detects any surviving descendants.
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
            stopped_pids=tuple(
                pid
                for pid in requested_pids
                if not any(self._pid(p) == pid and self._tree_alive(p) for p in processes)
            ),
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
        evidence = ShutdownEvidence(
            requested_pids=requested_pids,
            orphaned_pids=orphaned_pids,
            stopped_pids=tuple(
                pid
                for pid in requested_pids
                if not any(self._pid(p) == pid and self._tree_alive(p) for p in processes)
            ),
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
        lease_check: Callable[[EpochLease], bool | None] | None = None,
        poll_seconds: float = 0.5,
        on_lost: Callable[[bool], None] | None = None,
    ) -> None:
        self.lease = lease
        self.process_supervisor = process_supervisor
        self.paths = paths
        self.lease_check = lease_check
        self.poll_seconds = max(0.1, poll_seconds)
        self.on_lost = on_lost
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._lease_lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise BrowserRuntimeError("runtime_supervisor_running", "supervisor is already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="browser-account-supervisor", daemon=True
        )
        self._thread.start()

    def close(self, timeout: float = 25.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def update_lease(self, lease: EpochLease) -> None:
        """Advance the watched deadline only for the same fenced generation."""

        with self._lease_lock:
            current = self.lease
            if (
                current.node_id != lease.node_id
                or current.boot_id != lease.boot_id
                or current.epoch != lease.epoch
                or current.owner_id != lease.owner_id
                or lease.expires_at <= current.expires_at
            ):
                raise BrowserRuntimeError(
                    "stale_lease",
                    "lease supervisor renewal does not advance the current generation",
                )
            self.lease = lease

    @property
    def shutdown_evidence(self) -> ShutdownEvidence | None:
        return self.process_supervisor.last_shutdown

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            with self._lease_lock:
                lease = self.lease
            valid = lease.is_valid()
            if valid and self.lease_check is not None:
                try:
                    checked = self.lease_check(lease)
                    valid = checked is not False
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
            if self.on_lost is not None:
                try:
                    self.on_lost(stopped)
                except Exception:
                    pass
            return


def verify_password_manager_policy(
    policy_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Verify the managed Chromium policy without reading credential values."""

    path = Path(
        policy_path
        or os.environ.get(
            "CHROMIUM_POLICY_FILE", "/etc/chromium/policies/managed/opencli-account-runtime.json"
        )
    )
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BrowserRuntimeError(
            "credential_policy_unverified", "managed Chromium policy is unavailable"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrowserRuntimeError(
            "credential_policy_unverified", "managed Chromium policy is unavailable"
        ) from exc
    if not isinstance(payload, dict) or payload.get("PasswordManagerEnabled") is not False:
        raise BrowserRuntimeError(
            "credential_policy_unverified", "password manager policy is not disabled"
        )
    for key in ("AutofillAddressEnabled", "AutofillCreditCardEnabled"):
        if key in payload and payload[key] is not False:
            raise BrowserRuntimeError(
                "credential_policy_unverified", "autofill policy is not disabled"
            )
    return {"policy": "verified", "password_manager_enabled": False, "source": str(path)}


def inspect_password_inventory(profile_dir: str | os.PathLike[str]) -> PasswordInventory:
    """Count credential rows with a complete, bounded store enumeration.

    Only allowlisted Chromium password-store filenames are opened.  The
    enumeration records every matching database and sidecar path so a
    snapshot manifest can authorize the exact set rather than silently
    dropping a nested or account-specific ``Login Data`` store.
    """

    profile = Path(profile_dir)
    if (
        not profile.is_absolute()
        or profile == Path("/")
        or profile.is_symlink()
        or not profile.is_dir()
    ):
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
            raise BrowserRuntimeError(
                "password_inventory_blocked", "password metadata cannot be verified"
            ) from exc
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
        raise BrowserRuntimeError(
            "password_inventory_blocked", "profile contains unverifiable credentials"
        )

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
        checksums_payload = json.dumps(checksums, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
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
        _atomic_write_bytes(
            staging / "complete.marker", f"sha256:{checksum_digest}\n".encode("ascii")
        )
        _fsync_tree(staging)
        # Serialize the final version/pointer decision.  Two stopped writers
        # may copy concurrently, but only one can claim each deterministic
        # version and advance current; the loser receives a fresh version.
        with _exclusive_file_lock(paths.snapshot_lock_file):
            version = _next_snapshot_version(paths.snapshots_dir)
            manifest["version"] = version
            _atomic_write_bytes(
                staging / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
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
                raise BrowserRuntimeError(
                    "profile_manifest_invalid", "current snapshot pointer is invalid"
                )
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
        raise BrowserRuntimeError(
            "profile_manifest_invalid", "snapshot metadata is incomplete"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or not isinstance(checksums, list)
        or not marker.startswith("sha256:")
    ):
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot metadata shape is invalid")
    if (
        manifest.get("runtime_version") != expected_runtime_version
        or expected_runtime_version != _RUNTIME_VERSION
    ):
        raise BrowserRuntimeError(
            "profile_manifest_incompatible", "snapshot runtime version is incompatible"
        )
    expected_identity = {
        "workspace_id": expected_workspace_id,
        "account_id": expected_account_id,
        "profile_id": expected_profile_id,
        "node_id": expected_node_id,
    }
    for field, expected in expected_identity.items():
        if expected is not None and manifest.get(field) != expected:
            raise BrowserRuntimeError(
                "profile_identity_mismatch", "snapshot identity does not match account"
            )
    if expected_bundle is not None and manifest.get("bundle") != expected_bundle:
        raise BrowserRuntimeError(
            "profile_manifest_incompatible", "snapshot bundle is incompatible"
        )
    if (
        expected_browser_version is not None
        and manifest.get("browser_version") != expected_browser_version
    ):
        raise BrowserRuntimeError(
            "profile_manifest_incompatible", "snapshot browser version is incompatible"
        )
    checksums_payload = json.dumps(checksums, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if marker != f"sha256:{hashlib.sha256(checksums_payload).hexdigest()}":
        raise BrowserRuntimeError(
            "profile_manifest_invalid", "snapshot marker does not match checksums"
        )
    if manifest.get("password_inventory_status") not in _ALLOWED_SNAPSHOT_PASSWORD_STATES:
        raise BrowserRuntimeError(
            "password_inventory_blocked", "snapshot credential metadata is not safe"
        )
    data_dir = snapshot_dir / "profile"
    total_bytes = 0
    for item in checksums:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BrowserRuntimeError(
                "profile_manifest_invalid", "snapshot checksum entry is invalid"
            )
        relative = Path(item["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not isinstance(item.get("sha256"), str)
        ):
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
    if (
        state.get("state") in {"running", "quarantined", "recovery_required"}
        or paths.dirty_marker.exists()
    ):
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
            old_dir = (
                paths.profile_dir.parent / f".{paths.profile_dir.name}.dirty-{uuid.uuid4().hex}"
            )
            os.replace(paths.profile_dir, old_dir)
        os.replace(staging, paths.profile_dir)
        paths.write_state(
            "stopped",
            manifest_ref=f"snapshots/{snapshot_dir.name}/manifest.json",
            reason="restored",
        )
        return manifest
    except Exception:
        if (
            paths.profile_dir.exists()
            and paths.profile_dir.is_dir()
            and not any(paths.profile_dir.iterdir())
        ):
            paths.profile_dir.rmdir()
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_id(value: str, field: str, *, max_length: int = 128) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or not _SAFE_COMPONENT.fullmatch(value)
    ):
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
        raise BrowserRuntimeError(
            "epoch_lock_unavailable", "epoch fencing lock cannot be opened"
        ) from exc
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
                raise BrowserRuntimeError(
                    "epoch_lock_unavailable", "epoch fencing lock cannot be acquired"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise BrowserRuntimeError(
                    "epoch_lock_unavailable", "epoch fencing lock cannot be acquired"
                ) from exc
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
            raise BrowserRuntimeError(
                "path_permissions", "runtime path permissions cannot be restricted"
            ) from exc


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
        raise BrowserRuntimeError(
            "password_inventory_blocked", "password metadata cannot be enumerated"
        )

    try:
        walker = os.walk(root, topdown=True, followlinks=False, onerror=onerror)
        for current, dirnames, filenames in walker:
            directories_examined += 1
            if directories_examined + files_examined > _MAX_PASSWORD_SCAN_ENTRIES:
                raise BrowserRuntimeError(
                    "password_inventory_unbounded", "password store enumeration exceeded its bound"
                )
            for dirname in sorted(dirnames):
                directory = Path(current) / dirname
                if directory.is_symlink():
                    raise BrowserRuntimeError("profile_symlink", "profile contains a symlink")
            for filename in sorted(filenames):
                files_examined += 1
                if directories_examined + files_examined > _MAX_PASSWORD_SCAN_ENTRIES:
                    raise BrowserRuntimeError(
                        "password_inventory_unbounded",
                        "password store enumeration exceeded its bound",
                    )
                path = Path(current) / filename
                if path.is_symlink():
                    raise BrowserRuntimeError("profile_symlink", "profile contains a symlink")
                if (
                    filename not in _PASSWORD_DATABASE_NAMES
                    and filename not in _PASSWORD_DATABASE_SIDECARS
                ):
                    continue
                if not path.is_file():
                    raise BrowserRuntimeError(
                        "password_inventory_blocked", "password store is not a regular file"
                    )
                stores.append(path)
    except BrowserRuntimeError:
        raise
    except OSError as exc:
        raise BrowserRuntimeError(
            "password_inventory_blocked", "password metadata cannot be enumerated"
        ) from exc
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
        raise BrowserRuntimeError(
            "password_inventory_changed", "password store changed during snapshot"
        )


def _validate_manifest_password_inventory(
    manifest: Mapping[str, Any], inventory: PasswordInventory
) -> None:
    """Require snapshot metadata to authorize the exact observed store set."""

    listed = manifest.get("password_databases")
    inventory_metadata = manifest.get("password_inventory")
    if not isinstance(listed, list) or not isinstance(inventory_metadata, dict):
        raise BrowserRuntimeError(
            "password_manifest_unverified", "password store authorization is missing"
        )
    if inventory_metadata.get("enumeration_complete") is not True:
        raise BrowserRuntimeError(
            "password_inventory_blocked", "password store enumeration was incomplete"
        )
    if (
        manifest.get("password_inventory_status") != inventory.status
        or manifest.get("password_credential_count") != inventory.credential_count
    ):
        raise BrowserRuntimeError(
            "password_manifest_mismatch", "password metadata does not match the store"
        )
    expected_paths = list(inventory.database_paths)
    listed_paths: list[str] = []
    normalized_entries: list[dict[str, Any]] = []
    for entry in listed:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("role"), str)
        ):
            raise BrowserRuntimeError(
                "password_manifest_unverified", "password store authorization is malformed"
            )
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != entry["path"]:
            raise BrowserRuntimeError(
                "password_manifest_unverified", "password store path is invalid"
            )
        name = relative.name
        if name not in _PASSWORD_DATABASE_NAMES and name not in _PASSWORD_DATABASE_SIDECARS:
            raise BrowserRuntimeError(
                "password_manifest_unverified", "password store is not allowlisted"
            )
        role = "database" if name in _PASSWORD_DATABASE_NAMES else "sidecar"
        if (
            entry["role"] != role
            or not isinstance(entry.get("credential_count"), int)
            or entry["credential_count"] < 0
        ):
            raise BrowserRuntimeError(
                "password_manifest_unverified", "password store metadata is invalid"
            )
        listed_paths.append(entry["path"])
        normalized_entries.append(
            {"path": entry["path"], "role": role, "credential_count": entry["credential_count"]}
        )
    if listed_paths != expected_paths or len(set(listed_paths)) != len(listed_paths):
        raise BrowserRuntimeError(
            "password_database_unlisted", "snapshot contains an unlisted password store"
        )
    if normalized_entries != [dict(entry) for entry in inventory.database_entries]:
        raise BrowserRuntimeError(
            "password_manifest_mismatch", "password store metadata does not match the store"
        )
    if (
        inventory_metadata.get("database_count") != inventory.database_count
        or inventory_metadata.get("database_paths") != expected_paths
    ):
        raise BrowserRuntimeError(
            "password_manifest_mismatch", "password inventory evidence does not match the store"
        )


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
            raise BrowserRuntimeError(
                "profile_file_invalid", "snapshot contains a non-regular file"
            )


def _next_snapshot_version(root: Path) -> int:
    versions = []
    for path in root.glob("v*"):
        if path.is_symlink():
            raise BrowserRuntimeError(
                "profile_manifest_invalid", "snapshot version path is a symlink"
            )
        if path.is_dir() and re.fullmatch(r"v[1-9][0-9]*", path.name):
            versions.append(int(path.name[1:]))
    return max(versions, default=0) + 1


def _select_snapshot_dir(root: Path, *, snapshot_version: int | None = None) -> Path:
    """Select one version only after rejecting malformed/ambiguous candidates."""

    candidates: dict[int, tuple[int, Path]] = {}
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise BrowserRuntimeError(
            "profile_manifest_invalid", "snapshot versions cannot be enumerated"
        ) from exc
    for child in children:
        if child.is_symlink() and child.name.startswith("v"):
            raise BrowserRuntimeError(
                "profile_manifest_invalid", "snapshot version path is a symlink"
            )
        if not child.is_dir() or not child.name.startswith("v"):
            continue
        if not re.fullmatch(r"v[1-9][0-9]*", child.name):
            raise BrowserRuntimeError(
                "profile_manifest_invalid", "snapshot version path is invalid"
            )
        directory_version = int(child.name[1:])
        try:
            manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise BrowserRuntimeError(
                "profile_manifest_invalid", "snapshot manifest cannot be read"
            ) from exc
        manifest_version = manifest.get("version") if isinstance(manifest, dict) else None
        if (
            type(manifest_version) is not int
            or manifest_version < 1
            or manifest_version != directory_version
        ):
            raise BrowserRuntimeError(
                "profile_manifest_invalid", "snapshot version is inconsistent"
            )
        if manifest_version in candidates:
            raise BrowserRuntimeError(
                "profile_version_ambiguous", "multiple snapshots claim the same version"
            )
        candidates[manifest_version] = (directory_version, child)
    if snapshot_version is not None:
        if type(snapshot_version) is not int or snapshot_version < 1:
            raise BrowserRuntimeError(
                "profile_version_invalid", "requested snapshot version is invalid"
            )
        selected = candidates.get(snapshot_version)
        if selected is None:
            raise BrowserRuntimeError(
                "profile_version_missing", "requested snapshot version does not exist"
            )
        return selected[1]
    pointer = _read_pointer(root / "current")
    if pointer is None:
        if len(candidates) > 1:
            raise BrowserRuntimeError(
                "profile_version_ambiguous", "snapshot selection has no current version"
            )
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
        raise BrowserRuntimeError(
            "profile_manifest_invalid", "snapshot pointer cannot be read"
        ) from exc
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
        expected_endpoints = {
            "cdp_endpoint": f"http://127.0.0.1:{self.cdp_port}",
            "bbx_remote": f"http://127.0.0.1:{self.bbx_port}",
            "daemon_endpoint": f"http://127.0.0.1:{self.daemon_port}",
        }
        for name, expected in expected_endpoints.items():
            if getattr(self, name).rstrip("/") != expected:
                raise BrowserRuntimeError(
                    "runtime_endpoint_invalid",
                    f"{name} does not match session port",
                )

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
                raise BrowserRuntimeError(
                    "stale_runtime_binding", "session runtime generation is already fenced"
                )
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
            raise BrowserRuntimeError(
                "capability_missing", "session runtime binding is unavailable"
            )
        if binding.node_id != node_id or binding.boot_id != boot_id or binding.epoch != epoch:
            raise BrowserRuntimeError(
                "stale_runtime_binding", "session runtime generation is no longer current"
            )
        binding.validate()
        return binding

    def revoke(self, *, session_id: str, node_id: str, boot_id: str, epoch: int) -> None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            if binding.node_id != node_id or binding.boot_id != boot_id or binding.epoch != epoch:
                raise BrowserRuntimeError(
                    "stale_runtime_binding", "cannot revoke a newer runtime generation"
                )
            self._bindings.pop(session_id, None)


@dataclass(frozen=True)
class PortalRuntimeRegistration:
    """Edge-owned live portal facts; never serialized into a durable command."""

    session_id: str
    node_id: str
    boot_id: str
    epoch: int
    agent_url: str
    owner_endpoint: str
    tunnel_handle: str
    tunnel_auth_digest: str
    record_session: Any

    def validate(self) -> None:
        for field_name, value in (
            ("session_id", self.session_id),
            ("node_id", self.node_id),
            ("boot_id", self.boot_id),
            ("tunnel_handle", self.tunnel_handle),
        ):
            _validate_id(value, field_name, max_length=255)
        if self.epoch < 0:
            raise BrowserRuntimeError("portal_registration_invalid", "portal generation is invalid")
        if not self.agent_url.startswith(("https://", "wss://")):
            raise BrowserRuntimeError(
                "portal_registration_invalid", "portal agent URL must use TLS"
            )
        if not self.owner_endpoint.startswith("https://"):
            raise BrowserRuntimeError(
                "portal_registration_invalid", "portal owner endpoint must use TLS"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.tunnel_auth_digest):
            raise BrowserRuntimeError(
                "portal_registration_invalid", "portal tunnel digest is invalid"
            )
        if (
            getattr(self.record_session, "session_id", None) is None
            or getattr(self.record_session, "page", None) is None
        ):
            raise BrowserRuntimeError(
                "portal_record_missing", "portal record session is not a live page record"
            )


class SessionPortalRegistry:
    """Registers actual edge RecordSessions for one fenced runtime generation."""

    def __init__(self) -> None:
        self._registrations: dict[str, PortalRuntimeRegistration] = {}
        self._lock = threading.RLock()

    def register(self, registration: PortalRuntimeRegistration) -> None:
        registration.validate()
        with self._lock:
            existing = self._registrations.get(registration.session_id)
            if existing is not None and existing != registration:
                raise BrowserRuntimeError(
                    "stale_portal_registration",
                    "portal session already has a different live registration",
                )
            self._registrations[registration.session_id] = registration

    def resolve(
        self,
        *,
        session_id: str,
        node_id: str,
        boot_id: str,
        epoch: int,
        agent_url: str,
    ) -> PortalRuntimeRegistration:
        with self._lock:
            registration = self._registrations.get(session_id)
        if registration is None:
            raise BrowserRuntimeError(
                "portal_record_missing", "portal record session is unavailable"
            )
        if (
            registration.node_id != node_id
            or registration.boot_id != boot_id
            or registration.epoch != epoch
            or registration.agent_url != agent_url
        ):
            raise BrowserRuntimeError(
                "stale_portal_registration",
                "portal registration does not match the current runtime generation",
            )
        registration.validate()
        return registration

    def revoke(self, *, session_id: str, node_id: str, boot_id: str, epoch: int) -> None:
        with self._lock:
            registration = self._registrations.get(session_id)
            if registration is None:
                return
            if (
                registration.node_id != node_id
                or registration.boot_id != boot_id
                or registration.epoch != epoch
            ):
                raise BrowserRuntimeError(
                    "stale_portal_registration",
                    "cannot revoke a newer portal registration",
                )
            self._registrations.pop(session_id, None)


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

    def claim(
        self, *, claim: Any, session: Any, node_identity: Any, now: datetime | None = None
    ) -> RuntimeLeaseAdmission:
        current = now or _now()
        if node_identity.node_id != claim.node_id or node_identity.boot_id != claim.boot_id:
            raise BrowserRuntimeError(
                "node_identity_mismatch", "claim identity does not match authenticated node"
            )
        if current >= claim.expires_at:
            raise BrowserRuntimeError("claim_expired", "claim deadline has passed")
        if session.session_id != claim.session_id or session.node_id != claim.node_id:
            raise BrowserRuntimeError("session_claim_mismatch", "session is not owned by claim")
        if session.node_boot_id != claim.boot_id or session.epoch != claim.epoch:
            raise BrowserRuntimeError(
                "session_claim_mismatch", "session generation is not owned by claim"
            )
        admission = RuntimeLeaseAdmission(claim, session, node_identity)
        with self._lock:
            existing = self._claims.get(claim.command_id)
            if existing is not None and existing.claim != claim:
                raise BrowserRuntimeError(
                    "duplicate_claim", "command is already claimed by another generation"
                )
            self._claims[claim.command_id] = admission
        return admission

    def renew(
        self, *, claim: Any, node_identity: Any, now: datetime | None = None
    ) -> RuntimeLeaseAdmission:
        current = now or _now()
        if node_identity.node_id != claim.node_id or node_identity.boot_id != claim.boot_id:
            raise BrowserRuntimeError(
                "node_identity_mismatch", "renewal identity does not match authenticated node"
            )
        with self._lock:
            admission = self._claims.get(claim.command_id)
            if admission is None:
                raise BrowserRuntimeError("stale_claim", "renewal has no admitted claim")
            previous = admission.claim
            lineage = (
                "workspace_id",
                "account_id",
                "command_id",
                "session_id",
                "node_id",
                "boot_id",
                "epoch",
                "expected_revision",
                "claimed_at",
            )
            if any(getattr(previous, field) != getattr(claim, field) for field in lineage):
                raise BrowserRuntimeError("stale_claim", "renewal changes admitted claim lineage")
            if claim.expires_at < previous.expires_at:
                raise BrowserRuntimeError("stale_claim", "renewal shortens the admitted claim")
            if current >= claim.expires_at:
                raise BrowserRuntimeError("claim_expired", "claim deadline has passed")
            updated = RuntimeLeaseAdmission(
                claim,
                admission.session,
                admission.node_identity,
                admission.result,
            )
            self._claims[claim.command_id] = updated
            return updated

    def result(
        self, *, result: Any, node_identity: Any, now: datetime | None = None
    ) -> RuntimeLeaseAdmission:
        if node_identity.node_id != result.node_id or node_identity.boot_id != result.boot_id:
            raise BrowserRuntimeError(
                "node_identity_mismatch", "result identity does not match authenticated node"
            )
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
                raise BrowserRuntimeError(
                    "result_claim_mismatch", "result does not match its claim"
                )
            updated = RuntimeLeaseAdmission(
                admission.claim, admission.session, admission.node_identity, result
            )
            self._claims[result.command_id] = updated
            return updated

    def revoke(self, command_id: str) -> None:
        with self._lock:
            self._claims.pop(command_id, None)


@dataclass(frozen=True)
class AccountRuntimeConfiguration:
    """Trusted deployment inputs for isolated account stacks.

    Every executable, filesystem root, bundle and port range comes from the
    node deployment.  No field is accepted from a durable command or user
    request.
    """

    runtime_root: Path
    profile_root: Path
    state_root: Path
    bundle_root: Path
    bundle_manifest: Path
    bundle_id: str
    policy_file: Path
    xvfb_argv: tuple[str, ...]
    browser_argv: tuple[str, ...]
    bbx_argv: tuple[str, ...]
    bbx_install_argv: tuple[str, ...]
    bbx_extension_id_file: Path
    daemon_argv: tuple[str, ...]
    display_min: int = 100
    display_max: int = 199
    port_min: int = 20_000
    port_max: int = 29_999
    startup_timeout: float = 30.0
    require_linux: bool = True

    @classmethod
    def from_environment(cls) -> AccountRuntimeConfiguration:
        """Load a single trusted bundle and fixed process toolchain."""

        # Compose mounts this existing volume; standalone defaults match the
        # image's writable directory. Honor the same parent as the entrypoint.
        persistent_root = Path(
            os.environ.get("RUNTIME_STATE_DIR", "/var/lib/opencli/account-runtime")
        ).expanduser()
        runtime_root = Path(
            os.environ.get("ACCOUNT_RUNTIME_ROOT", str(persistent_root / "sessions"))
        ).expanduser()
        profile_root = Path(
            os.environ.get("ACCOUNT_PROFILE_ROOT", str(persistent_root / "profiles"))
        ).expanduser()
        state_root = Path(
            os.environ.get("ACCOUNT_RUNTIME_STATE_ROOT", str(persistent_root / "state"))
        ).expanduser()
        bundle_root = Path(
            os.environ.get("BROWSER_RUNTIME_BUNDLE_ROOT", "/opt/browser-runtime-bundles")
        ).expanduser()
        bundle_manifest = Path(
            os.environ.get(
                "BROWSER_RUNTIME_BUNDLE_MANIFEST",
                str(bundle_root / "opencli-default" / "2" / "manifest.json"),
            )
        ).expanduser()
        policy_file = Path(
            os.environ.get(
                "CHROMIUM_POLICY_FILE",
                "/etc/chromium/policies/managed/opencli-account-runtime.json",
            )
        ).expanduser()
        browser_binary = os.environ.get("ACCOUNT_RUNTIME_BROWSER_BIN") or os.environ.get(
            "CHROMIUM_BINARY", "chromium"
        )
        daemon_script = os.environ.get("ACCOUNT_RUNTIME_OPENCLI_DAEMON_JS", "")
        daemon_argv: tuple[str, ...] = ()
        if daemon_script:
            daemon_argv = (
                os.environ.get("ACCOUNT_RUNTIME_NODE_BIN", "node"),
                daemon_script,
            )
        try:
            display_min = int(os.environ.get("ACCOUNT_RUNTIME_DISPLAY_MIN", "100"))
            display_max = int(os.environ.get("ACCOUNT_RUNTIME_DISPLAY_MAX", "199"))
            port_min = int(os.environ.get("ACCOUNT_RUNTIME_PORT_MIN", "20000"))
            port_max = int(os.environ.get("ACCOUNT_RUNTIME_PORT_MAX", "29999"))
            startup_timeout = float(os.environ.get("ACCOUNT_RUNTIME_STARTUP_TIMEOUT", "30"))
        except ValueError as exc:
            raise BrowserRuntimeError(
                "runtime_configuration_invalid",
                "account runtime numeric configuration is invalid",
            ) from exc
        return cls(
            runtime_root=runtime_root,
            profile_root=profile_root,
            state_root=state_root,
            bundle_root=bundle_root,
            bundle_manifest=bundle_manifest,
            bundle_id=os.environ.get("BROWSER_RUNTIME_BUNDLE_ID", "").strip(),
            policy_file=policy_file,
            xvfb_argv=(os.environ.get("ACCOUNT_RUNTIME_XVFB_BIN", "Xvfb"),),
            browser_argv=(browser_binary,),
            bbx_argv=(os.environ.get("ACCOUNT_RUNTIME_BBX_DAEMON_BIN", "bbx-daemon"),),
            bbx_install_argv=(os.environ.get("ACCOUNT_RUNTIME_BBX_BIN", "bbx"),),
            bbx_extension_id_file=Path(
                os.environ.get(
                    "ACCOUNT_RUNTIME_BBX_EXTENSION_ID_FILE",
                    "/etc/browser-bridge-extension-id",
                )
            ).expanduser(),
            daemon_argv=daemon_argv,
            display_min=display_min,
            display_max=display_max,
            port_min=port_min,
            port_max=port_max,
            startup_timeout=startup_timeout,
        )

    def validate(self) -> None:
        if self.require_linux and (os.name != "posix" or not sys.platform.startswith("linux")):
            raise BrowserRuntimeError(
                "runtime_platform_unsupported",
                "account runtime process stacks require Linux",
            )
        for root in (self.runtime_root, self.profile_root, self.state_root, self.bundle_root):
            if (
                not root.is_absolute()
                or root == Path("/")
                or root.is_symlink()
                or not root.is_dir()
            ):
                raise BrowserRuntimeError(
                    "runtime_configuration_invalid",
                    "account runtime roots must be absolute non-symlink directories",
                )
        if not 1 <= self.display_min <= self.display_max <= 65_535:
            raise BrowserRuntimeError(
                "runtime_configuration_invalid",
                "account runtime display range is invalid",
            )
        if not 1_024 <= self.port_min <= self.port_max <= 65_535:
            raise BrowserRuntimeError(
                "runtime_configuration_invalid",
                "account runtime port range is invalid",
            )
        if self.port_max - self.port_min < 1:
            raise BrowserRuntimeError(
                "runtime_configuration_invalid",
                "account runtime port range must contain at least two ports",
            )
        if not 0 < self.startup_timeout <= 120:
            raise BrowserRuntimeError(
                "runtime_configuration_invalid",
                "account runtime startup timeout is invalid",
            )
        if not self.bundle_id:
            raise BrowserRuntimeError(
                "runtime_bundle_unavailable",
                "BROWSER_RUNTIME_BUNDLE_ID is not configured",
            )
        if (
            not self.bundle_manifest.is_absolute()
            or self.bundle_manifest.is_symlink()
            or not self.bundle_manifest.is_file()
        ):
            raise BrowserRuntimeError(
                "runtime_bundle_unavailable",
                "trusted browser runtime manifest is unavailable",
            )
        for argv, label in (
            (self.xvfb_argv, "Xvfb"),
            (self.bbx_install_argv, "Browser Bridge installer"),
            (self.browser_argv, "Chromium"),
            (self.bbx_argv, "Browser Bridge"),
            (self.daemon_argv, "OpenCLI daemon"),
        ):
            if not argv:
                raise BrowserRuntimeError(
                    "runtime_binary_missing",
                    f"{label} command is not configured",
                )
            executable = argv[0]
            resolved = (
                executable
                if Path(executable).is_absolute() and Path(executable).is_file()
                else shutil.which(executable)
            )
            if resolved is None:
                raise BrowserRuntimeError(
                    "runtime_binary_missing",
                    f"{label} executable is unavailable",
                )
        if len(self.daemon_argv) < 2:
            raise BrowserRuntimeError(
                "runtime_binary_missing",
                "OpenCLI daemon script is not configured",
            )
        daemon_script = Path(self.daemon_argv[1])
        if (
            not daemon_script.is_absolute()
            or daemon_script.is_symlink()
            or not daemon_script.is_file()
        ):
            raise BrowserRuntimeError(
                "runtime_binary_missing",
                "OpenCLI daemon script is unavailable",
            )
        if (
            not self.bbx_extension_id_file.is_absolute()
            or self.bbx_extension_id_file.is_symlink()
            or not self.bbx_extension_id_file.is_file()
        ):
            raise BrowserRuntimeError(
                "runtime_binary_missing",
                "Browser Bridge extension identity is unavailable",
            )
        verify_password_manager_policy(self.policy_file)


@dataclass(frozen=True)
class RuntimeBundleAdmission:
    extension_dirs: tuple[Path, ...]
    login_url: str
    login_origin: str
    bundle_name: str
    bundle_version: str


@dataclass
class RunningAccountRuntime:
    """One live process stack and the facts needed to stop it safely."""

    binding: SessionRuntimeBinding
    paths: ProfileRuntimePaths
    profile_id: str
    bundle: RuntimeBundleAdmission
    browser_version: str
    process_supervisor: ProcessTreeSupervisor
    epoch_store: EpochStore
    lease_supervisor: LeaseSupervisor
    lease: EpochLease
    command_id: str
    startup_nonce: str
    workspace_id: str


class BrowserAccountRuntimeAllocator:
    """Allocate, supervise and release exactly one stack per fenced session."""

    def __init__(
        self,
        configuration: AccountRuntimeConfiguration,
        *,
        process_factory: Callable[..., Any] = spawn_isolated_process,
    ) -> None:
        self.configuration = configuration
        self.process_factory = process_factory
        self._sessions: dict[str, RunningAccountRuntime] = {}
        self._profiles: dict[str, str] = {}
        self._ports: set[int] = set()
        self._displays: set[int] = set()
        self._start_lock = asyncio.Lock()
        self._state_lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> BrowserAccountRuntimeAllocator:
        return cls(AccountRuntimeConfiguration.from_environment())

    def get(self, session_id: str) -> RunningAccountRuntime | None:
        with self._state_lock:
            return self._sessions.get(session_id)

    def active_count(self) -> int:
        with self._state_lock:
            return len(self._sessions)

    async def start(
        self,
        *,
        command: Any,
        claim: Any,
        session: Any,
        node_identity: Any,
    ) -> RunningAccountRuntime:
        """Start a claimed login stack, idempotently, before any login action."""

        async with self._start_lock:
            self._validate_start_contract(command, claim, session, node_identity)
            with self._state_lock:
                existing = self._sessions.get(session.session_id)
            if existing is not None:
                if (
                    existing.binding.node_id != claim.node_id
                    or existing.binding.boot_id != claim.boot_id
                    or existing.binding.epoch != claim.epoch
                ):
                    raise BrowserRuntimeError(
                        "stale_runtime_binding",
                        "session already has a different runtime generation",
                    )
                await self.renew(claim=claim, session=session, node_identity=node_identity)
                return existing

            self.configuration.validate()

            bundle = self._admit_bundle(session)
            profile_id = session.profile_id or uuid.uuid4().hex
            _validate_id(profile_id, "profile_id")
            profile_key = f"{session.workspace_id}/{profile_id}"
            with self._state_lock:
                owner = self._profiles.get(profile_key)
                if owner is not None and owner != session.session_id:
                    raise BrowserRuntimeError(
                        "profile_locked",
                        "account profile already has an active writer",
                    )
                self._profiles[profile_key] = session.session_id

            process_supervisor = ProcessTreeSupervisor()
            registered = False
            running: RunningAccountRuntime | None = None
            stack: StackIsolation | None = None
            paths: ProfileRuntimePaths | None = None
            try:
                stack = self._allocate_isolation(
                    session.session_id,
                    session.workspace_id,
                    profile_id,
                )
                paths = ProfileRuntimePaths.from_profile_dir(
                    stack.profile_dir,
                    self.configuration.state_root / session.workspace_id / profile_id,
                )
                restore_current_snapshot(
                    paths,
                    allow_empty=session.profile_state == "new",
                    expected_workspace_id=session.workspace_id,
                    expected_account_id=session.account_id,
                    expected_profile_id=session.profile_id,
                    expected_node_id=session.node_id,
                    snapshot_version=session.profile_version,
                    expected_bundle=f"{bundle.bundle_name}:{bundle.bundle_version}",
                )
                epoch_store = EpochStore(paths)
                epoch_store.begin_boot(session.node_boot_id)
                epoch_store.allocate(
                    requested_epoch=claim.epoch,
                    boot_id=claim.boot_id,
                    owner_id=session.lease_id,
                    node_id=claim.node_id,
                    lease_expires_at=claim.expires_at,
                )
                paths.mark_running(
                    workspace_id=session.workspace_id,
                    account_id=session.account_id,
                    profile_id=profile_id,
                    node_id=claim.node_id,
                    boot_id=claim.boot_id,
                    epoch=claim.epoch,
                    command_id=command.command_id,
                )
                environment = stack.environment()
                environment.update(
                    {
                        "RUNTIME_STATE_DIR": str(paths.state_dir),
                        "CHROMIUM_POLICY_FILE": str(self.configuration.policy_file),
                        "BROWSER_RUNTIME_BUNDLE_ROOT": str(self.configuration.bundle_root),
                        "BROWSER_RUNTIME_BUNDLE_MANIFEST": str(self.configuration.bundle_manifest),
                        "BBX_REMOTE": f"http://127.0.0.1:{stack.bbx_port}",
                        "OPENCLI_DAEMON_ENDPOINT": f"http://127.0.0.1:{stack.daemon_port}",
                        "OPENCLI_DAEMON_LISTEN": "127.0.0.1",
                    }
                )
                await asyncio.to_thread(
                    _install_bbx_native_host,
                    self.configuration,
                    environment,
                    stack.home_dir,
                )
                xvfb = self.process_factory(
                    (
                        *self.configuration.xvfb_argv,
                        stack.display,
                        "-screen",
                        "0",
                        "1280x900x24",
                        "-nolisten",
                        "tcp",
                    ),
                    isolation=stack,
                    env=environment,
                )
                process_supervisor.register(xvfb)
                bbx = self.process_factory(
                    self.configuration.bbx_argv,
                    isolation=stack,
                    env=environment,
                )
                process_supervisor.register(bbx)
                daemon = self.process_factory(
                    self.configuration.daemon_argv,
                    isolation=stack,
                    env=environment,
                )
                process_supervisor.register(daemon)
                browser_args = [
                    *self.configuration.browser_argv,
                    f"--remote-debugging-port={stack.cdp_port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--remote-allow-origins=*",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "--disable-save-password-bubble",
                    f"--user-data-dir={stack.profile_dir}",
                    "--profile-directory=Default",
                    "--window-size=1280,900",
                ]
                if bundle.extension_dirs:
                    extension_csv = ",".join(str(path) for path in bundle.extension_dirs)
                    browser_args.extend(
                        (
                            f"--disable-extensions-except={extension_csv}",
                            f"--load-extension={extension_csv}",
                        )
                    )
                else:
                    browser_args.append("--disable-extensions")
                browser_args.append(bundle.login_url)
                browser = self.process_factory(
                    tuple(browser_args),
                    isolation=stack,
                    env=environment,
                )
                process_supervisor.register(browser)
                browser_version = await self._wait_until_ready(
                    stack=stack,
                    processes=(xvfb, bbx, daemon, browser),
                )
                binding = SessionRuntimeBinding(
                    session_id=session.session_id,
                    node_id=claim.node_id,
                    boot_id=claim.boot_id,
                    epoch=claim.epoch,
                    cdp_endpoint=f"http://127.0.0.1:{stack.cdp_port}",
                    bbx_remote=f"http://127.0.0.1:{stack.bbx_port}",
                    daemon_endpoint=f"http://127.0.0.1:{stack.daemon_port}",
                    profile_dir=stack.profile_dir,
                    home_dir=stack.home_dir,
                    cache_dir=stack.cache_dir,
                    display=stack.display,
                    cdp_port=stack.cdp_port,
                    bbx_port=stack.bbx_port,
                    daemon_port=stack.daemon_port,
                )
                lease = EpochLease(
                    claim.node_id,
                    claim.boot_id,
                    claim.epoch,
                    claim.expires_at,
                    session.lease_id,
                )
                lease_supervisor = LeaseSupervisor(
                    lease,
                    process_supervisor,
                    paths,
                    lease_check=epoch_store.assert_lease_current,
                    on_lost=lambda confirmed: self._on_lease_lost(
                        session.session_id,
                        binding,
                        profile_key,
                        stack,
                        confirmed,
                    ),
                )
                running = RunningAccountRuntime(
                    binding=binding,
                    paths=paths,
                    profile_id=profile_id,
                    bundle=bundle,
                    browser_version=browser_version,
                    process_supervisor=process_supervisor,
                    epoch_store=epoch_store,
                    lease_supervisor=lease_supervisor,
                    lease=lease,
                    command_id=command.command_id,
                    startup_nonce=secrets.token_hex(16),
                    workspace_id=session.workspace_id,
                )
                session_runtime_registry().register(binding)
                registered = True
                with self._state_lock:
                    self._sessions[session.session_id] = running
                lease_supervisor.start()
                return running
            except BaseException:
                if registered and running is not None:
                    try:
                        session_runtime_registry().revoke(
                            session_id=session.session_id,
                            node_id=claim.node_id,
                            boot_id=claim.boot_id,
                            epoch=claim.epoch,
                        )
                    except BrowserRuntimeError:
                        pass
                stopped = await process_supervisor.stop_async(grace_seconds=2)
                if paths is not None:
                    try:
                        paths.mark_stopped(
                            clean=False,
                            reason=(
                                "runtime_start_failed"
                                if stopped
                                else "runtime_start_stop_unconfirmed"
                            ),
                        )
                    except BrowserRuntimeError:
                        pass
                with self._state_lock:
                    if stopped:
                        self._profiles.pop(profile_key, None)
                        if stack is not None:
                            self._release_isolation(stack)
                raise

    async def renew(self, *, claim: Any, session: Any, node_identity: Any) -> RunningAccountRuntime:
        if node_identity.node_id != claim.node_id or node_identity.boot_id != claim.boot_id:
            raise BrowserRuntimeError(
                "node_identity_mismatch",
                "runtime renewal identity does not own the claim",
            )
        with self._state_lock:
            running = self._sessions.get(session.session_id)
        if running is None:
            raise BrowserRuntimeError(
                "capability_missing",
                "session runtime binding is unavailable",
            )
        if (
            running.binding.node_id != claim.node_id
            or running.binding.boot_id != claim.boot_id
            or running.binding.epoch != claim.epoch
            or running.lease.owner_id != session.lease_id
        ):
            raise BrowserRuntimeError(
                "stale_runtime_binding",
                "runtime renewal does not match the live generation",
            )
        if claim.expires_at > running.lease.expires_at:
            renewed = running.epoch_store.renew(
                running.lease,
                expires_at=claim.expires_at,
            )
            running.lease = renewed
            running.lease_supervisor.update_lease(renewed)
        return running

    async def stop(
        self,
        *,
        session_id: str,
        node_id: str,
        boot_id: str,
        epoch: int,
        save: bool = False,
        workspace_id: str | None = None,
        account_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._state_lock:
            running = self._sessions.get(session_id)
        if running is None:
            raise BrowserRuntimeError(
                "capability_missing",
                "session runtime binding is unavailable",
            )
        if (
            running.binding.node_id != node_id
            or running.binding.boot_id != boot_id
            or running.binding.epoch != epoch
        ):
            raise BrowserRuntimeError(
                "stale_runtime_binding",
                "runtime stop does not match the live generation",
            )
        if save and (not workspace_id or not account_id or not command_id):
            raise BrowserRuntimeError(
                "runtime_save_invalid",
                "profile save requires authorized command identity",
            )
        running.lease_supervisor.close()
        if running.lease_supervisor.lost.is_set():
            evidence = running.lease_supervisor.shutdown_evidence
            stopped = evidence is not None and evidence.confirmed
        else:
            # Chromium only removes its singleton files on an orderly browser
            # shutdown.  Ask the owned browser to close before terminating the
            # remaining process groups; a failed request still falls through
            # to the existing confirmed-stop/quarantine path.
            await _request_browser_shutdown(running.binding.cdp_port)
            stopped = await running.process_supervisor.stop_async()
        if not stopped:
            running.paths.mark_stopped(
                clean=False,
                reason="runtime_stop_unconfirmed",
            )
            self._revoke_binding(running.binding)
            raise BrowserRuntimeError(
                "runtime_stop_unconfirmed",
                "runtime process tree could not be proven stopped",
            )
        running.paths.mark_stopped(clean=True, reason="runtime_stopped")
        manifest: dict[str, Any] | None = None
        if save:
            assert workspace_id is not None
            assert account_id is not None
            assert command_id is not None
            try:
                manifest = await asyncio.to_thread(
                    snapshot_profile,
                    running.paths,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    profile_id=running.profile_id,
                    node_id=node_id,
                    command_id=command_id,
                    writer_epoch=epoch,
                    bundle=(f"{running.bundle.bundle_name}:{running.bundle.bundle_version}"),
                    browser_version=running.browser_version,
                    policy_path=self.configuration.policy_file,
                )
            except BaseException:
                running.paths.mark_quarantined("profile_save_failed")
                self._revoke_binding(running.binding)
                try:
                    session_portal_registry().revoke(
                        session_id=running.binding.session_id,
                        node_id=running.binding.node_id,
                        boot_id=running.binding.boot_id,
                        epoch=running.binding.epoch,
                    )
                except BrowserRuntimeError:
                    pass
                profile_key = f"{running.workspace_id}/{running.profile_id}"
                with self._state_lock:
                    self._sessions.pop(session_id, None)
                    if self._profiles.get(profile_key) == session_id:
                        self._profiles.pop(profile_key, None)
                    self._release_isolation(self._stack_from_binding(running.binding))
                raise
        self._revoke_binding(running.binding)
        try:
            session_portal_registry().revoke(
                session_id=running.binding.session_id,
                node_id=running.binding.node_id,
                boot_id=running.binding.boot_id,
                epoch=running.binding.epoch,
            )
        except BrowserRuntimeError:
            pass
        profile_key = f"{running.workspace_id}/{running.profile_id}"
        with self._state_lock:
            self._sessions.pop(session_id, None)
            if self._profiles.get(profile_key) == session_id:
                self._profiles.pop(profile_key, None)
            self._release_isolation(self._stack_from_binding(running.binding))
        return manifest

    async def close_all(self) -> None:
        with self._state_lock:
            running = list(self._sessions.values())
        for item in running:
            try:
                await self.stop(
                    session_id=item.binding.session_id,
                    node_id=item.binding.node_id,
                    boot_id=item.binding.boot_id,
                    epoch=item.binding.epoch,
                )
            except BrowserRuntimeError:
                continue

    def _validate_start_contract(
        self,
        command: Any,
        claim: Any,
        session: Any,
        node_identity: Any,
    ) -> None:
        if getattr(getattr(command, "kind", None), "value", None) != "start_login":
            raise BrowserRuntimeError(
                "runtime_command_invalid",
                "allocator start requires START_LOGIN",
            )
        if not command.session_id or command.session_id != session.session_id:
            raise BrowserRuntimeError(
                "session_claim_mismatch",
                "start command session is invalid",
            )
        if node_identity.node_id != claim.node_id or node_identity.boot_id != claim.boot_id:
            raise BrowserRuntimeError(
                "node_identity_mismatch",
                "authenticated node does not own runtime claim",
            )
        if (
            command.command_id != claim.command_id
            or command.workspace_id != claim.workspace_id
            or command.account_id != claim.account_id
            or session.workspace_id != claim.workspace_id
            or session.account_id != claim.account_id
            or session.node_id != claim.node_id
            or session.node_boot_id != claim.boot_id
            or session.epoch != claim.epoch
            or command.epoch != claim.epoch
        ):
            raise BrowserRuntimeError(
                "session_claim_mismatch",
                "start command, claim and session are not the same generation",
            )
        if session.purpose != "login":
            raise BrowserRuntimeError(
                "runtime_command_invalid",
                "START_LOGIN requires a login session",
            )
        if _now() >= claim.expires_at:
            raise BrowserRuntimeError("claim_expired", "claim deadline has passed")

    def _admit_bundle(self, session: Any) -> RuntimeBundleAdmission:
        if session.runtime_bundle_id != self.configuration.bundle_id:
            raise BrowserRuntimeError(
                "runtime_bundle_unavailable",
                "authorized runtime bundle is not installed on this node",
            )
        manifest_path = self.configuration.bundle_manifest
        root = self.configuration.bundle_root.resolve()
        try:
            resolved_manifest = manifest_path.resolve(strict=True)
        except OSError as exc:
            raise BrowserRuntimeError(
                "runtime_bundle_unavailable",
                "runtime bundle manifest is unavailable",
            ) from exc
        if manifest_path.is_symlink() or root not in resolved_manifest.parents:
            raise BrowserRuntimeError(
                "runtime_bundle_unavailable",
                "runtime bundle manifest escapes its trusted root",
            )
        try:
            manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrowserRuntimeError(
                "runtime_bundle_unavailable",
                "runtime bundle manifest is invalid",
            ) from exc
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("name"), str)
            or manifest.get("version") != session.runtime_bundle_version
            or not isinstance(manifest.get("components"), list)
        ):
            raise BrowserRuntimeError(
                "runtime_bundle_incompatible",
                "installed runtime bundle does not match the session",
            )
        extension_dirs: list[Path] = []
        script_host: Path | None = None
        for component in manifest["components"]:
            if not isinstance(component, dict) or component.get("kind") != "extension":
                continue
            relative = component.get("path")
            if not isinstance(relative, str):
                raise BrowserRuntimeError(
                    "runtime_bundle_unavailable",
                    "runtime bundle component path is invalid",
                )
            path = (resolved_manifest.parent / relative).resolve()
            if (
                resolved_manifest.parent not in path.parents
                or path.is_symlink()
                or not path.is_dir()
            ):
                raise BrowserRuntimeError(
                    "runtime_bundle_unavailable",
                    "runtime bundle component is unavailable",
                )
            extension_dirs.append(path)
            if component.get("id") == "opencli-script-host":
                script_host = path
        if script_host is None:
            raise BrowserRuntimeError(
                "runtime_bundle_incompatible",
                "runtime bundle has no Script Host",
            )
        rules_path = script_host / "packs" / "account-login" / "rules.json"
        if rules_path.is_symlink() or not rules_path.is_file():
            raise BrowserRuntimeError(
                "login_rule_unknown",
                "installed bundle has no verified login rule",
            )
        try:
            rule = json.loads(rules_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrowserRuntimeError(
                "login_rule_unknown",
                "installed login rule is invalid",
            ) from exc
        if (
            not isinstance(rule, dict)
            or rule.get("id") != session.login_rule_id
            or rule.get("version") != session.login_rule_version
        ):
            raise BrowserRuntimeError(
                "login_rule_unknown",
                "authorized login rule is not installed",
            )
        origins = rule.get("allowed_origins")
        login_url_value = rule.get("login_url")
        if (
            not isinstance(origins, list)
            or not origins
            or not all(isinstance(item, str) for item in origins)
            or not isinstance(login_url_value, str)
        ):
            raise BrowserRuntimeError(
                "login_rule_unknown",
                "installed login rule has no trusted login target",
            )
        parsed_origin = urlparse(origins[0])
        if (
            parsed_origin.scheme not in {"http", "https"}
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
            or (
                parsed_origin.scheme == "http"
                and parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            raise BrowserRuntimeError(
                "login_rule_unknown",
                "login rule origin is not an approved TLS or loopback origin",
            )
        login_url = urljoin(origins[0].rstrip("/") + "/", login_url_value)
        if (
            urlparse(login_url).scheme != parsed_origin.scheme
            or urlparse(login_url).netloc != parsed_origin.netloc
        ):
            raise BrowserRuntimeError(
                "login_rule_unknown",
                "login URL escapes the approved origin",
            )
        return RuntimeBundleAdmission(
            tuple(extension_dirs),
            login_url,
            f"{parsed_origin.scheme}://{parsed_origin.netloc}",
            manifest["name"],
            manifest["version"],
        )

    def _allocate_isolation(
        self,
        session_id: str,
        workspace_id: str,
        profile_id: str,
    ) -> StackIsolation:
        for value, name in (
            (session_id, "session_id"),
            (workspace_id, "workspace_id"),
            (profile_id, "profile_id"),
        ):
            _validate_id(value, name)
        with self._state_lock:
            display_number = next(
                (
                    candidate
                    for candidate in range(
                        self.configuration.display_min,
                        self.configuration.display_max + 1,
                    )
                    if candidate not in self._displays
                    and not Path(f"/tmp/.X{candidate}-lock").exists()
                    and not Path(f"/tmp/.X11-unix/X{candidate}").exists()
                ),
                None,
            )
            if display_number is None:
                raise BrowserRuntimeError(
                    "capacity_missing",
                    "no isolated X display is available",
                )
            if _OPENCLI_DAEMON_PORT in self._ports or not _local_port_available(
                _OPENCLI_DAEMON_PORT
            ):
                raise BrowserRuntimeError(
                    "capacity_missing",
                    "the fixed OpenCLI daemon transport is already in use on this node",
                )
            ports: list[int] = []
            for candidate in range(
                self.configuration.port_min,
                self.configuration.port_max + 1,
            ):
                if (
                    candidate == _OPENCLI_DAEMON_PORT
                    or candidate in self._ports
                    or not _local_port_available(candidate)
                ):
                    continue
                ports.append(candidate)
                if len(ports) == 2:
                    break
            if len(ports) != 2:
                raise BrowserRuntimeError(
                    "capacity_missing",
                    "two isolated runtime ports are unavailable",
                )
            self._displays.add(display_number)
            self._ports.update((*ports, _OPENCLI_DAEMON_PORT))
        try:
            session_root = self.configuration.runtime_root / workspace_id / session_id
            home_dir = session_root / "home"
            cache_dir = session_root / "cache"
            profile_dir = self.configuration.profile_root / workspace_id / profile_id / "profile"
            for path in (session_root, home_dir, cache_dir, profile_dir):
                _ensure_private_directory(path)
            stack = StackIsolation(
                display=f":{display_number}",
                cdp_port=ports[0],
                bbx_port=ports[1],
                daemon_port=_OPENCLI_DAEMON_PORT,
                home_dir=home_dir,
                cache_dir=cache_dir,
                profile_dir=profile_dir,
            )
            stack.validate()
            return stack
        except BaseException:
            with self._state_lock:
                self._displays.discard(display_number)
                self._ports.difference_update((*ports, _OPENCLI_DAEMON_PORT))
            raise

    async def _wait_until_ready(
        self,
        *,
        stack: StackIsolation,
        processes: Sequence[Any],
    ) -> str:
        deadline = asyncio.get_running_loop().time() + self.configuration.startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if any(_process_returncode(process) is not None for process in processes):
                raise BrowserRuntimeError(
                    "runtime_process_exited",
                    "account runtime process exited before readiness",
                )
            display_ready = (
                os.name != "posix" or Path(f"/tmp/.X11-unix/X{stack.display[1:]}").exists()
            )
            if (
                display_ready
                and _local_port_accepting(stack.bbx_port)
                and _local_port_accepting(stack.daemon_port)
            ):
                version = await asyncio.to_thread(_read_cdp_browser_version, stack.cdp_port)
                if version is not None:
                    return version
            await asyncio.sleep(0.05)
        raise BrowserRuntimeError(
            "runtime_readiness_timeout",
            "account runtime did not become ready before the trusted deadline",
        )

    def _on_lease_lost(
        self,
        session_id: str,
        binding: SessionRuntimeBinding,
        profile_key: str,
        stack: StackIsolation,
        confirmed: bool,
    ) -> None:
        self._revoke_binding(binding)
        try:
            session_portal_registry().revoke(
                session_id=binding.session_id,
                node_id=binding.node_id,
                boot_id=binding.boot_id,
                epoch=binding.epoch,
            )
        except BrowserRuntimeError:
            pass
        with self._state_lock:
            if confirmed:
                self._sessions.pop(session_id, None)
                if self._profiles.get(profile_key) == session_id:
                    self._profiles.pop(profile_key, None)
                self._release_isolation(stack)

    @staticmethod
    def _revoke_binding(binding: SessionRuntimeBinding) -> None:
        try:
            session_runtime_registry().revoke(
                session_id=binding.session_id,
                node_id=binding.node_id,
                boot_id=binding.boot_id,
                epoch=binding.epoch,
            )
        except BrowserRuntimeError:
            pass

    @staticmethod
    def _stack_from_binding(binding: SessionRuntimeBinding) -> StackIsolation:
        return StackIsolation(
            display=binding.display,
            cdp_port=binding.cdp_port,
            bbx_port=binding.bbx_port,
            daemon_port=binding.daemon_port,
            home_dir=binding.home_dir,
            cache_dir=binding.cache_dir,
            profile_dir=binding.profile_dir,
        )

    def _release_isolation(self, stack: StackIsolation) -> None:
        self._displays.discard(int(stack.display[1:]))
        self._ports.difference_update({stack.cdp_port, stack.bbx_port, stack.daemon_port})


def _install_bbx_native_host(
    configuration: AccountRuntimeConfiguration,
    environment: Mapping[str, str],
    home_dir: Path,
) -> None:
    try:
        extension_id = configuration.bbx_extension_id_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BrowserRuntimeError(
            "runtime_binary_missing",
            "Browser Bridge extension identity cannot be read",
        ) from exc
    if re.fullmatch(r"[a-p]{32}", extension_id) is None:
        raise BrowserRuntimeError(
            "runtime_bundle_incompatible",
            "Browser Bridge extension identity is invalid",
        )
    try:
        result = subprocess.run(
            [
                *configuration.bbx_install_argv,
                "install",
                extension_id,
                "--browser",
                "chromium",
            ],
            check=False,
            cwd=home_dir,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowserRuntimeError(
            "runtime_binary_missing",
            "Browser Bridge native host installation failed",
        ) from exc
    if result.returncode != 0:
        raise BrowserRuntimeError(
            "runtime_binary_missing",
            "Browser Bridge native host installation failed",
        )


def _process_returncode(process: Any) -> int | None:
    poll = getattr(process, "poll", None)
    return poll() if callable(poll) else getattr(process, "returncode", None)


def _local_port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _local_port_accepting(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.05)
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def _read_cdp_browser_version(port: int) -> str | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.1) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read(65_536))
    except (OSError, ValueError):
        return None
    browser = payload.get("Browser") if isinstance(payload, dict) else None
    return browser if isinstance(browser, str) and 0 < len(browser) <= 100 else None


def _read_cdp_browser_websocket_url(port: int) -> str | None:
    if not 1 <= port <= 65_535:
        return None
    version_url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urlopen(version_url, timeout=1.0) as response:
            if response.status != 200 or response.geturl() != version_url:
                return None
            payload = json.loads(response.read(65_536))
    except (OSError, ValueError):
        return None
    websocket_url = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
    if not isinstance(websocket_url, str):
        return None
    try:
        parsed = urlparse(websocket_url)
        websocket_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "ws"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or websocket_port != port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/devtools/browser/")
        or parsed.path == "/devtools/browser/"
    ):
        return None
    # Never retain authority supplied by the discovery document.  Chromium is
    # owned only through the allocator's exact IPv4 loopback binding.
    return f"ws://127.0.0.1:{port}{parsed.path}"


async def _request_browser_shutdown(port: int, *, timeout_seconds: float = 3.0) -> bool:
    """Request Chromium's orderly shutdown within a bounded grace period."""

    if timeout_seconds <= 0:
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    try:
        websocket_url = await asyncio.wait_for(
            asyncio.to_thread(_read_cdp_browser_websocket_url, port),
            timeout=min(1.0, timeout_seconds),
        )
    except TimeoutError:
        return False
    if websocket_url is None:
        return False

    try:
        import websockets
        from websockets.exceptions import WebSocketException
    except ImportError:
        return False

    request_sent = False
    try:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        async with asyncio.timeout(remaining):
            async with websockets.connect(
                websocket_url,
                open_timeout=remaining,
                close_timeout=min(1.0, remaining),
                ping_interval=None,
                max_size=65_536,
                proxy=None,
            ) as websocket:
                peer = websocket.remote_address
                if (
                    not isinstance(peer, tuple)
                    or len(peer) < 2
                    or peer[0] != "127.0.0.1"
                    or peer[1] != port
                ):
                    return False
                await websocket.send(json.dumps({"id": 1, "method": "Browser.close"}))
                request_sent = True
                # A successful Browser.close normally closes the connection.
                # If Chromium replies but remains up, retain the rest of the
                # bounded grace period before the supervisor sends SIGTERM.
                while True:
                    await websocket.recv()
    except (TimeoutError, EOFError, OSError, WebSocketException):
        return request_sent


_ACCOUNT_RUNTIME_ALLOCATOR: BrowserAccountRuntimeAllocator | None = None


def account_runtime_allocator() -> BrowserAccountRuntimeAllocator:
    global _ACCOUNT_RUNTIME_ALLOCATOR
    if _ACCOUNT_RUNTIME_ALLOCATOR is None:
        _ACCOUNT_RUNTIME_ALLOCATOR = BrowserAccountRuntimeAllocator.from_environment()
    return _ACCOUNT_RUNTIME_ALLOCATOR


async def close_account_runtime_allocator() -> None:
    allocator = _ACCOUNT_RUNTIME_ALLOCATOR
    if allocator is not None:
        await allocator.close_all()


def set_account_runtime_allocator(
    allocator: BrowserAccountRuntimeAllocator | None,
) -> None:
    """Install an explicit allocator for tests or a supervised node process."""

    global _ACCOUNT_RUNTIME_ALLOCATOR
    _ACCOUNT_RUNTIME_ALLOCATOR = allocator


_SESSION_RUNTIME_REGISTRY = SessionRuntimeRegistry()
_SESSION_PORTAL_REGISTRY = SessionPortalRegistry()
_RUNTIME_LEASE_BOOK = AuthenticatedRuntimeLeaseBook()


def session_runtime_registry() -> SessionRuntimeRegistry:
    return _SESSION_RUNTIME_REGISTRY


def session_portal_registry() -> SessionPortalRegistry:
    return _SESSION_PORTAL_REGISTRY


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
