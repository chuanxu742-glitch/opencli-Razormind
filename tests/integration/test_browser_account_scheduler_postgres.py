from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.models.browser import (
    BrowserAccount,
    BrowserDurableCommand,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.models.identity import Workspace
from backend.schemas.browser_account import BrowserAccountCreate
from backend.services import browser_account_service
from backend.services.browser_account_scheduler import BrowserAccountScheduler, NodeIdentity
from tests.postgres_conformance import temporary_postgres_database

_ROOT = Path(__file__).parents[2]


def _upgrade(database_url: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SQLALCHEMY_DATABASE_URI": database_url,
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.asyncio
@pytest.mark.postgres_conformance
async def test_claim_skips_account_locked_by_queued_promotion_then_reloads_revision():
    async with temporary_postgres_database("browser_scheduler_lock_order") as database_url:
        upgraded = await asyncio.to_thread(_upgrade, database_url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as setup:
                setup.add(Workspace(id="pg-lock-w", name="PG lock", slug="pg-lock"))
                await setup.flush()
                bundle = await setup.scalar(
                    select(BrowserRuntimeBundle).where(
                        BrowserRuntimeBundle.name == "opencli-default",
                        BrowserRuntimeBundle.version == "2",
                    )
                )
                assert bundle is not None
                now = datetime.now(UTC)
                setup.add(
                    EdgeNode(
                        id="login-node",
                        url="http://127.0.0.1:19823",
                        status="online",
                        account_capable=True,
                        boot_id="login-boot",
                    )
                )
                await setup.flush()
                setup.add_all(
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
                await setup.flush()
                account = await browser_account_service.create_browser_account(
                    setup,
                    "pg-lock-w",
                    BrowserAccountCreate(
                        workspace_id="pg-lock-w",
                        label="PG lock",
                        site="http://127.0.0.1:49906",
                        node_id="login-node",
                        runtime_bundle_id=bundle.id,
                        login_rule_id="controlled-login-fixture",
                        login_rule_version="1.0.0",
                    ),
                )
                session = await browser_account_service.create_login_session(
                    setup,
                    "pg-lock-w",
                    account.id,
                    expected_revision=account.revision,
                )
                command_id = session.command_id
                await setup.commit()

            holder = factory()
            await holder.begin()
            locked_account = await holder.scalar(
                select(BrowserAccount)
                .where(BrowserAccount.id == account.id)
                .with_for_update()
            )
            assert locked_account is not None

            async def claim_once():
                async with factory() as contender:
                    claims = await BrowserAccountScheduler().claim(
                        NodeIdentity(
                            node_id="login-node",
                            boot_id="login-boot",
                            owner_id="pg-owner",
                        ),
                        db=contender,
                    )
                    await contender.commit()
                    return claims

            # The allocator owns the command first. SKIP LOCKED on the account
            # must make this poll finish while the independent API transaction
            # still owns the account row.
            assert await asyncio.wait_for(claim_once(), timeout=1.5) == []

            command = await holder.get(
                BrowserDurableCommand, command_id, with_for_update=True
            )
            locked_account.revision += 1
            command.expected_revision = locked_account.revision
            await holder.commit()
            await holder.close()

            claims = await claim_once()
            assert len(claims) == 1
            assert claims[0].command_id == command_id
            assert claims[0].expected_revision == locked_account.revision
        finally:
            await engine.dispose()
