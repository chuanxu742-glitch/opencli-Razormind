"""Exercise one real Linux account-runtime stack in the built agent image.

This harness deliberately uses synthetic durable contracts and the packaged controlled
loopback login rule.  It proves process/runtime protocol behavior only; it never
performs authorization or signs in to a real platform.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend.browser_account_runtime import (
    BrowserAccountRuntimeAllocator,
    BrowserRuntimeError,
)
from backend.models.browser import BrowserCommandKind
from backend.schemas.browser_account import (
    DurableCommandV1,
    EmptyCommandPayloadV1,
    NodeClaimV1,
    NodeIdentityV1,
    SessionEnvelopeV1,
)

_OPENCLI_VERSION = "1.8.7"
_LOGIN_HOST = "127.0.0.1"
_LOGIN_PORT = 49906


class VerificationError(RuntimeError):
    """The container did not prove the managed runtime contract."""


class _LoginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/login":
            self.send_error(404)
            return
        body = b"<!doctype html><title>Controlled runtime fixture</title><form><input name=identity></form>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def controlled_login_site() -> Iterator[None]:
    """Serve the rule's packaged loopback target without recording requests."""
    server = ThreadingHTTPServer((_LOGIN_HOST, _LOGIN_PORT), _LoginHandler)
    server.daemon_threads = True
    task = asyncio.get_running_loop().run_in_executor(None, server.serve_forever)
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        task.cancel()


def _contracts() -> tuple[DurableCommandV1, NodeClaimV1, SessionEnvelopeV1, NodeIdentityV1]:
    now = datetime.now(UTC)
    command = DurableCommandV1(
        command_id="smoke-command",
        workspace_id="smoke-workspace",
        account_id="smoke-account",
        node_id="smoke-node",
        kind=BrowserCommandKind.START_LOGIN,
        idempotency_scope="session:smoke-session",
        idempotency_key="smoke-start",
        epoch=1,
        expected_revision=1,
        available_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
        session_id="smoke-session",
        payload=EmptyCommandPayloadV1(),
    )
    claim = NodeClaimV1(
        workspace_id=command.workspace_id,
        account_id=command.account_id,
        command_id=command.command_id,
        session_id=command.session_id,
        node_id=command.node_id,
        boot_id="smoke-boot",
        epoch=command.epoch,
        expected_revision=command.expected_revision,
        claimed_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=2),
    )
    session = SessionEnvelopeV1(
        workspace_id=command.workspace_id,
        account_id=command.account_id,
        session_id=command.session_id,
        node_id=command.node_id,
        node_boot_id=claim.boot_id,
        lease_id="smoke-lease",
        epoch=claim.epoch,
        lease_expires_at=claim.expires_at,
        runtime_bundle_id="opencli-default",
        runtime_bundle_version="2",
        login_rule_id="controlled-login-fixture",
        login_rule_version="1.0.0",
        view_generation=0,
        purpose="login",
        command_id=command.command_id,
        profile_state="new",
    )
    return command, claim, session, NodeIdentityV1(node_id=claim.node_id, boot_id=claim.boot_id)


