from __future__ import annotations

import asyncio
import json
import socket
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.browser_account_runtime import (
    AccountRuntimeConfiguration,
    BrowserAccountRuntimeAllocator,
    BrowserRuntimeError,
    session_runtime_registry,
)
from backend.models.browser import BrowserCommandKind
from backend.schemas.browser_account import (
    DurableCommandV1,
    EmptyCommandPayloadV1,
    NodeClaimV1,
    NodeIdentityV1,
    SessionEnvelopeV1,
)


def test_account_paths_follow_configured_persistent_volume(monkeypatch, tmp_path):
    volume = tmp_path / "mounted-account-volume"
    monkeypatch.setenv("RUNTIME_STATE_DIR", str(volume))
    for name in ("ACCOUNT_RUNTIME_ROOT", "ACCOUNT_PROFILE_ROOT", "ACCOUNT_RUNTIME_STATE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    configuration = AccountRuntimeConfiguration.from_environment()
    assert configuration.runtime_root == volume / "sessions"
    assert configuration.profile_root == volume / "profiles"
    assert configuration.state_root == volume / "state"

    override = tmp_path / "separately-mounted-profiles"
    monkeypatch.setenv("ACCOUNT_PROFILE_ROOT", str(override))
    assert AccountRuntimeConfiguration.from_environment().profile_root == override


_RUNTIME_PROCESS = r"""
import json
import os
import signal
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

role = sys.argv[1]
running = True

def stop(*_args):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
if hasattr(signal, "SIGINT"):
    signal.signal(signal.SIGINT, stop)

if role == "bbx-install":
    raise SystemExit(0)

if role == "fail":
    raise SystemExit(17)

if role == "xvfb":
    display = next(arg for arg in sys.argv[2:] if arg.startswith(":"))
    unix_socket = None
    unix_path = f"/tmp/.X11-unix/X{display[1:]}"
    if os.name == "posix":
        os.makedirs("/tmp/.X11-unix", exist_ok=True)
        try:
            os.unlink(unix_path)
        except FileNotFoundError:
            pass
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_socket.bind(unix_path)
        unix_socket.listen(1)
    try:
        while running:
            time.sleep(0.05)
    finally:
        if unix_socket is not None:
            unix_socket.close()
            try:
                os.unlink(unix_path)
            except FileNotFoundError:
                pass
    raise SystemExit(0)

if role == "browser":
    raw = next(arg for arg in sys.argv[2:] if arg.startswith("--remote-debugging-port="))
    port = int(raw.split("=", 1)[1])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (
                json.dumps({"Browser": "TestChromium/1.0"})
                if self.path == "/json/version"
                else "[]"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 0.05
    while running:
        server.handle_request()
    server.server_close()
    raise SystemExit(0)

if role == "daemon":
    if "OPENCLI_DAEMON_PORT" in os.environ:
        raise SystemExit(23)
    port = 19825
else:
    port = int(os.environ["BBX_TCP_PORT"])
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen(4)
listener.settimeout(0.05)
try:
    while running:
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        conn.close()
finally:
    listener.close()
"""


def _configuration(tmp_path: Path, *, browser_role: str = "browser") -> AccountRuntimeConfiguration:
    process_script = tmp_path / "runtime_process.py"
    process_script.write_text(textwrap.dedent(_RUNTIME_PROCESS), encoding="utf-8")

    bundle_root = tmp_path / "bundles"
    bundle_dir = bundle_root / "managed" / "2"
    bridge = bundle_dir / "extensions" / "bridge"
    script_host = bundle_dir / "extensions" / "script-host"
    rules_dir = script_host / "packs" / "account-login"
    bridge.mkdir(parents=True)
    rules_dir.mkdir(parents=True)
    (rules_dir / "rules.json").write_text(
        json.dumps(
            {
                "id": "controlled-login-fixture",
                "version": "1.0.0",
                "allowed_origins": ["http://127.0.0.1:49906"],
                "login_url": "/login",
            }
        ),
        encoding="utf-8",
    )
    manifest = bundle_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "managed",
                "version": "2",
                "components": [
                    {"kind": "extension", "id": "bridge", "path": "extensions/bridge"},
                    {
                        "kind": "extension",
                        "id": "opencli-script-host",
                        "path": "extensions/script-host",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "PasswordManagerEnabled": False,
                "AutofillAddressEnabled": False,
                "AutofillCreditCardEnabled": False,
            }
        ),
        encoding="utf-8",
    )
    extension_id_file = tmp_path / "browser-bridge-extension-id"
    extension_id_file.write_text("abcdefghijklmnopabcdefghijklmnop\n", encoding="utf-8")
    command = (sys.executable, str(process_script))
    for root in (tmp_path / "runtime", tmp_path / "profiles", tmp_path / "state"):
        root.mkdir()
    return AccountRuntimeConfiguration(
        runtime_root=tmp_path / "runtime",
        profile_root=tmp_path / "profiles",
        state_root=tmp_path / "state",
        bundle_root=bundle_root,
        bundle_manifest=manifest,
        bundle_id="bundle-2",
        policy_file=policy,
        xvfb_argv=(*command, "xvfb"),
        browser_argv=(*command, browser_role),
        bbx_argv=(*command, "bbx"),
        bbx_install_argv=(*command, "bbx-install"),
        bbx_extension_id_file=extension_id_file,
        daemon_argv=(*command, "daemon"),
        display_min=510,
        display_max=520,
        port_min=31_000,
        port_max=31_999,
        startup_timeout=5,
        require_linux=False,
    )


