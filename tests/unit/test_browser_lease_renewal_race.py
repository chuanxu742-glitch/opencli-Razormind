from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.browser_account_runtime import BrowserRuntimeError, EpochLease, LeaseSupervisor


def test_persisted_renewal_and_supervisor_deadline_are_one_atomic_generation():
    lease = EpochLease("node", "boot", 1, datetime.now(UTC) + timedelta(seconds=30), "owner")
    renewed = EpochLease("node", "boot", 1, lease.expires_at + timedelta(seconds=30), "owner")
    persisted = [lease]
    written, publish, checked = Event(), Event(), Event()
    process = SimpleNamespace(stop=Mock(return_value=True), last_shutdown=None)
    paths = SimpleNamespace(mark_stopped=Mock(), mark_quarantined=Mock())

    def check(current):
        assert current == persisted[0], "supervisor saw old deadline after persistence advanced"
        checked.set()

    supervisor = LeaseSupervisor(lease, process, paths, lease_check=check, poll_seconds=0.1)
    errors = []

    def persist(current):
        assert current == lease
        persisted[0] = renewed
        written.set()
        assert publish.wait(2)
        return renewed

    def renew():
        try:
            supervisor.renew_lease(persist)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=renew)
    thread.start()
    try:
        assert written.wait(2)
        supervisor.start()
        # A full polling interval falls inside the deliberately paused disk write.
        assert not checked.wait(0.2)
        assert not supervisor.lost.is_set()
        publish.set()
        thread.join(2)
        assert not thread.is_alive()
        assert not errors
        assert checked.wait(2)
        assert supervisor.lease == renewed
        assert not supervisor.lost.is_set()
        process.stop.assert_not_called()
    finally:
        publish.set()
        thread.join(2)
        supervisor.close()


def test_lost_supervisor_cannot_be_resurrected_by_late_renewal():
    lease = EpochLease("node", "boot", 1, datetime.now(UTC) + timedelta(seconds=30), "owner")
    supervisor = LeaseSupervisor(lease, Mock(), Mock())
    supervisor.lost.set()
    persist = Mock()
    with pytest.raises(BrowserRuntimeError, match="stale_lease"):
        supervisor.renew_lease(persist)
    persist.assert_not_called()
