"""Bounded, deduplicated login observation over an existing authenticated WS."""

import asyncio
import json


async def observe_until_terminal(
    *, current, observe, send, interval=2.0, sleep=asyncio.sleep, initial=None, refresh=None
):
    last = initial
    while True:
        admitted = current()
        if admitted is None:
            return
        continuous = admitted.session.purpose == "browser"
        try:
            observation = await observe(admitted)
        except Exception:
            # A manually controlled browser can temporarily leave the login
            # origin. Keep looking while its fenced browser lease is current;
            # QR login keeps its original bounded failure behavior.
            after = current()
            if not continuous or after is None or after.claim.epoch != admitted.claim.epoch:
                raise
            await sleep(interval)
            continue
        # A close/lease change during a slow DOM observation revokes its result.
        after = current()
        if after is None or after.claim.epoch != admitted.claim.epoch:
            return
        wire = observation.model_dump(mode="json")
        fingerprint = json.dumps(
            {
                key: wire.get(key)
                for key in (
                    "state",
                    "evidence_kind",
                    "browser_session_state",
                    "external_identity",
                    "target",
                    "view_generation",
                    "error_code",
                )
            },
            sort_keys=True,
        )
        if fingerprint != last:
            await send({"type": "login_observation", "observation": wire})
            last = fingerprint
        if not continuous and (
            wire.get("evidence_kind") == "valid" or wire.get("state") in {
            "saving",
            "saved",
            "closed",
            "expired",
            "error",
            }
        ):
            return
        if (
            refresh is not None
            and wire.get("state") == "refreshing"
            and wire.get("error_code") == "auth_required"
            and wire.get("evidence_kind") == "unknown"
        ):
            await refresh(after, observation)
        await sleep(interval)
