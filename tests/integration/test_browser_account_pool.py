import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.models.browser import BrowserDurableCommand
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.services.browser_account_pool import DockerAccountPool, pool_node_blocked
from tests.integration.test_browser_account_login_readiness import BASE, data
from tests.integration.test_browser_account_login_readiness import login_account as login_account


@pytest.fixture
def pool(tmp_path, db_session, monkeypatch):
    import json
    from contextlib import asynccontextmanager

    cfg = dict(
        image="sha256:" + "a" * 64,
        max_running=2,
        idle_seconds=30,
        advertise_base="https://localhost/pool",
        state_dir=str(tmp_path),
        env_template=str(tmp_path / "node.env"),
        trust_file=str(tmp_path / "trust.pem"),
        ca_file=str(tmp_path / "ca.pem"),
        docker=str(tmp_path / "docker"),
        network="test",
        bundle_id="login-bundle",
    )
    name = tmp_path / "config.json"
    name.write_text(json.dumps(cfg))
    monkeypatch.setenv("ACCOUNT_NODE_POOL_CONFIG", str(name))

    @asynccontextmanager
    async def factory():
        yield db_session

    return DockerAccountPool(cfg, session_factory=factory)


async def destination(db, pool):
    key = "b" * 32
    node_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    now = datetime.now(UTC)
    db.add(
        EdgeNode(
            id=node_id,
            url="https://localhost/new",
            status="online",
            account_capable=True,
            boot_id="new-boot",
        )
    )
    await db.flush()
    db.add_all(
        [
            EdgeNodeBoot(
                id="new-boot-row",
                node_id=node_id,
                boot_id="new-boot",
                status="active",
                started_at=now - timedelta(seconds=10),
            ),
            EdgeNodeCapacity(
                id="new-cap",
                node_id=node_id,
                boot_id="new-boot",
                valid=True,
                slot_limit=1,
                occupied_slots=0,
                disk_available=1000000,
                observed_at=now,
                expires_at=now + timedelta(minutes=5),
                capabilities={
                    "browser_login_bundles": [
                        {
                            "id": "login-bundle",
                            "version": "2",
                            "rules": [{"id": "controlled-login-fixture", "version": "1.0.0"}],
                        }
                    ]
                },
            ),
        ]
    )
    await db.flush()
    item = dict(key=key, node_id=node_id, phase="ready", boot_id="new-boot", container_id="c" * 64)
    pool.items.append(item)
    pool.save()
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["login", "browser"])
async def test_pool_moves_only_unstarted_queue_atomically(
    client, db_session, login_account, pool, purpose
):
    capacity = await db_session.get(EdgeNodeCapacity, "login-capacity")
    capacity.occupied_slots = 1
    await db_session.flush()
    queued = data(
        await client.post(
            f"{BASE}/{login_account.id}/login-sessions",
            json={"purpose": purpose, "expected_revision": 0},
        )
    )
    item = await destination(db_session, pool)
    item["demand"] = queued["command_id"]
    assert await pool.bind(item)
    account = data(await client.get(f"{BASE}/{login_account.id}"))
    session = data(await client.get(f"{BASE}/{login_account.id}/login-sessions/{queued['id']}"))
    command = await db_session.get(BrowserDurableCommand, queued["command_id"])
    assert account["node_id"] == session["node_id"] == command.node_id == item["node_id"]
    assert session["node_boot_id"] == "new-boot"
    assert command.expected_revision == account["revision"]
    assert session["epoch"] == 0 and session["lease_id"] is None and command.status == "queued"


@pytest.mark.asyncio
async def test_pool_does_not_move_profile_account(client, db_session, login_account, pool):
    queued = data(
        await client.post(
            f"{BASE}/{login_account.id}/login-sessions",
            json={"purpose": "login", "expected_revision": 0},
        )
    )
    item = await destination(db_session, pool)
    item["demand"] = queued["command_id"]
    login_account.profile_id = "retained-profile"
    await db_session.flush()
    assert await pool.bind(item)
    assert login_account.node_id == "login-node"


@pytest.mark.asyncio
async def test_idle_guard_persists_before_stop_and_preserves_container(db_session, pool):
    item = await destination(db_session, pool)
    pool.idle_since[item["node_id"]] = time.monotonic() - 31
    pool.inspect = AsyncMock(return_value={"State": {"Running": False}})

    async def docker(*args):
        assert args[0] == "stop" and args[-1] == item["container_id"]
        assert pool_node_blocked(item["node_id"])
        return ""

    pool.docker = docker
    assert await pool.idle(item)
    assert item["phase"] == "stopped" and item["container_id"] == "c" * 64
    assert pool_node_blocked(item["node_id"])


@pytest.mark.asyncio
async def test_stale_capacity_never_causes_idle_stop(db_session, pool):
    item = await destination(db_session, pool)
    cap = await db_session.get(EdgeNodeCapacity, "new-cap")
    cap.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    pool.idle_since[item["node_id"]] = time.monotonic() - 60
    pool.docker = AsyncMock()
    assert not await pool.idle(item)
    pool.docker.assert_not_called()


