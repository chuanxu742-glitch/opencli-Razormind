"""Durable resources for the actual packaged loopback login fixture."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.models.browser import BrowserRuntimeBundle
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity


async def seed_login_resources(db):
    now = datetime.now(UTC)
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "chrome/runtime-bundles/opencli-default/2/manifest.json"
        ).read_text(encoding="utf-8")
    )
    db.add_all(
        [
            BrowserRuntimeBundle(
                id="login-bundle", name="opencli-default", version="2", manifest=manifest
            ),
            EdgeNode(
                id="login-node",
                url="http://127.0.0.1:19823",
                status="online",
                account_capable=True,
                boot_id="login-boot",
            ),
        ]
    )
    await db.flush()
    db.add_all(
        [
            EdgeNodeBoot(
                id="login-boot-row",
                node_id="login-node",
                boot_id="login-boot",
                status="active",
                started_at=now - timedelta(seconds=10),
            ),
            EdgeNodeCapacity(
                id="login-capacity",
                node_id="login-node",
                boot_id="login-boot",
                slot_limit=1,
                occupied_slots=0,
                disk_available=1_000_000,
                observed_at=now,
                expires_at=now + timedelta(minutes=5),
                valid=True,
            ),
        ]
    )
    await db.flush()
    return {
        "site": "http://127.0.0.1:49906",
        "node_id": "login-node",
        "runtime_bundle_id": "login-bundle",
        "login_rule_id": "controlled-login-fixture",
        "login_rule_version": "1.0.0",
    }
