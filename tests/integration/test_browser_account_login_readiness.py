import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.main import app
from backend.models.browser import (
    BrowserAccountLease,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import browser_account_service
from tests.fixtures.browser_account_login import seed_login_resources

BASE = "/api/v1/workspaces/login-options-workspace/browser-accounts"


@pytest_asyncio.fixture
async def login_account(db_session):
    db_session.add_all(
        [
            User(id="login-user", subject="login-subject"),
            Workspace(id="login-options-workspace", name="Login", slug="login"),
            WorkspaceMembership(
                id="login-member",
                workspace_id="login-options-workspace",
                user_id="login-user",
                role=WorkspaceRole.ADMIN,
            ),
        ]
    )
    await db_session.flush()
    resources = await seed_login_resources(db_session)
    account = await browser_account_service.create_browser_account(
        db_session,
        "login-options-workspace",
        {"workspace_id": "login-options-workspace", "label": "Controlled", **resources},
    )
    await db_session.commit()

    async def identity():
        return RequestIdentity(subject="login-subject", auth_method="test")

    app.dependency_overrides[get_request_identity] = identity
    yield account
    app.dependency_overrides.pop(get_request_identity, None)


def data(response):
    assert response.is_success, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_catalog_does_not_claim_commercial_qr_support(client, login_account):
    items = data(await client.get(f"{BASE}/login-options"))["items"]
    assert [item["id"] for item in items[:8]] == [
        "facebook", "instagram", "twitter", "youtube", "douyin", "bilibili", "xiaohongshu", "tiktok",
    ]
    assert "github" not in {item["id"] for item in items}
    assert len(items) == 175
    assert all(not item["available"] and not item["authentication_verified"] for item in items)
    qr_items = [item for item in items if item["id"] in {"xiaohongshu", "douyin", "bilibili"}]
    assert all(item["qr_supported"] for item in qr_items)
    assert all(item["reason_code"] == "login_resources_unavailable" for item in qr_items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("missing_rule", "login_rule_missing"),
        ("unknown_rule", "login_rule_unavailable"),
        ("commercial_site", "login_rule_site_mismatch"),
        ("wrong_loopback_port", "login_rule_site_mismatch"),
        ("missing_bundle", "runtime_bundle_missing"),
        ("bundle_version", "runtime_bundle_unavailable"),
        ("unpackaged_bundle", "runtime_bundle_unavailable"),
        ("missing_node", "node_missing"),
        ("offline", "node_offline"),
        ("quarantined", "node_not_account_capable"),
        ("not_capable", "node_not_account_capable"),
        ("old_boot", "node_boot_unavailable"),
        ("stopped_boot", "node_boot_unavailable"),
        ("expired_capacity", "capacity_unavailable"),
        ("invalid_capacity", "capacity_unavailable"),
        ("future_capacity", "capacity_unavailable"),
    ],
)
async def test_not_ready_rejects_without_session_or_command(
    client, db_session, login_account, scenario, expected
):
    account = login_account
    node = await db_session.get(EdgeNode, "login-node")
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    if scenario == "missing_rule":
        account.login_rule_id = None
    elif scenario == "unknown_rule":
        account.login_rule_id = "unverified"
    elif scenario == "commercial_site":
        account.site = "xiaohongshu.com"
    elif scenario == "wrong_loopback_port":
        account.site = "http://127.0.0.1:49907"
    elif scenario == "missing_bundle":
        account.runtime_bundle_id = None
    elif scenario == "bundle_version":
        account.runtime_bundle_version = "wrong"
    elif scenario == "unpackaged_bundle":
        bundle = await db_session.get(BrowserRuntimeBundle, "login-bundle")
        bundle.manifest = {"name": "opencli-default", "version": "2", "components": []}
    elif scenario == "missing_node":
        account.node_id = None
    elif scenario == "offline":
        node.status = "offline"
    elif scenario == "quarantined":
        node.quarantined = True
    elif scenario == "not_capable":
        node.account_capable = False
    elif scenario == "old_boot":
        node.boot_id = "not-current"
    elif scenario == "stopped_boot":
        boot = await db_session.get(EdgeNodeBoot, "login-boot-row")
        boot.stopped_at = datetime.now(UTC)
    elif scenario == "expired_capacity":
        capacity.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif scenario == "invalid_capacity":
        capacity.valid = False
    elif scenario == "future_capacity":
        capacity.observed_at = datetime.now(UTC) + timedelta(minutes=1)
    elif scenario == "full":
        capacity.occupied_slots = capacity.slot_limit
    await db_session.flush()
    readiness = data(await client.get(f"{BASE}/{account.id}/login-readiness"))
    assert readiness == {"ready": False, "code": expected, "message": readiness["message"]}
    response = await client.post(
        f"{BASE}/{account.id}/login-sessions",
        json={"purpose": "login", "expected_revision": account.revision},
    )
    assert response.status_code == 409
    assert expected in response.text
    assert await db_session.scalar(select(func.count()).select_from(BrowserLoginSession)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BrowserDurableCommand)) == 0


