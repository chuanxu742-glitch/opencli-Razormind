"""Interactive portal boundary regressions; no live browser or secret fixtures."""

import json
import struct
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import agent_runtime_dispatch as dispatch
from backend.schemas.browser_account import PortalControlMessageV1, PortalOwnerRouteV1
from backend.services.browser_portal_contract import decode_portal_wire_frame


def route_for(kind="approved"):
    target = {
        "tab_id": 7,
        "frame_id": 0,
        "document_id": "document",
        "origin": "https://example.com",
    }
    return PortalOwnerRouteV1(
        binding={
            "account_ref": {"workspace_id": "w", "account_id": "a"},
            "session_id": "s",
            "epoch": 1,
            "target": target,
            "view_generation": 1,
            "record_session_id": "r",
        },
        node_identity={"node_id": "n", "boot_id": "b"},
        owner_endpoint="https://node.test",
        tunnel_handle="tunnel",
        tunnel_auth_digest="a" * 64,
        region_focus={
            "target": target,
            "view_generation": 1,
            "region_kind": kind,
            "approved_regions": [{"x": 100, "y": 100, "width": 100, "height": 100}],
            "focused_field_ref": "official-login:otp" if kind == "form" else None,
        },
        session_revision=1,
        route_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        max_frame_bytes=4_000_000,
        max_input_bytes=4096,
    )


def control_for(route, kind="pointer", sequence=1, **extra):
    return PortalControlMessageV1(
        **dispatch._portal_binding(route).model_dump(), kind=kind, sequence=sequence, **extra
    )


def wire_control(control, sensitive=None):
    message = control.model_dump(mode="json", exclude={"sensitive_payload"})
    if sensitive is not None:
        message["sensitive_payload"] = sensitive
    binding = {
        key: message[key]
        for key in [
            "contract_version",
            "workspace_id",
            "account_id",
            "session_id",
            "epoch",
            "target",
            "view_generation",
        ]
    }
    metadata = json.dumps(
        {
            "contract_version": 1,
            "protocol": "qrac2.portal.v1",
            "sequence": control.sequence,
            "encoding": "control-json",
            "content_type": "application/json",
            "mime_type": "application/json",
            "byte_length": None,
            "binding": binding,
            "message": message,
        }
    ).encode()
    return struct.pack(">4sBBBBHIH", b"Q2P1", 1, 0, 0, 0, len(metadata), 0, 0) + metadata


@pytest.mark.parametrize("action", [None, "down", "move", "up"])
def test_wire_preserves_legacy_click_and_explicit_pointer_action(action):
    route = route_for()
    control = control_for(route)
    payload = {"value_present": False, "key": None, "x": 150, "y": 150}
    if action is not None:
        payload["pointer_action"] = action
    decoded = decode_portal_wire_frame(wire_control(control, payload))
    assert decoded.transient.control.sensitive_payload.pointer_action == action


@pytest.mark.parametrize(
    "action,events",
    [
        (None, ["mousePressed", "mouseReleased"]),
        ("down", ["mousePressed"]),
        ("move", ["mouseMoved"]),
        ("up", ["mouseReleased"]),
    ],
)
@pytest.mark.asyncio
async def test_pointer_dispatches_only_requested_action_inside_approved_region(
    monkeypatch, action, events
):
    cdp = AsyncMock(return_value={})
    monkeypatch.setattr(dispatch, "_cdp_command", cdp)
    route = route_for()
    control = control_for(route, sensitive_payload={"x": 150, "y": 160, "pointer_action": action})
    assert await dispatch.apply_portal_control(
        websocket_url="ws://bound", cdp_endpoint="http://local", route=route, control=control
    )
    assert [call.args[2]["type"] for call in cdp.await_args_list] == events
    assert all(call.args[0] == "ws://bound" for call in cdp.await_args_list)
    assert cdp.await_args_list[-1].args[2]["buttons"] == (0 if events[-1] == "mouseReleased" else 1)


@pytest.mark.parametrize(
    "kind,payload,region",
    [
        ("pointer", {"x": 99, "y": 150}, "approved"),
        ("pointer", {"x": 150, "y": 150}, "qr"),
        ("key", {"key": "a"}, "approved"),
        ("key", {"key": "Enter"}, "qr"),
    ],
)
@pytest.mark.asyncio
async def test_unapproved_pointer_and_key_never_reach_browser(monkeypatch, kind, payload, region):
    cdp = AsyncMock()
    monkeypatch.setattr(dispatch, "_cdp_command", cdp)
    route = route_for(region)
    with pytest.raises(ValueError):
        await dispatch.apply_portal_control(
            websocket_url="ws://bound",
            cdp_endpoint="http://local",
            route=route,
            control=control_for(route, kind, sensitive_payload=payload),
        )
    cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_takeover_preserves_full_target_binding_and_rejects_failed_switch(monkeypatch):
    from backend import browser_account_runtime

    route = route_for("qr")
    monkeypatch.setattr(
        browser_account_runtime,
        "account_runtime_allocator",
        lambda: SimpleNamespace(get=lambda _: SimpleNamespace(command_id="cmd")),
    )
    monkeypatch.setattr(
        dispatch,
        "runtime_lease_book",
        lambda: SimpleNamespace(
            current=lambda _: SimpleNamespace(
                session=SimpleNamespace(login_rule_id="rule", login_rule_version="1")
            )
        ),
    )
    invoke = AsyncMock(
        return_value={
            "result": {
                "ok": True,
                "result": {"mode": "form", "state": "presenting", "view_generation": 1},
            }
        }
    )
    monkeypatch.setattr(dispatch, "invoke_script_host", invoke)
    assert await dispatch.apply_portal_control(
        websocket_url="ws://bound",
        cdp_endpoint="http://local",
        route=route,
        control=control_for(route, "takeover"),
    )
    request = invoke.await_args.args[0]
    assert request.input["target"] == {**route.binding.target.model_dump(), "view_generation": 1}
    assert set(request.input["target"]) == {
        "tab_id",
        "frame_id",
        "document_id",
        "origin",
        "view_generation",
    }
    assert request.input["rule_id"] == "rule"
    assert request.config["tab_id"] == 7
    invoke.return_value = {"result": {"ok": False, "code": "stale_generation"}}
    with pytest.raises(ValueError, match="rejected"):
        await dispatch.apply_portal_control(
            websocket_url="ws://bound",
            cdp_endpoint="http://local",
            route=route,
            control=control_for(route, "takeover"),
        )


