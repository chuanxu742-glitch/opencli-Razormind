from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import backend.browser_account_runtime as runtime
from backend.browser_account_runtime import ProcessTreeSupervisor


class _ExitedRegisteredProcess:
    def __init__(self, events: list[str], *, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._events = events

    def poll(self) -> int:
        self._events.append("poll")
        self.returncode = 0
        return 0


class ProcessTreeSupervisorTests(unittest.TestCase):
    def test_posix_tree_alive_reaps_parent_before_group_probe(self) -> None:
        events: list[str] = []
        process = _ExitedRegisteredProcess(events)

        def missing_group(pid: int, sig: int) -> None:
            self.assertEqual((pid, sig), (process.pid, 0))
            events.append("probe")
            raise ProcessLookupError(errno.ESRCH, "missing process group")

        with (
            patch.object(runtime.os, "name", "posix"),
            patch.object(runtime.os, "killpg", missing_group, create=True),
        ):
            self.assertFalse(ProcessTreeSupervisor._tree_alive(process))

        self.assertEqual(events, ["poll", "probe"])

    def test_posix_tree_alive_preserves_surviving_orphan_group(self) -> None:
        events: list[str] = []
        process = _ExitedRegisteredProcess(events)

        def live_group(pid: int, sig: int) -> None:
            self.assertEqual((pid, sig), (process.pid, 0))
            events.append("probe")

        with (
            patch.object(runtime.os, "name", "posix"),
            patch.object(runtime.os, "killpg", live_group, create=True),
        ):
            self.assertTrue(ProcessTreeSupervisor._tree_alive(process))

        self.assertEqual(process.returncode, 0)
        self.assertEqual(events, ["poll", "probe"])

    def test_posix_tree_alive_fails_closed_on_probe_errors(self) -> None:
        for probe_error in (
            PermissionError(errno.EPERM, "permission denied"),
            OSError(errno.EIO, "unknown process-group failure"),
        ):
            with self.subTest(probe_error=probe_error):
                process = _ExitedRegisteredProcess([])

                def failed_probe(_pid: int, _sig: int) -> None:
                    raise probe_error

                with (
                    patch.object(runtime.os, "name", "posix"),
                    patch.object(runtime.os, "killpg", failed_probe, create=True),
                ):
                    self.assertTrue(ProcessTreeSupervisor._tree_alive(process))

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_posix_stop_reaps_actual_registered_child(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            start_new_session=True,
        )
        supervisor = ProcessTreeSupervisor([process])
        try:
            self.assertTrue(supervisor.stop(grace_seconds=0.5))
            self.assertIsNotNone(process.poll())
            self.assertIsNotNone(supervisor.last_shutdown)
            self.assertTrue(supervisor.last_shutdown.confirmed)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