@pytest.mark.asyncio
async def test_packaged_fixture_can_start_but_unbound_opening_cannot_be_reused(
    client, db_session, login_account
):
    url = f"{BASE}/{login_account.id}"
    assert data(await client.get(f"{url}/login-readiness"))["ready"] is True
    session = data(
        await client.post(
            f"{url}/login-sessions", json={"purpose": "login", "expected_revision": 0}
        )
    )
    assert session["status"] == "opening"
    assert data(await client.get(f"{url}/login-readiness"))["code"] == "session_queued"
    rejected = await client.post(
        f"{url}/login-sessions",
        json={"purpose": "login", "expected_revision": login_account.revision},
    )
    assert rejected.status_code == 201
    assert data(rejected)["id"] == session["id"]
    assert await db_session.scalar(select(func.count()).select_from(BrowserLoginSession)) == 1
    login_account.login_rule_id = None
    await db_session.flush()
    assert data(await client.get(f"{url}/login-readiness"))["code"] == "login_rule_missing"


@pytest.mark.asyncio
async def test_current_session_reuses_its_lease_at_full_capacity(client, db_session, login_account):
    url = f"{BASE}/{login_account.id}"
    created = data(
        await client.post(
            f"{url}/login-sessions", json={"purpose": "login", "expected_revision": 0}
        )
    )
    session = await db_session.get(BrowserLoginSession, created["id"])
    now = datetime.now(UTC)
    session.lease_id = "current-lease"
    session.epoch = 1
    session.tab_id = "1"
    session.frame_id = "0"
    session.document_id = "document-1"
    session.origin = login_account.site
    session.status = "presenting"
    db_session.add(
        BrowserAccountLease(
            id="lease-row",
            workspace_id=login_account.workspace_id,
            account_id=login_account.id,
            node_id="login-node",
            node_boot_id="login-boot",
            lease_id="current-lease",
            epoch=1,
            owner_id="login-owner",
            status="active",
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.occupied_slots = 1
    await db_session.flush()
    assert data(await client.get(f"{url}/login-readiness"))["code"] == "session_reusable"
    reused = data(
        await client.post(
            f"{url}/login-sessions",
            json={"purpose": "login", "expected_revision": login_account.revision},
        )
    )
    assert reused["id"] == session.id
    session.epoch = 2
    await db_session.flush()
    assert data(await client.get(f"{url}/login-readiness"))["ready"] is False


@pytest.mark.asyncio
async def test_readiness_and_login_are_scoped_to_workspace(client, db_session, login_account):
    db_session.add_all(
        [
            Workspace(id="other-login-workspace", name="Other", slug="other-login"),
            WorkspaceMembership(
                id="other-login-member",
                workspace_id="other-login-workspace",
                user_id="login-user",
                role=WorkspaceRole.ADMIN,
            ),
        ]
    )
    await db_session.flush()
    other = f"/api/v1/workspaces/other-login-workspace/browser-accounts/{login_account.id}"
    assert (await client.get(f"{other}/login-readiness")).status_code == 404
    assert (
        await client.post(
            f"{other}/login-sessions", json={"purpose": "login", "expected_revision": 0}
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_read_only_can_inspect_but_cannot_start(client, db_session, login_account):
    membership = await db_session.get(WorkspaceMembership, "login-member")
    membership.role = WorkspaceRole.VIEWER
    await db_session.flush()
    assert (await client.get(f"{BASE}/login-options")).status_code == 200
    assert (await client.get(f"{BASE}/{login_account.id}/login-readiness")).status_code == 200
    assert (
        await client.post(
            f"{BASE}/{login_account.id}/login-sessions",
            json={"purpose": "login", "expected_revision": 0},
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_execution_path_keeps_existing_service_semantics(client, db_session, login_account):
    login_account.login_rule_id = None
    login_account.login_rule_version = None
    login_account.runtime_bundle_id = None
    login_account.runtime_bundle_version = None
    await db_session.flush()
    session = data(
        await client.post(
            f"{BASE}/{login_account.id}/login-sessions",
            json={
                "purpose": "execution",
                "execution_id": "execution-unchanged",
                "expected_revision": 0,
            },
        )
    )
    assert session["purpose"] == "execution"
    assert session["status"] == "opening"


@pytest.mark.asyncio
async def test_new_platform_account_requires_measured_installation_and_auto_binds(
    client, db_session, login_account
):
    request = {
        "workspace_id": login_account.workspace_id,
        "site": "xiaohongshu.com",
        "label": "真实QR预览",
    }
    response = await client.post(BASE, json=request)
    unbound = data(response)
    assert unbound["node_id"] is None
    assert unbound["runtime_bundle_id"] is None
    denied = await client.post(
        f"{BASE}/{unbound['id']}/login-sessions",
        json={"purpose": "login", "expected_revision": unbound["revision"]},
    )
    assert denied.status_code == 409
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.capabilities = {
        "browser_login_bundles": [
            {
                "id": "login-bundle",
                "version": "2",
                "rules": [{"id": "xiaohongshu-qr", "version": "0.1.0"}],
            }
        ]
    }
    await db_session.flush()
    created = data(await client.post(BASE, json=request))
    assigned = data(
        await client.post(
            f"{BASE}/{unbound['id']}/login-sessions",
            json={"purpose": "login", "expected_revision": unbound["revision"]},
        )
    )
    assert assigned["node_id"] == "login-node"
    assert data(await client.get(f"{BASE}/{unbound['id']}"))["runtime_bundle_id"] == "login-bundle"
    assert created["node_id"] == "login-node"
    assert created["runtime_bundle_id"] == "login-bundle"
    assert created["login_rule_id"] == "xiaohongshu-qr"
    assert created["auth_evidence"] == "unknown"
    assert data(await client.get(f"{BASE}/{created['id']}/login-readiness"))["ready"] is True
    xhs = next(item for item in data(await client.get(f"{BASE}/login-options"))["items"] if item["id"] == "xiaohongshu")
    assert xhs["available"] is True and xhs["authentication_verified"] is False
    assert login_account.site == "http://127.0.0.1:49906"
    assert login_account.login_rule_id == "controlled-login-fixture"
    capacity.valid = False
    await db_session.flush()
    assert data(await client.get(f"{BASE}/{created['id']}/login-readiness"))["ready"] is False


@pytest.mark.asyncio
async def test_unverified_platform_is_not_automatically_configured(client, login_account):
    response = await client.post(
        BASE,
        json={"workspace_id": login_account.workspace_id, "site": "douyin.com", "label": "抖音"},
    )
    unbound = data(response)
    assert unbound["node_id"] is None
    assert unbound["runtime_bundle_id"] is None
    denied = await client.post(
        f"{BASE}/{unbound['id']}/login-sessions",
        json={"purpose": "login", "expected_revision": unbound["revision"]},
    )
    assert denied.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["bilibili", "douyin", "xiaohongshu"])
async def test_bundle4_platform_uses_only_matching_measured_installation(
    client, db_session, login_account, platform
):
    bundle = await db_session.get(BrowserRuntimeBundle, "login-bundle")
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.capabilities = {
        "browser_login_bundles": [
            {
                "id": bundle.id,
                "version": "2",
                "rules": [{"id": platform + "-qr", "version": "0.1.0"}],
            }
        ]
    }
    request = {
        "workspace_id": login_account.workspace_id,
        "site": platform + ".com",
        "label": "官方扫码",
    }
    await db_session.flush()
    if platform != "xiaohongshu":
        assert data(await client.post(BASE, json=request))["node_id"] is None
    root = Path(__file__).resolve().parents[2]
    bundle.version = "4"
    bundle.manifest = json.loads(
        (root / "chrome/runtime-bundles/opencli-default/4/manifest.json").read_text()
    )
    capacity.capabilities = {
        "browser_login_bundles": [
            {
                "id": bundle.id,
                "version": "4",
                "rules": [
                    {
                        "id": platform + "-qr",
                        "version": "0.2.0" if platform == "xiaohongshu" else "0.1.0",
                    }
                ],
            }
        ]
    }
    await db_session.flush()
    account = data(await client.post(BASE, json=request))
    assert account["login_rule_id"] == platform + "-qr"
    assert account["auth_evidence"] == "unknown"
    assert data(await client.get(f"{BASE}/{account['id']}/login-readiness"))["ready"] is True


@pytest.mark.asyncio
async def test_official_form_requires_installed_rule_and_never_claims_identity(
    client, db_session, login_account
):
    bundle = await db_session.get(BrowserRuntimeBundle, "login-bundle")
    bundle.version = "4"
    root = Path(__file__).resolve().parents[2]
    bundle.manifest = json.loads(
        (root / "chrome/runtime-bundles/opencli-default/4/manifest.json").read_text()
    )
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    request = {"workspace_id": login_account.workspace_id, "site": "facebook", "label": "Official"}
    await db_session.flush()
    assert data(await client.post(BASE, json=request))["node_id"] is None
    capacity.capabilities = {
        "browser_login_bundles": [
            {
                "id": bundle.id,
                "version": "4",
                "rules": [{"id": "official-facebook", "version": "0.1.0"}],
            }
        ]
    }
    await db_session.flush()
    account = data(await client.post(BASE, json=request))
    assert account["login_rule_id"] == "official-facebook"
    assert account["auth_evidence"] == "unknown"
    assert data(await client.get(f"{BASE}/{account['id']}/login-readiness"))["ready"]
    option = next(
        x for x in data(await client.get(f"{BASE}/login-options"))["items"] if x["id"] == "facebook"
    )
    assert option["available"] and option["interaction_supported"]
    assert not option["qr_supported"] and not option["identity_probe_supported"]

    capacity.occupied_slots = capacity.slot_limit
    await db_session.flush()
    busy_options = data(await client.get(f"{BASE}/login-options"))["items"]
    busy = next(x for x in busy_options if x["id"] == "facebook")
    assert not busy["available"]
    assert busy["reason_code"] == "capacity_full"
    assert "占用" in busy["message"]
    missing = next(x for x in busy_options if x["id"] == "bilibili")
    assert missing["reason_code"] == "login_resources_unavailable"
    blocked = await client.post(BASE, json={**request, "label": "Busy"})
    assert data(blocked)["node_id"] == "login-node"

    capacity.occupied_slots = 0
    await db_session.flush()
    recovered = next(
        x for x in data(await client.get(f"{BASE}/login-options"))["items"] if x["id"] == "facebook"
    )
    assert recovered["available"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site,code",
    [
        ("ones", "platform_configuration_required"),
        ("spotify", "browser_login_entry_unavailable"),
        ("cursor", "browser_login_entry_unavailable"),
    ],
)
async def test_non_fixed_entries_do_not_start_managed_login(client, login_account, site, code):
    response = await client.post(
        BASE, json={"workspace_id": login_account.workspace_id, "site": site, "label": "Not fixed"}
    )
    assert response.status_code == 409
    assert code in response.text


@pytest.mark.asyncio
async def test_full_node_queues_then_claims_and_cancelled_queue_never_runs(
    client, db_session, login_account
):
    from backend.services.browser_account_scheduler import BrowserAccountScheduler, NodeIdentity

    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.occupied_slots = capacity.slot_limit
    await db_session.flush()
    url = f"{BASE}/{login_account.id}"
    queued = data(
        await client.post(
            f"{url}/login-sessions",
            json={"purpose": "login", "expected_revision": login_account.revision},
        )
    )
    assert queued["lease_id"] is None and queued["epoch"] == 0
    assert data(await client.get(f"{url}/login-readiness"))["code"] == "session_queued"
    scheduler = BrowserAccountScheduler()
    identity = NodeIdentity(node_id="login-node", boot_id="login-boot", owner_id="test-owner")
    assert await scheduler.claim(identity, db=db_session) == []
    assert await db_session.scalar(select(func.count()).select_from(BrowserAccountLease)) == 0
    capacity.occupied_slots = 0
    await db_session.flush()
    claims = await scheduler.claim(identity, db=db_session)
    assert len(claims) == 1 and claims[0].session_id == queued["id"]
    assert capacity.occupied_slots == 1


@pytest.mark.asyncio
async def test_cancel_waiting_login_does_not_enqueue_browser_close(
    client, db_session, login_account
):
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.occupied_slots = capacity.slot_limit
    await db_session.flush()
    url = f"{BASE}/{login_account.id}"
    queued = data(
        await client.post(
            f"{url}/login-sessions",
            json={"purpose": "login", "expected_revision": login_account.revision},
        )
    )
    closed = data(
        await client.post(
            f"{url}/login-sessions/{queued['id']}/close",
            headers={"If-Match": str(queued["revision"])},
        )
    )
    assert closed["status"] == "closed"
    command = await db_session.get(BrowserDurableCommand, queued["command_id"])
    assert command.status == "cancelled"
    assert await db_session.scalar(select(func.count()).select_from(BrowserDurableCommand)) == 1
    assert capacity.occupied_slots == capacity.slot_limit


@pytest.mark.asyncio
async def test_queued_login_promotes_to_browser_with_claimable_revision_at_full_capacity(
    client, db_session, login_account
):
    from backend.services.browser_account_scheduler import BrowserAccountScheduler, NodeIdentity

    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.occupied_slots = capacity.slot_limit
    await db_session.flush()
    url = f"{BASE}/{login_account.id}"
    queued = data(
        await client.post(
            f"{url}/login-sessions",
            json={"purpose": "login", "expected_revision": login_account.revision},
        )
    )

    pre_promotion_revision = login_account.revision
    promoted = data(
        await client.post(
            f"{url}/login-sessions",
            json={"purpose": "browser", "expected_revision": login_account.revision},
        )
    )
    command = await db_session.get(BrowserDurableCommand, promoted["command_id"])
    assert promoted["id"] == queued["id"]
    assert promoted["purpose"] == "browser"
    assert command.status == "queued"
    assert command.expected_revision == login_account.revision
    stale = await client.post(
        f"{url}/login-sessions",
        json={"purpose": "browser", "expected_revision": pre_promotion_revision},
    )
    assert stale.status_code == 409
    assert "stale_generation" in stale.text

    capacity.occupied_slots = 0
    claims = await BrowserAccountScheduler().claim(
        NodeIdentity(node_id="login-node", boot_id="login-boot", owner_id="test-owner"),
        db=db_session,
    )
    assert len(claims) == 1
    assert claims[0].expected_revision == login_account.revision


@pytest.mark.asyncio
async def test_new_browser_session_requires_same_runtime_readiness_as_login(
    client, db_session, login_account
):
    login_account.login_rule_id = None
    await db_session.flush()
    response = await client.post(
        f"{BASE}/{login_account.id}/login-sessions",
        json={"purpose": "browser", "expected_revision": login_account.revision},
    )
    assert response.status_code == 409
    assert "login_rule_missing" in response.text
    assert await db_session.scalar(select(func.count()).select_from(BrowserLoginSession)) == 0


@pytest.mark.asyncio
async def test_started_login_with_stale_lease_cannot_promote_to_browser(
    db_session, login_account
):
    session = await browser_account_service.create_login_session(
        db_session,
        login_account.workspace_id,
        login_account.id,
        expected_revision=login_account.revision,
    )
    command = await db_session.get(BrowserDurableCommand, session.command_id)
    command.status = "running"
    session.lease_id = "missing-lease"
    session.epoch = 1
    session.status = "presenting"
    await db_session.flush()

    with pytest.raises(browser_account_service.BrowserAccountError) as caught:
        await browser_account_service.create_login_session(
            db_session,
            login_account.workspace_id,
            login_account.id,
            purpose="browser",
            expected_revision=login_account.revision,
        )

    assert caught.value.code == "lease_lost"
