"""Exercise discovery races through real observation contracts and the node watcher."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from backend import agent_runtime_dispatch as dispatch
from backend import agent_server as server
from backend import browser_login_observer as observer
from backend.browser_account_runtime import BrowserRuntimeError
from tests.unit.test_browser_login_refresh import facts


def invocation_wire(observation, *, generation):
    wire = observation.model_dump(mode="json")
    return {
        "result": {
            "ok": True,
            "result": {
                key: wire[key]
                for key in (
                    "session_id",
                    "epoch",
                    "rule_id",
                    "rule_version",
                    "target",
                    "state",
                    "evidence_kind",
                    "browser_session_state",
                    "external_identity",
                    "observed_at",
                    "error_code",
                )
            }
            | {"view_generation": generation},
        }
    }


@pytest.mark.asyncio
async def test_invoke_http_failure_becomes_retryable_observation_error(monkeypatch):
    admission, _, target = facts(state="presenting")
    monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=target))
    monkeypatch.setattr(
        dispatch,
        "invoke_script_host",
        AsyncMock(side_effect=HTTPException(status_code=502, detail="generation changed")),
    )
    with pytest.raises(BrowserRuntimeError) as caught:
        await dispatch.observe_login_runtime(
            cdp_endpoint="http://cdp",
            session=admission.session,
            claim=admission.claim,
            node_identity=admission.node_identity,
            expected_origin=target["origin"],
            expected_tab_id=7,
        )
    assert caught.value.code == "login_observation_unavailable"


@pytest.mark.asyncio
async def test_node_watcher_rediscovers_after_race_then_emits_presenting(monkeypatch):
    admission, presenting, target = facts(state="presenting")
    discover = AsyncMock(side_effect=[target, target | {"view_generation": 2, "qr_generation": 2}])
    invoke = AsyncMock(
        side_effect=[
            HTTPException(status_code=502, detail="stale generation"),
            invocation_wire(presenting, generation=2),
        ]
    )
    monkeypatch.setattr(dispatch, "_discover_login_target", discover)
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    monkeypatch.setattr(server, "observe_login_runtime", dispatch.observe_login_runtime)
    active = [admission]
    monkeypatch.setattr(server, "_current_login_admission", lambda _: active[0])
    running = Mock()
    running.binding.cdp_endpoint = "http://cdp"
    running.bundle.login_origin = target["origin"]
    allocator = Mock()
    allocator.get.return_value = running
    monkeypatch.setattr(server, "account_runtime_allocator", lambda: allocator)
    retry_sleep = AsyncMock()
    monkeypatch.setattr(server.asyncio, "sleep", retry_sleep)
    actual_observer = observer.observe_until_terminal

    async def without_poll_delay(**kwargs):
        await actual_observer(**kwargs, sleep=AsyncMock())

    monkeypatch.setattr(observer, "observe_until_terminal", without_poll_delay)
    received = []

    async def send(payload):
        received.append(json.loads(payload))
        active[0] = None  # End by explicit lease revocation only after successful delivery.

    ws = Mock()
    ws.send = send
    warning = Mock()
    monkeypatch.setattr(server.logger, "warning", warning)
    await server._watch_login(ws, admission.claim.command_id, expected_tab_id=7)
    assert len(received) == 1
    result = received[0]["observation"]
    assert received[0]["type"] == "login_observation"
    assert result["state"] == "presenting"
    assert result["view_generation"] == 2
    assert result["claim"] == admission.claim.model_dump(mode="json")
    assert discover.await_count == invoke.await_count == 2
    assert invoke.await_args.args[0].input["expected_qr_generation"] == 2
    retry_sleep.assert_awaited_once_with(1)
    warning.assert_not_called()


@pytest.mark.asyncio
async def test_direct_browser_session_can_produce_trusted_login_observation(monkeypatch):
    admission, observation, target = facts(state="presenting")
    browser_session = admission.session.model_copy(update={"purpose": "browser"})
    monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=target))
    monkeypatch.setattr(
        dispatch,
        "invoke_script_host",
        AsyncMock(return_value=invocation_wire(observation, generation=1)),
    )

    result = await dispatch.observe_login_runtime(
        cdp_endpoint="http://cdp",
        session=browser_session,
        claim=admission.claim,
        node_identity=admission.node_identity,
        expected_origin=target["origin"],
    )

    assert result.session_id == browser_session.session_id
    assert result.claim == admission.claim


@pytest.mark.asyncio
async def test_browser_appearance_passes_wire_and_deduplicates_separately_from_auth(monkeypatch):
    admission, base, target = facts(state="presenting")
    from backend.schemas.browser_account import LoginObservationV1

    visible = LoginObservationV1.model_validate(base.model_dump() | {"browser_session_state": "signed_in_visible"})
    monkeypatch.setattr(dispatch, "_discover_login_target", AsyncMock(return_value=target))
    monkeypatch.setattr(dispatch, "invoke_script_host", AsyncMock(return_value=invocation_wire(visible, generation=1)))
    wire_observation = await dispatch.observe_login_runtime(
        cdp_endpoint="http://cdp", session=admission.session, claim=admission.claim,
        node_identity=admission.node_identity, expected_origin=target["origin"],
    )
    assert wire_observation.browser_session_state == "signed_in_visible"
    assert wire_observation.evidence_kind == "unknown"
    assert wire_observation.external_identity is None
    active = [admission]
    observations = iter([base, visible, visible, base])
    sent = []

    async def send(message):
        sent.append(message)
        if len(sent) == 3:
            active[0] = None

    await observer.observe_until_terminal(
        current=lambda: active[0], observe=AsyncMock(side_effect=lambda _: next(observations)),
        send=send, sleep=AsyncMock(),
    )
    assert [message["observation"]["browser_session_state"] for message in sent] == ["unknown", "signed_in_visible", "unknown"]
    with pytest.raises(ValueError):
        LoginObservationV1.model_validate(base.model_dump() | {"browser_session_state": "SECRET arbitrary text"})


@pytest.mark.asyncio
async def test_browser_observer_continues_after_valid_and_temporary_navigation():
    admission, base, _ = facts(state="presenting")
    admission = admission.__class__(
        claim=admission.claim,
        session=admission.session.model_copy(update={"purpose": "browser"}),
        node_identity=admission.node_identity,
    )
    valid = base.model_copy(
        update={
            "state": "verifying",
            "evidence_kind": "valid",
            "error_code": None,
            "external_identity": {"provider": "xiaohongshu", "subject": "user-a"},
        }
    )
    switched = valid.model_copy(
        update={
            "state": "error",
            "evidence_kind": "valid",
            "external_identity": {"provider": "xiaohongshu", "subject": "user-b"},
        }
    )
    observations = [valid, BrowserRuntimeError("login_observation_unavailable", "away"), switched]
    active = [admission]
    sent = []

    async def observe(_admission):
        value = observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def send(message):
        sent.append(message)
        if len(sent) == 2:
            active[0] = None

    await observer.observe_until_terminal(
        current=lambda: active[0],
        observe=observe,
        send=send,
        interval=0,
        sleep=AsyncMock(),
    )

    assert [item["observation"]["external_identity"]["subject"] for item in sent] == [
        "user-a",
        "user-b",
    ]
