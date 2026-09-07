from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import backend.browser_account_runtime as runtime
from backend.browser_account_errors import BrowserRuntimeError
from backend.browser_account_process import (
    ProcessTreeSupervisor,
    StackIsolation,
    validate_parallel_isolation,
)


def _isolation(root: Path, *, display: str = ":501") -> StackIsolation:
    return StackIsolation(
        display=display,
        cdp_port=31_001,
        bbx_port=31_002,
        daemon_port=19_825,
        home_dir=root / "home",
        cache_dir=root / "cache",
        profile_dir=root / "profile",
    )


def test_runtime_exports_process_types_unchanged() -> None:
    assert runtime.BrowserRuntimeError is BrowserRuntimeError
    assert runtime.ProcessTreeSupervisor is ProcessTreeSupervisor


def test_parallel_isolation_rejects_shared_display(tmp_path: Path) -> None:
    with pytest.raises(BrowserRuntimeError, match="runtime_isolation_conflict"):
        validate_parallel_isolation(
            (_isolation(tmp_path / "first"), _isolation(tmp_path / "second"))
        )


def test_process_shutdown_records_confirmed_evidence() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    supervisor = ProcessTreeSupervisor([process])
    try:
        assert supervisor.stop(grace_seconds=1)
        evidence = supervisor.last_shutdown
        assert evidence is not None
        assert evidence.requested_pids == (process.pid,)
        assert evidence.stopped_pids == (process.pid,)
        assert evidence.confirmed
        assert evidence.completed_at
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        time.sleep(0.01)