def _accepts(port: int) -> bool:
    try:
        with socket.create_connection((_LOGIN_HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def _require_pinned_opencli() -> str:
    completed = subprocess.run(
        ["opencli", "--version"], capture_output=True, check=False, text=True, timeout=20
    )
    version = (completed.stdout + completed.stderr).strip()
    if completed.returncode or _OPENCLI_VERSION not in version:
        raise VerificationError("the image does not contain the pinned OpenCLI runtime")
    return _OPENCLI_VERSION


def _pid_gone(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(0.05)
    return not Path(f"/proc/{pid}").exists()


async def verify() -> dict[str, Any]:
    opencli_version = _require_pinned_opencli()
    with tempfile.TemporaryDirectory(prefix="account-runtime-smoke-") as state_root:
        root = Path(state_root)
        os.environ.update(
            {
                "RUNTIME_STATE_DIR": str(root),
                "ACCOUNT_RUNTIME_ROOT": str(root / "sessions"),
                "ACCOUNT_PROFILE_ROOT": str(root / "profiles"),
                "ACCOUNT_RUNTIME_STATE_ROOT": str(root / "state"),
                "BROWSER_RUNTIME_BUNDLE_ID": "opencli-default",
                "BROWSER_RUNTIME_BUNDLE_ROOT": "/opt/browser-runtime-bundles",
                "BROWSER_RUNTIME_BUNDLE_MANIFEST": "/opt/browser-runtime-bundles/opencli-default/2/manifest.json",
                "CHROMIUM_POLICY_FILE": "/etc/chromium/policies/managed/opencli-account-runtime.json",
                "ACCOUNT_RUNTIME_BROWSER_BIN": "/usr/bin/chromium",
                "ACCOUNT_RUNTIME_OPENCLI_DAEMON_JS": "/usr/lib/node_modules/@jackwener/opencli/dist/src/daemon.js",
                "ACCOUNT_RUNTIME_DISPLAY_MIN": "310",
                "ACCOUNT_RUNTIME_DISPLAY_MAX": "319",
                "ACCOUNT_RUNTIME_PORT_MIN": "41000",
                "ACCOUNT_RUNTIME_PORT_MAX": "41020",
                "ACCOUNT_RUNTIME_STARTUP_TIMEOUT": "60",
            }
        )
        for directory in (root / "sessions", root / "profiles", root / "state"):
            directory.mkdir(mode=0o700)

        command, claim, session, identity = _contracts()
        allocator = BrowserAccountRuntimeAllocator.from_environment()
        with controlled_login_site():
            first = await allocator.start(
                command=command, claim=claim, session=session, node_identity=identity
            )
            try:
                if allocator.active_count() != 1:
                    raise VerificationError("allocator did not retain exactly one live stack")
                if not first.browser_version or "Chrom" not in first.browser_version:
                    raise VerificationError("Chromium CDP did not report a browser version")
                if not all(
                    _accepts(port)
                    for port in (
                        first.binding.cdp_port,
                        first.binding.bbx_port,
                        first.binding.daemon_port,
                    )
                ):
                    raise VerificationError("CDP, Browser Bridge, or OpenCLI daemon is not reachable")
                second_command, second_claim, second_session, _ = _contracts()
                second_command = second_command.model_copy(
                    update={"command_id": "smoke-command-2", "session_id": "smoke-session-2"}
                )
                second_claim = second_claim.model_copy(
                    update={"command_id": second_command.command_id, "session_id": second_command.session_id}
                )
                second_session = second_session.model_copy(
                    update={"command_id": second_command.command_id, "session_id": second_command.session_id}
                )
                try:
                    await allocator.start(
                        command=second_command,
                        claim=second_claim,
                        session=second_session,
                        node_identity=identity,
                    )
                except BrowserRuntimeError as error:
                    if error.code != "capacity_missing":
                        raise
                else:
                    raise VerificationError("second fixed-port stack unexpectedly started")
                if allocator.active_count() != 1 or not _accepts(first.binding.daemon_port):
                    raise VerificationError("rejected second stack harmed the first stack")
                await allocator.stop(
                    session_id=session.session_id,
                    node_id=identity.node_id,
                    boot_id=identity.boot_id,
                    epoch=claim.epoch,
                )
                shutdown = first.process_supervisor.last_shutdown
                if shutdown is None or not shutdown.confirmed:
                    raise VerificationError("runtime stop was not process-tree confirmed")
                if any(not _pid_gone(pid) for pid in shutdown.requested_pids):
                    raise VerificationError("confirmed stop left a registered child process")
                if any(
                    _accepts(port)
                    for port in (
                        first.binding.cdp_port,
                        first.binding.bbx_port,
                        first.binding.daemon_port,
                    )
                ):
                    raise VerificationError("confirmed stop left an account-runtime port open")
                if allocator.active_count() != 0:
                    raise VerificationError("allocator retained a stopped stack")
            finally:
                await allocator.close_all()
    return {
        "ready": True,
        "evidence_scope": "synthetic contracts; loopback controlled login rule; no authorization or real platform login",
        "opencli_version": opencli_version,
        "chromium_cdp": True,
        "bbx_tcp_connection": True,
        "fixed_daemon_port": 19825,
        "second_same_container_rejected": True,
        "stop_freed_processes_and_ports": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        report = asyncio.run(verify())
    except (BrowserRuntimeError, OSError, subprocess.SubprocessError, VerificationError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
