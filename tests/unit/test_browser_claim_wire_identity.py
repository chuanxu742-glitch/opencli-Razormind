"""Exercise claim wire construction through the real database service."""

from datetime import UTC, datetime, timedelta

import pytest

from backend.models.browser import BrowserAccount, BrowserLoginSession
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.models.identity import Workspace
from backend.schemas.browser_account import DurableCommandV1, NodeClaimV1
from backend.services.browser_account_scheduler import BrowserAccountScheduler, NodeIdentity


@pytest.mark.asyncio
async def test_new_reused_and_renewed_claims_carry_durable_account_identity(db_session):
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=5)
    db_session.add(Workspace(id="claim-w", name="Claims", slug="claims"))
    db_session.add(
        EdgeNode(
            id="claim-n",
            url="https://claim-node.test",
            boot_id="boot",
            status="online",
            account_capable=True,
        )
    )
    await db_session.flush()
    db_session.add_all(
        [
            EdgeNodeBoot(node_id="claim-n", boot_id="boot", started_at=now),
            EdgeNodeCapacity(
                node_id="claim-n",
                boot_id="boot",
                slot_limit=2,
                occupied_slots=0,
                disk_available=1000,
                observed_at=now,
                expires_at=expires,
            ),
            BrowserAccount(
                id="claim-a",
                workspace_id="claim-w",
                site="fixture.test",
                label="Claim",
                node_id="claim-n",
            ),
        ]
    )
    await db_session.flush()
    db_session.add(
        BrowserLoginSession(
            id="claim-s", workspace_id="claim-w", account_id="claim-a", expires_at=expires
        )
    )
    await db_session.flush()
    scheduler = BrowserAccountScheduler(clock=lambda: now)
    identity = NodeIdentity(node_id="claim-n", boot_id="boot", owner_id="owner")
    for number in (1, 2):
        await scheduler.enqueue(
            db_session,
            DurableCommandV1(
                command_id=f"claim-{number}",
                workspace_id="claim-w",
                account_id="claim-a",
                node_id="claim-n",
                session_id="claim-s",
                kind="start_login",
                idempotency_scope="claim-test",
                idempotency_key=str(number),
                epoch=0,
                expected_revision=0,
                available_at=now,
                expires_at=expires,
            ),
        )
        claims = await scheduler.claim(identity, limit=1, db=db_session)
        assert len(claims) == 1
        claim = claims[0]
        assert (claim.workspace_id, claim.account_id) == ("claim-w", "claim-a")
        assert NodeClaimV1.from_wire(claim.to_wire()) == claim
        renewed = await scheduler.renew(identity, claim, db=db_session)
        assert (renewed.workspace_id, renewed.account_id) == ("claim-w", "claim-a")
        assert renewed.command_id == f"claim-{number}"
