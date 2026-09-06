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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote


_RUNTIME_VERSION = "1.0"
_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_POINTER_NAMES = {"current", "previous"}
_SINGLETON_NAMES = {"SingletonLock", "SingletonCookie", "SingletonSocket"}
_ALLOWED_SNAPSHOT_PASSWORD_STATES = {"not_present", "verified"}


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

    def _read(self) -> dict[str, Any]:
        if not self.paths.epoch_file.exists():
            return {"max_epoch": 0, "boot_id": None}
        try:
            value = json.loads(self.paths.epoch_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrowserRuntimeError("epoch_state_corrupt", "epoch state cannot be read") from exc
        if not isinstance(value, dict) or not isinstance(value.get("max_epoch"), int):
            raise BrowserRuntimeError("epoch_state_corrupt", "epoch state shape is invalid")
        return value

    def begin_boot(self, boot_id: str | None = None) -> str:
        with self._lock:
            value = self._read()
            current_boot = boot_id or uuid.uuid4().hex
            _validate_id(current_boot, "boot_id")
            _atomic_write_json(
                self.paths.epoch_file,
                {"max_epoch": max(0, int(value["max_epoch"])), "boot_id": current_boot, "updated_at": _now().isoformat()},
            )
            return current_boot

    def allocate(self, requested_epoch: int | None = None, *, boot_id: str | None = None) -> int:
        with self._lock:
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
                {"max_epoch": next_epoch, "boot_id": value.get("boot_id"), "updated_at": _now().isoformat()},
            )
            return next_epoch

    def assert_current(self, *, epoch: int, boot_id: str) -> None:
        value = self._read()
        if value.get("boot_id") != boot_id or int(value.get("max_epoch", -1)) != epoch:
            raise BrowserRuntimeError("stale_epoch", "epoch or boot generation is no longer current")


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


class ProcessTreeSupervisor:
    """Stops every registered child before a lease can be released."""

    def __init__(self, processes: Iterable[Any] = ()) -> None:
        self._processes: list[Any] = list(processes)
        self._lock = threading.Lock()
        self._stopping = False

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
            return all(self._returncode(process) is not None for process in self._processes)

    @staticmethod
    def _pid(process: Any) -> int | None:
        value = getattr(process, "pid", None)
        return value if isinstance(value, int) and value > 0 else None

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
        for process in processes:
            if self._returncode(process) is None:
                self._send_signal(process, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and not all(self._returncode(p) is not None for p in processes):
            time.sleep(0.05)
        for process in processes:
            if self._returncode(process) is None:
                self._send_signal(process, kill_signal)
        deadline = time.monotonic() + max(1.0, grace_seconds)
        while time.monotonic() < deadline and not all(self._returncode(p) is not None for p in processes):
            time.sleep(0.05)
        return all(self._returncode(process) is not None for process in processes)
    async def stop_async(self, *, grace_seconds: float = 10.0) -> bool:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        for process in processes:
            if self._returncode(process) is None:
                self._send_signal(process, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline and not all(self._returncode(p) is not None for p in processes):
            await asyncio.sleep(0.05)
        for process in processes:
            if self._returncode(process) is None:
                self._send_signal(process, kill_signal)
        deadline = asyncio.get_running_loop().time() + max(1.0, grace_seconds)
        while asyncio.get_running_loop().time() < deadline and not all(self._returncode(p) is not None for p in processes):
            await asyncio.sleep(0.05)
        return all(self._returncode(process) is not None for process in processes)


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
    """Count credential rows without decrypting or selecting secret values."""

    profile = Path(profile_dir)
    if not profile.is_absolute() or profile == Path("/") or profile.is_symlink() or not profile.is_dir():
        raise BrowserRuntimeError("profile_path_invalid", "profile path is invalid")
    try:
        databases = sorted(path for path in profile.glob("**/Login Data") if path.is_file() and not path.is_symlink())
    except OSError as exc:
        raise BrowserRuntimeError("password_inventory_blocked", "password metadata cannot be enumerated") from exc
    if not databases:
        return PasswordInventory("not_present", 0, 0)
    total = 0
    unknown_databases = 0
    for database in databases:
        safe_path = quote(str(database), safe="/\\:")
        uri = f"file:{safe_path}?mode=ro"
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
                total += int(row[0] if row else 0)
            finally:
                connection.close()
                connection = None
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise BrowserRuntimeError("password_inventory_blocked", "password metadata cannot be verified") from exc
    if unknown_databases:
        return PasswordInventory("unknown", total, len(databases))
    return PasswordInventory("present" if total else "not_present", total, len(databases))


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
        }
        manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(staging / "manifest.json", manifest_payload)
        _atomic_write_bytes(staging / "complete.marker", f"sha256:{checksum_digest}\n".encode("ascii"))
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
) -> tuple[dict[str, Any], Path]:
    """Validate the current manifest, marker and every copied file.

    The optional identity arguments let a caller bind recovery to the
    already-authorized account/session.  Without that binding, a valid
    snapshot from another account could be copied into this profile path.
    """

    paths.ensure()
    pointer = _read_pointer(paths.snapshots_dir / "current")
    if pointer is None:
        raise BrowserRuntimeError("profile_missing", "no committed profile snapshot exists")
    if pointer in _POINTER_NAMES or not re.fullmatch(r"v[1-9][0-9]*", pointer):
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot pointer is invalid")
    snapshot_dir = (paths.snapshots_dir / pointer).resolve()
    root = paths.snapshots_dir.resolve()
    if snapshot_dir.parent != root or not snapshot_dir.is_dir():
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot path escapes snapshot root")
    try:
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        checksums = json.loads((snapshot_dir / "checksums.json").read_text(encoding="utf-8"))
        marker = (snapshot_dir / "complete.marker").read_text(encoding="ascii").strip()
    except (OSError, ValueError, UnicodeError) as exc:
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot metadata is incomplete") from exc
    if not isinstance(manifest, dict) or not isinstance(checksums, list) or not marker.startswith("sha256:"):
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot metadata shape is invalid")
    if manifest.get("runtime_version") != _RUNTIME_VERSION:
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot runtime version is incompatible")
    expected_identity = {
        "workspace_id": expected_workspace_id,
        "account_id": expected_account_id,
        "profile_id": expected_profile_id,
        "node_id": expected_node_id,
    }
    for field, expected in expected_identity.items():
        if expected is not None and manifest.get(field) != expected:
            raise BrowserRuntimeError("profile_identity_mismatch", "snapshot identity does not match account")
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
    return manifest, snapshot_dir


def restore_current_snapshot(
    paths: ProfileRuntimePaths,
    *,
    allow_empty: bool = False,
    expected_workspace_id: str | None = None,
    expected_account_id: str | None = None,
    expected_profile_id: str | None = None,
    expected_node_id: str | None = None,
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


def _read_pointer(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BrowserRuntimeError("profile_manifest_invalid", "snapshot pointer cannot be read") from exc
    return value or None


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
