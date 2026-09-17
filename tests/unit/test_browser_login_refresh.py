from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from backend import agent_runtime_dispatch as dispatch
from backend.browser_account_runtime import RuntimeLeaseAdmission
from backend.browser_login_observer import observe_until_terminal
from backend.browser_login_refresh import ExpiredQrRefresher
from backend.schemas.browser_account import (
    LoginObservationV1,
    NodeClaimV1,
    NodeIdentityV1,
    SessionEnvelopeV1,
    SessionTargetV1,
)


def facts(platform="xiaohongshu", generation=1, state="refreshing"):
    target_data = {
        "tab_id": 7,
        "frame_id": 0,
        "document_id": f"d{generation}",
        "origin": {
            "xiaohongshu": "https://www.xiaohongshu.com",
            "bilibili": "https://passport.bilibili.com",
            "douyin": "https://creator.douyin.com",
        }[platform],
    }
    now = datetime.now(UTC)
    claim = NodeClaimV1(
        workspace_id="w",
        account_id="a",
        command_id="c",
        session_id="s",
        node_id="n",
        boot_id="b",
        epoch=1,
        expected_revision=1,
        claimed_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    node = NodeIdentityV1(node_id="n", boot_id="b")
    target = SessionTargetV1(**target_data)
    observation = LoginObservationV1(
        claim=claim,
        node_identity=node,
        account_ref={"workspace_id": "w", "account_id": "a"},
        session_id="s",
        epoch=1,
        observed_at=now,
        state=state,
        error_code="auth_required",
        evidence_kind="unknown",
        external_identity=None,
        target=target,
        view_generation=generation,
        rule_id=f"{platform}-qr",
        rule_version="0.2.0" if platform == "xiaohongshu" else "0.1.0",
    )
    session = SessionEnvelopeV1(
        workspace_id="w",
        account_id="a",
        session_id="s",
        node_id="n",
        node_boot_id="b",
        lease_id="l",
        epoch=1,
        lease_expires_at=claim.expires_at,
        runtime_bundle_id="bundle",
        runtime_bundle_version="4",
        login_rule_id=observation.rule_id,
        login_rule_version=observation.rule_version,
        target=target,
        view_generation=generation,
        purpose="login",
        command_id="c",
    )
    admission = RuntimeLeaseAdmission(claim=claim, session=session, node_identity=node)
    discovery = target_data | {"view_generation": generation, "qr_generation": generation}
    return admission, observation, discovery


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["xiaohongshu", "bilibili", "douyin"])
async def test_expired_qr_invokes_fixed_rule_same_tab_with_generation(monkeypatch, platform):
    admission, observation, discovery = facts(platform)
    discover = AsyncMock(return_value=discovery)
    invoke = AsyncMock(return_value={"result": {"ok": True, "result": {"state": "refreshing"}}})
    monkeypatch.setattr(dispatch, "_discover_login_target", discover)
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    assert await ExpiredQrRefresher().refresh(
        admission=admission,
        observation=observation,
        cdp_endpoint="http://cdp",
        current=lambda: admission,
    )
    request = invoke.await_args.args[0]
    assert request.workflow == "login.refresh"
    assert request.input["rule_id"] == observation.rule_id
    assert request.input["expected_qr_generation"] == 1
    assert request.input["target"]["tab_id"] == 7
    assert discover.await_args.kwargs["expected_tab_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["challenge", "verifying", "unknown", "presenting", "saved"])
async def test_no_refresh_for_scan_challenge_or_missing_qr(monkeypatch, state):
    admission, observation, _ = facts(state=state)
    discover = AsyncMock()
    monkeypatch.setattr(dispatch, "_discover_login_target", discover)
    assert not await ExpiredQrRefresher().refresh(
        admission=admission,
        observation=observation,
        cdp_endpoint="http://cdp",
        current=lambda: admission,
    )
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_and_interval_survive_new_document(monkeypatch):
    clock = [0]
    refresher = ExpiredQrRefresher(clock=lambda: clock[0])
    invoke = AsyncMock(return_value={"result": {"ok": True, "result": {"state": "refreshing"}}})
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    for generation, at, expected in [
        (1, 0, True),
        (2, 29, False),
        (2, 30, True),
        (3, 60, True),
        (4, 90, False),
    ]:
        admission, observation, discovery = facts(generation=generation)
        monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=discovery))
        clock[0] = at
        assert (
            await refresher.refresh(
                admission=admission,
                observation=observation,
                cdp_endpoint="http://cdp",
                current=lambda: admission,
            )
            is expected
        )
    assert invoke.await_count == 3


@pytest.mark.asyncio
async def test_string_target_ids_are_normalized_for_worker(monkeypatch):
    admission, observation, discovery = facts()
    data = observation.target.model_dump() | {"tab_id": "7", "frame_id": "0"}
    observation = LoginObservationV1.model_validate(observation.model_dump() | {"target": data})
    monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=discovery))
    invoke = AsyncMock(return_value={"result": {"ok": True, "result": {"state": "refreshing"}}})
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    assert await ExpiredQrRefresher().refresh(
        admission=admission,
        observation=observation,
        cdp_endpoint="http://cdp",
        current=lambda: admission,
    )
    request = invoke.await_args.args[0]
    assert type(request.input["target"]["tab_id"]) is int
    assert type(request.input["target"]["frame_id"]) is int
    assert request.input["target"] == data | {"tab_id": 7, "frame_id": 0, "view_generation": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [None, {}, {"ok": True}, {"result": {}}, {"result": {"ok": False}}, {"result": {"ok": 1}}],
)
async def test_failed_refresh_is_not_reported_as_success(monkeypatch, result):
    admission, observation, discovery = facts()
    monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=discovery))
    invoke = AsyncMock(return_value=result)
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    refresher = ExpiredQrRefresher()
    assert not await refresher.refresh(
        admission=admission,
        observation=observation,
        cdp_endpoint="http://cdp",
        current=lambda: admission,
    )
    assert refresher.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["document", "lease"])
async def test_recheck_blocks_document_or_lease_change(monkeypatch, mismatch):
    admission, observation, discovery = facts()
    if mismatch == "document":
        discovery["document_id"] = "new-document"
    monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=discovery))
    invoke = AsyncMock()
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    assert not await ExpiredQrRefresher().refresh(
        admission=admission,
        observation=observation,
        cdp_endpoint="http://cdp",
        current=lambda: None if mismatch == "lease" else admission,
    )
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_sends_expiration_before_refresh_and_then_continues():
    admission, expired, _ = facts()
    valid = LoginObservationV1.model_validate(
        expired.model_dump()
        | {
            "state": "verifying",
            "evidence_kind": "valid",
            "error_code": None,
            "external_identity": {"provider": "xiaohongshu", "subject": "stable-account"},
        }
    )
    observe = AsyncMock(side_effect=[expired, valid])
    events = []

    async def send(message):
        events.append(message["observation"]["state"])

    async def refresh(*_):
        events.append("reload")

    await observe_until_terminal(
        current=lambda: admission, observe=observe, send=send, sleep=AsyncMock(), refresh=refresh
    )
    assert events == ["refreshing", "reload", "verifying"]
