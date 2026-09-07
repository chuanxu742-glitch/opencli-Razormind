"""Explicit service boundaries for isolated pipeline orchestration tests."""

import pytest

from backend.services.browser_account_service import execution_account_ref


@pytest.fixture
def anonymous_account_resolution(monkeypatch):
    """Keep non-account unit fixtures away from the application database.

    Account admission and real task provenance are exercised separately in
    test_browser_execution_session_producer.py. Reject an account-bearing
    fixture here rather than accidentally bypassing its authorization.
    """

    async def resolve(task_id, source, parameters, *, run_id=None):
        assert execution_account_ref(parameters, source.channel_config or {}) is None
        return None

    monkeypatch.setattr("backend.pipeline.pipeline._resolve_account_execution", resolve)