@pytest.mark.asyncio
async def test_busy_capacity_never_causes_idle_stop(db_session, pool):
    item = await destination(db_session, pool)
    cap = await db_session.get(EdgeNodeCapacity, "new-cap")
    cap.occupied_slots = 1
    await db_session.flush()
    pool.idle_since[item["node_id"]] = time.monotonic() - 60
    pool.docker = AsyncMock()
    assert not await pool.idle(item)
    pool.docker.assert_not_called()


@pytest.mark.asyncio
async def test_container_replacement_is_rejected(pool):
    item = dict(key="b" * 32, node_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", container_id="c" * 64)
    import json

    pool.docker = AsyncMock(
        return_value=json.dumps(
            [
                {
                    "Id": "d" * 64,
                    "Config": {
                        "Labels": {
                            "opencli.pool.node": item["node_id"],
                            "opencli.pool.root": pool.config["state_dir"],
                        }
                    },
                }
            ]
        )
    )
    with pytest.raises(RuntimeError, match="replaced"):
        await pool.inspect(item)


@pytest.mark.asyncio
async def test_pool_container_uses_init_reaper(pool):
    item = {
        "key": "d" * 32,
        "node_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "phase": "creating",
    }
    env_path = Path(pool.config["state_dir"]) / (item["key"] + ".env")
    env_path.write_text("AGENT_NODE_ID=test\n", encoding="utf-8")
    pool.items.append(item)
    pool.start = AsyncMock()
    pool.docker = AsyncMock(side_effect=["", "e" * 64])

    await pool.ensure_created(item)

    create_args = pool.docker.await_args_list[1].args
    assert create_args[:3] == ("create", "--init", "--name")


@pytest.mark.asyncio
async def test_starting_ignores_fresh_previous_boot(db_session, pool):
    item = await destination(db_session, pool)
    item.update(phase="starting", previous_boot="new-boot")
    pool.bind = AsyncMock(return_value=True)
    pool.inspect = AsyncMock(return_value={"State": {"Running": True}})
    await pool.reconcile(item)
    assert item["phase"] == "starting"
    pool.bind.assert_not_called()


@pytest.mark.asyncio
async def test_ready_pool_reconfirms_a_fresh_restarted_boot(db_session, pool):
    item = await destination(db_session, pool)
    item["boot_id"] = "old-boot"
    pool.idle_since[item["node_id"]] = time.monotonic() - 60
    pool.inspect = AsyncMock(return_value={"State": {"Running": True}})
    pool.bind = AsyncMock(return_value=True)

    await pool.reconcile(item)

    assert item["phase"] == "ready"
    assert item["boot_id"] == "new-boot"
    assert item["node_id"] not in pool.idle_since
    pool.bind.assert_awaited_once_with(item)


@pytest.mark.asyncio
async def test_one_failed_container_does_not_block_other_reconciliation(db_session, pool):
    first = await destination(db_session, pool)
    second = {**first, "key": "a" * 32, "node_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
    pool.items.append(second)
    pool.reconcile = AsyncMock(side_effect=[RuntimeError("failed"), None])
    await pool.tick()
    assert pool.reconcile.await_count == 2


@pytest.mark.asyncio
async def test_new_account_on_full_managed_node_can_expand(client, db_session, login_account, pool):
    queued = data(
        await client.post(
            f"{BASE}/{login_account.id}/login-sessions",
            json={"purpose": "login", "expected_revision": 0},
        )
    )
    item = await destination(db_session, pool)
    item["demand"] = queued["command_id"]
    await pool.bind(item)
    item["demand"] = None
    cap = await db_session.get(EdgeNodeCapacity, "new-cap")
    cap.occupied_slots = 1
    await db_session.flush()
    pool.reconcile = AsyncMock()
    pool.create = AsyncMock()
    await pool.tick()
    pool.create.assert_awaited_once_with(queued["command_id"])


@pytest.mark.asyncio
async def test_starting_retries_failed_docker_start_without_overwriting_boot(db_session, pool):
    item = await destination(db_session, pool)
    item.update(phase="starting", previous_boot="original-boot")
    pool.inspect = AsyncMock(return_value={"State": {"Running": False}})
    pool.docker = AsyncMock(return_value="")
    await pool.reconcile(item)
    pool.docker.assert_awaited_once_with("start", item["container_id"])
    assert item["previous_boot"] == "original-boot" and item["phase"] == "starting"


@pytest.mark.asyncio
async def test_recover_running_creation_does_not_set_current_boot_as_previous(db_session, pool):
    item = await destination(db_session, pool)
    item.update(phase="creating", previous_boot=None)
    pool.inspect = AsyncMock(return_value={"State": {"Running": True}})
    pool.docker = AsyncMock()
    await pool.start(item)
    assert item["previous_boot"] is None and item["phase"] == "starting"
    pool.docker.assert_not_called()