@pytest.mark.asyncio
async def test_official_field_rejects_stale_focus_before_evaluating_sensitive_input(monkeypatch):
    evaluate = AsyncMock(return_value=True)
    monkeypatch.setattr(dispatch, "_evaluate_cdp_target", evaluate)
    route = route_for("form")
    control = control_for(
        route,
        "field_input",
        field_ref="official-login:old",
        sensitive_payload={"value": "test-value"},
    )
    with pytest.raises(ValueError, match="outside approved focus"):
        await dispatch._apply_guarded_field_input("ws://bound", route, control)
    evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_sensitive_field_crossing_clip_boundary_is_masked_inside_clip(monkeypatch):
    monkeypatch.setattr(
        dispatch,
        "_evaluate_cdp_target",
        AsyncMock(return_value=[{"x": 80, "y": 120, "width": 50, "height": 20}]),
    )
    masks = await dispatch._portal_masked_regions("ws://bound", route_for())
    assert masks == [{"x": 100, "y": 120, "width": 30, "height": 20}]


@pytest.mark.asyncio
async def test_too_many_sensitive_fields_fail_closed_instead_of_omitting_last(monkeypatch):
    fields = [{"x": 100, "y": 100 + index * 2, "width": 20, "height": 1} for index in range(33)]
    monkeypatch.setattr(dispatch, "_evaluate_cdp_target", AsyncMock(return_value=fields))
    with pytest.raises((ValueError, RuntimeError)):
        await dispatch._portal_masked_regions("ws://bound", route_for())


@pytest.mark.asyncio
async def test_expired_route_after_pointer_down_releases_mouse(monkeypatch):
    from backend import agent_server as agent

    route = route_for()
    cdp = AsyncMock(return_value={})
    monkeypatch.setattr(dispatch, "_cdp_command", cdp)
    monkeypatch.setattr(agent, "resolve_portal_target", AsyncMock(return_value="ws://bound"))
    runtime = agent._PortalRuntime(
        portal_id="s", route=route, cdp_endpoint="http://local", websocket_url="ws://bound"
    )
    monkeypatch.setitem(agent._ACTIVE_PORTALS, "s", runtime)
    ws = SimpleNamespace(send=AsyncMock())
    await agent._handle_ws_portal_binary(
        ws,
        wire_control(
            control_for(route),
            {"value_present": False, "key": None, "x": 150, "y": 150, "pointer_action": "down"},
        ),
    )
    runtime.route = route.model_copy(
        update={"route_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    await agent._handle_ws_portal_binary(
        ws,
        wire_control(
            control_for(route, sequence=2),
            {"value_present": False, "key": None, "x": 150, "y": 150, "pointer_action": "up"},
        ),
    )
    assert "s" not in agent._ACTIVE_PORTALS
    assert [call.args[2]["type"] for call in cdp.await_args_list] == [
        "mousePressed",
        "mouseReleased",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": 2},
        {"view_generation": 2},
        {
            "target": {
                "tab_id": 8,
                "frame_id": 0,
                "document_id": "document",
                "origin": "https://example.com",
            }
        },
    ],
)
@pytest.mark.asyncio
async def test_route_lineage_mismatch_never_dispatches_pointer(monkeypatch, change):
    from backend import agent_server as agent

    route = route_for()
    apply = AsyncMock()
    monkeypatch.setattr(agent, "apply_portal_control", apply)
    runtime = agent._PortalRuntime(
        portal_id="s", route=route, cdp_endpoint="http://local", websocket_url="ws://bound"
    )
    monkeypatch.setitem(agent._ACTIVE_PORTALS, "s", runtime)
    control = PortalControlMessageV1.model_validate({**control_for(route).model_dump(), **change})
    await agent._handle_ws_portal_binary(
        SimpleNamespace(send=AsyncMock()),
        wire_control(
            control,
            {"value_present": False, "key": None, "x": 150, "y": 150, "pointer_action": "down"},
        ),
    )
    apply.assert_not_awaited()
    assert "s" not in agent._ACTIVE_PORTALS
