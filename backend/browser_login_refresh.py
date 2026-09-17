"""Evidence-triggered refresh of the same leased login tab; never a timer reload."""

import time


class ExpiredQrRefresher:
    """Keep the fixed 30-second / three-attempt budget across document changes."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.attempts = 0
        self.last_attempt = None

    async def refresh(self, *, admission, observation, cdp_endpoint, current):
        from backend.agent_runtime_dispatch import (
            RuntimeInvokeRequest,
            _discover_login_target,
            invoke_script_host,
        )

        if (
            observation.state != "refreshing"
            or observation.error_code != "auth_required"
            or observation.evidence_kind != "unknown"
            or observation.external_identity is not None
        ):
            return False
        now = self.clock()
        if self.attempts >= 3 or (self.last_attempt is not None and now - self.last_attempt < 30):
            return False
        target = observation.target
        discovered = await _discover_login_target(
            cdp_endpoint=cdp_endpoint,
            expected_origin=target.origin,
            rule_id=observation.rule_id,
            rule_version=observation.rule_version,
            expected_tab_id=int(target.tab_id),
        )
        if (
            discovered["tab_id"] != int(target.tab_id)
            or str(discovered["frame_id"]) != str(target.frame_id)
            or str(discovered["document_id"]) != str(target.document_id)
            or discovered["origin"] != target.origin
            or discovered["view_generation"] != observation.view_generation
        ):
            return False
        latest = current()
        if latest is None or (
            latest.claim.command_id,
            latest.claim.session_id,
            latest.claim.epoch,
            latest.claim.node_id,
            latest.claim.boot_id,
        ) != (
            admission.claim.command_id,
            admission.claim.session_id,
            admission.claim.epoch,
            admission.claim.node_id,
            admission.claim.boot_id,
        ):
            return False
        # Count even uncertain attempts: a timeout must not create an unlimited
        # reload loop. The worker re-observes expiration immediately before reload.
        self.attempts += 1
        self.last_attempt = now
        request = RuntimeInvokeRequest(
            runtime="script-host",
            workflow="login.refresh",
            input={
                "session_id": admission.session.session_id,
                "epoch": admission.claim.epoch,
                "rule_id": observation.rule_id,
                "rule_version": observation.rule_version,
                "target": target.model_dump(mode="json")
                | {
                    "tab_id": int(target.tab_id), "frame_id": int(target.frame_id),
                    "view_generation": observation.view_generation,
                },
                "trigger": "qr_expired",
                "expected_view_generation": observation.view_generation,
                "expected_qr_generation": discovered["qr_generation"],
            },
            config={
                "pack": "account-login",
                "action": "login.refresh",
                "tab_id": int(target.tab_id),
            },
        )
        result = await invoke_script_host(request, cdp_endpoint=cdp_endpoint)
        response = result.get("result") if isinstance(result, dict) else None
        return isinstance(response, dict) and response.get("ok") is True
