import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import browser_target_mapping as mapping
from backend.browser_login_observer import observe_until_terminal
from backend.browser_login_rules import load_bundle_login_rule, load_packaged_rule

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("platform", ["bilibili", "douyin"])
def test_bundle_resolver_does_not_confuse_probe_implementation_with_scan_acceptance(platform):
    def resolve(version, manifest=None):
        packaged = json.loads(
            (ROOT / f"chrome/runtime-bundles/opencli-default/{version}/manifest.json").read_text()
        )
        return load_bundle_login_rule(
            ROOT,
            bundle_name="opencli-default",
            bundle_version=version,
            bundle_manifest=packaged if manifest is None else manifest,
            rule_id=platform + "-qr",
            rule_version="0.1.0",
        )

    assert resolve("3") is None
    assert resolve("4", {}) is None
    rule = resolve("4")
    assert rule["identity_probe_supported"] is True
    assert rule["authentication_verified"] is False


def test_fixed_registry_preserves_fixture_and_qr_only_platform(tmp_path):
    host = ROOT / "chrome/script-host"
    assert load_packaged_rule(host, "controlled-login-fixture", "1.0.0")["login_url"] == "/login"
    xhs = load_packaged_rule(host, "xiaohongshu-qr", "0.1.0")
    assert xhs["allowed_origins"] == ["https://www.xiaohongshu.com"]
    assert xhs["authentication_verified"] is False
    assert xhs["modes"] == ["qr"]
    assert load_packaged_rule(host, "douyin", "1") is None
    assert load_packaged_rule(host, "../rules", "1") is None
    changed = tmp_path / "packs/account-login"
    changed.mkdir(parents=True)
    xhs["allowed_origins"] = ["https://example.invalid"]
    (changed / "xiaohongshu-qr.json").write_text(json.dumps(xhs))
    assert load_packaged_rule(tmp_path, "xiaohongshu-qr", "0.1.0") is None


def observation(state, evidence="unknown", at="one"):
    return SimpleNamespace(
        model_dump=lambda **_: {
            "state": state,
            "evidence_kind": evidence,
            "target": {"document_id": "same"},
            "view_generation": 1,
            "observed_at": at,
        }
    )


@pytest.mark.asyncio
async def test_observer_deduplicates_while_waiting_then_stops_on_valid_identity():
    admitted = SimpleNamespace(claim=SimpleNamespace(epoch=1), session=SimpleNamespace(purpose="login"))
    observe = AsyncMock(
        side_effect=[
            observation("presenting"),
            observation("presenting", at="two"),
            observation("verifying", "valid"),
        ]
    )
    send, sleep = AsyncMock(), AsyncMock()
    await observe_until_terminal(current=lambda: admitted, observe=observe, send=send, sleep=sleep)
    assert observe.await_count == 3
    assert send.await_count == 2
    assert send.call_args.args[0]["observation"]["evidence_kind"] == "valid"


@pytest.mark.asyncio
async def test_observer_discards_result_if_lease_revoked_during_read():
    admitted = SimpleNamespace(claim=SimpleNamespace(epoch=1), session=SimpleNamespace(purpose="login"))
    current = iter([admitted, None])
    send = AsyncMock()
    await observe_until_terminal(
        current=lambda: next(current),
        observe=AsyncMock(return_value=observation("presenting")),
        send=send,
    )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_closed_session_does_not_poll_again():
    admitted = SimpleNamespace(claim=SimpleNamespace(epoch=1), session=SimpleNamespace(purpose="login"))
    observe = AsyncMock(return_value=observation("closed"))
    await observe_until_terminal(
        current=lambda: admitted, observe=observe, send=AsyncMock(), sleep=AsyncMock()
    )
    assert observe.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_target_mapping_requires_unique_isolated_probe_and_cleans_up(monkeypatch, count):
    evaluate = AsyncMock(
        side_effect=["OpenCLI Script Host", "actual-document", True, None]
        if count == 1
        else ["OpenCLI Script Host", "actual-document", None]
    )
    monkeypatch.setattr(mapping, "_probe_page", AsyncMock(return_value=True))
    target = SimpleNamespace(
        tab_id=7,
        frame_id=0,
        document_id="bound-doc",
        view_generation=2,
        origin="https://www.xiaohongshu.com",
    )
    pages = [
        {"url": target.origin, "webSocketDebuggerUrl": f"ws://local/page-{i}"} for i in range(count)
    ]
    kwargs = dict(
        pages=pages,
        workers=[{"webSocketDebuggerUrl": "ws://local/worker"}],
        target=target,
        evaluate=evaluate,
    )
    if count == 1:
        assert await mapping.map_chrome_tab(**kwargs) == "ws://local/page-0"
    else:
        with pytest.raises(RuntimeError, match="missing or ambiguous"):
            await mapping.map_chrome_tab(**kwargs)
    expressions = [call.args[1] for call in evaluate.call_args_list]
    assert "world:'ISOLATED'" in expressions[1]
    assert "delete globalThis[key]" in expressions[-1]
    assert all("world:'MAIN'" not in expression for expression in expressions)