def _contracts(
    *, expires_in: float = 30, suffix: str = "1"
) -> tuple[DurableCommandV1, NodeClaimV1, SessionEnvelopeV1, NodeIdentityV1]:
    now = datetime.now(UTC)
    command = DurableCommandV1(
        command_id=f"command-{suffix}",
        workspace_id="workspace-1",
        account_id=f"account-{suffix}",
        node_id="node-1",
        kind=BrowserCommandKind.START_LOGIN,
        idempotency_scope=f"session:session-{suffix}",
        idempotency_key="start",
        epoch=1,
        expected_revision=2,
        available_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=1),
        session_id=f"session-{suffix}",
        payload=EmptyCommandPayloadV1(),
    )
    claim = NodeClaimV1(
        workspace_id="workspace-1",
        account_id=f"account-{suffix}",
        command_id=command.command_id,
        session_id=f"session-{suffix}",
        node_id="node-1",
        boot_id="boot-1",
        epoch=1,
        expected_revision=2,
        claimed_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=expires_in),
    )
    session = SessionEnvelopeV1(
        workspace_id="workspace-1",
        account_id=f"account-{suffix}",
        session_id=f"session-{suffix}",
        node_id="node-1",
        node_boot_id="boot-1",
        lease_id=f"lease-{suffix}",
        epoch=1,
        lease_expires_at=claim.expires_at,
        runtime_bundle_id="bundle-2",
        runtime_bundle_version="2",
        login_rule_id="controlled-login-fixture",
        login_rule_version="1.0.0",
        view_generation=0,
        purpose="login",
        command_id=command.command_id,
        profile_state="new",
    )
    return command, claim, session, NodeIdentityV1(node_id="node-1", boot_id="boot-1")


def _accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_start_login_allocates_one_real_isolated_stack_and_is_idempotent(
    tmp_path: Path,
) -> None:
    allocator = BrowserAccountRuntimeAllocator(_configuration(tmp_path))
    command, claim, session, identity = _contracts()

    first = await allocator.start(
        command=command,
        claim=claim,
        session=session,
        node_identity=identity,
    )
    second = await allocator.start(
        command=command,
        claim=claim,
        session=session,
        node_identity=identity,
    )

    assert second is first
    assert allocator.active_count() == 1
    assert first.binding.daemon_port == 19_825
    assert len({first.binding.cdp_port, first.binding.bbx_port}) == 2
    assert first.binding.display != ":99"
    assert first.binding.profile_dir != first.binding.home_dir
    assert first.binding.profile_dir != first.binding.cache_dir
    assert first.browser_version == "TestChromium/1.0"
    assert (
        session_runtime_registry().resolve(
            session_id=session.session_id,
            node_id=identity.node_id,
            boot_id=identity.boot_id,
            epoch=claim.epoch,
        )
        is first.binding
    )

    assert _accepts(first.binding.cdp_port)
    assert _accepts(first.binding.bbx_port)
    assert _accepts(first.binding.daemon_port)
    await allocator.stop(
        session_id=session.session_id,
        node_id=identity.node_id,
        boot_id=identity.boot_id,
        epoch=claim.epoch,
    )

    assert allocator.active_count() == 0
    assert first.process_supervisor.last_shutdown is not None
    assert first.process_supervisor.last_shutdown.confirmed is True
    assert not _accepts(first.binding.cdp_port)
    assert not _accepts(first.binding.bbx_port)
    assert not _accepts(first.binding.daemon_port)
    assert first.paths.read_state()["state"] == "stopped"
    assert not first.paths.dirty_marker.exists()
    with pytest.raises(BrowserRuntimeError, match="capability_missing"):
        session_runtime_registry().resolve(
            session_id=session.session_id,
            node_id=identity.node_id,
            boot_id=identity.boot_id,
            epoch=claim.epoch,
        )