@pytest.mark.asyncio
async def test_cdp_probe_ignores_page_world_and_checks_main_frame(monkeypatch):
    class Socket:
        def __init__(self):
            self.messages = []
            self.evaluated = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, wire):
            command = json.loads(wire)
            method = command["method"]
            result = {}
            if method == "Page.getFrameTree":
                result = {"frameTree": {"frame": {"id": "main"}}}
            if method == "Runtime.enable":
                for ident, default, frame in [
                    (1, True, "main"),
                    (2, False, "other"),
                    (3, False, "main"),
                ]:
                    self.messages.append(
                        {
                            "method": "Runtime.executionContextCreated",
                            "params": {
                                "context": {
                                    "id": ident,
                                    "auxData": {"isDefault": default, "frameId": frame},
                                }
                            },
                        }
                    )
            if method == "Runtime.evaluate":
                self.evaluated.append(command["params"]["contextId"])
                result = {"result": {"value": True}}
            self.messages.append({"id": command["id"], "result": result})

        async def recv(self):
            return json.dumps(self.messages.pop(0))

    socket = Socket()
    monkeypatch.setattr(mapping.websockets, "connect", lambda *args, **kwargs: socket)
    assert await mapping._probe_page("ws://local", "probe", "nonce", "https://www.xiaohongshu.com")
    assert socket.evaluated == [3]


def test_capacity_reports_only_actual_packaged_rules_and_measured_slots(tmp_path, monkeypatch):
    import shutil

    from backend import agent_server
    from backend.schemas.browser_account import NodeIdentityV1

    bundle = tmp_path / "bundles/default/3"
    script_host = bundle / "extensions/script-host"
    shutil.copytree(ROOT / "chrome/script-host", script_host)
    manifest = bundle / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "3",
                "components": [{"id": "opencli-script-host", "path": "extensions/script-host"}],
            }
        )
    )
    config = SimpleNamespace(
        bundle_id="actual-bundle",
        bundle_manifest=manifest,
        bundle_root=tmp_path / "bundles",
        runtime_root=tmp_path,
    )
    allocator = SimpleNamespace(configuration=config, occupied_slots=lambda: 1)
    monkeypatch.setattr(agent_server, "account_runtime_allocator", lambda: allocator)
    fact = agent_server._measured_account_capacity(NodeIdentityV1(node_id="n", boot_id="boot"))
    assert fact.slot_limit == 1 and fact.occupied_slots == 1 and fact.disk_available > 0
    assert (fact.expires_at - fact.observed_at).total_seconds() == 30
    rules = fact.capabilities["browser_login_bundles"][0]["rules"]
    assert {rule["id"] for rule in rules} == {"controlled-login-fixture", "xiaohongshu-qr"}
    (script_host / "packs/account-login/xiaohongshu-qr.json").unlink()
    remaining = agent_server._measured_account_capacity(NodeIdentityV1(node_id="n", boot_id="boot"))
    assert [rule["id"] for rule in remaining.capabilities["browser_login_bundles"][0]["rules"]] == [
        "controlled-login-fixture"
    ]