@pytest.mark.asyncio
async def test_second_account_stack_fails_closed_on_fixed_opencli_transport(
    tmp_path: Path,
) -> None:
    allocator = BrowserAccountRuntimeAllocator(_configuration(tmp_path))
    first_command, first_claim, first_session, identity = _contracts()
    second_command, second_claim, second_session, _ = _contracts(suffix="2")
    first = await allocator.start(
        command=first_command,
        claim=first_claim,
        session=first_session,
        node_identity=identity,
    )

    try:
        with pytest.raises(
            BrowserRuntimeError,
            match="fixed OpenCLI daemon transport is already in use",
        ):
            await allocator.start(
                command=second_command,
                claim=second_claim,
                session=second_session,
                node_identity=identity,
            )
        assert allocator.active_count() == 1
        assert _accepts(first.binding.daemon_port)
    finally:
        await allocator.stop(
            session_id=first_session.session_id,
            node_id=identity.node_id,
            boot_id=identity.boot_id,
            epoch=first_claim.epoch,
        )


@pytest.mark.asyncio
async def test_failed_start_stops_partial_children_and_preserves_dirty_profile(
    tmp_path: Path,
) -> None:
    allocator = BrowserAccountRuntimeAllocator(_configuration(tmp_path, browser_role="fail"))
    command, claim, session, identity = _contracts()

    with pytest.raises(BrowserRuntimeError, match="runtime_process_exited"):
        await allocator.start(
            command=command,
            claim=claim,
            session=session,
            node_identity=identity,
        )

    assert allocator.active_count() == 0
    profile_dirs = list((tmp_path / "profiles" / "workspace-1").glob("*/profile"))
    assert len(profile_dirs) == 1
    state_dirs = list((tmp_path / "state" / "workspace-1").iterdir())
    assert len(state_dirs) == 1
    state = json.loads((state_dirs[0] / "runtime-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "quarantined"
    assert (state_dirs[0] / "dirty.marker").exists()
    with pytest.raises(BrowserRuntimeError, match="capability_missing"):
        session_runtime_registry().resolve(
            session_id=session.session_id,
            node_id=identity.node_id,
            boot_id=identity.boot_id,
            epoch=claim.epoch,
        )


@pytest.mark.asyncio
async def test_lease_loss_stops_stack_before_releasing_profile(tmp_path: Path) -> None:
    allocator = BrowserAccountRuntimeAllocator(_configuration(tmp_path))
    command, claim, session, identity = _contracts(expires_in=0.8)
    running = await allocator.start(
        command=command,
        claim=claim,
        session=session,
        node_identity=identity,
    )
    assert _accepts(running.binding.cdp_port)
    assert _accepts(running.binding.bbx_port)
    assert _accepts(running.binding.daemon_port)

    for _ in range(60):
        if allocator.active_count() == 0:
            break
        await asyncio.sleep(0.05)

    assert allocator.active_count() == 0
    assert running.lease_supervisor.lost.is_set()
    assert running.lease_supervisor.shutdown_evidence is not None
    assert running.lease_supervisor.shutdown_evidence.confirmed is True
    assert not _accepts(running.binding.cdp_port)
    assert not _accepts(running.binding.bbx_port)
    assert not _accepts(running.binding.daemon_port)
    assert running.paths.read_state()["state"] == "stopped"
    assert not running.paths.dirty_marker.exists()


def test_missing_trusted_bundle_id_fails_before_process_start(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    configuration = AccountRuntimeConfiguration(**{**configuration.__dict__, "bundle_id": ""})
    allocator = BrowserAccountRuntimeAllocator(configuration)
    command, claim, session, identity = _contracts()

    async def run() -> None:
        with pytest.raises(BrowserRuntimeError, match="runtime_bundle_unavailable"):
            await allocator.start(
                command=command,
                claim=claim,
                session=session,
                node_identity=identity,
            )

    asyncio.run(run())
    assert allocator.active_count() == 0
