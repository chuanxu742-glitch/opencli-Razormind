"""OpenCLI Agent Server — runs on LAN/NAT edge nodes.

Accepts HTTP POST /collect requests from the center API, executes opencli
locally (pointing at the node's own Chrome instance), and returns results.

Registration modes (AGENT_REGISTER):
  http  — LAN mode: agent POSTs its URL to center; center calls back via HTTP.
           Requires the agent to be reachable from the center.
  ws    — NAT/reverse-channel mode: agent initiates a persistent WebSocket to
           the center's /api/v1/nodes/ws endpoint.  The center pushes
           collect tasks down the WS connection; agent returns results in-band.
           Use this when the center cannot reach the agent (NAT, firewall, etc.).
  off   — Disable auto-registration entirely.

Usage on the edge node:
    pip install fastapi uvicorn httpx pyyaml websockets
    python -m backend.agent_server
    # or standalone:
    uvicorn backend.agent_server:app --host 0.0.0.0 --port 19823

Environment variables:
    AGENT_PORT              HTTP port to listen on (default: 19823)
    AGENT_ADVERTISE_URL     Canonical URL the center uses to identify this agent
                            (default: auto-detected from outbound IP)
    AGENT_MODE              Collection mode reported to center: bridge | cdp (default: bridge)
    AGENT_LABEL             Human-readable label for this agent (default: hostname)
    AGENT_REGISTER          Registration mode: http | ws | off (default: http)
    CENTRAL_API_URL         Center API base URL for self-registration
                            e.g. http://192.168.1.1:8031
                            Leave empty to skip auto-registration.
    HTTP_PROXY              HTTP proxy for outbound requests (agent → center)
    HTTPS_PROXY             HTTPS proxy for outbound requests (agent → center)
    AGENT_API_TOKEN         Fleet auth bearer token (ADR-0005) attached to both the
                            HTTP register call and the WS reverse-channel handshake.
                            Preferred name for this process; takes priority.
    API_AUTH_TOKEN          Fallback token env var — a node sharing the center's
                            process environment (e.g. same .env) just works without
                            a separate AGENT_API_TOKEN. Ignored if AGENT_API_TOKEN is set.
    OPENCLI_BRIDGE_BIN      Path to opencli 1.0 binary (default: /opt/opencli-bridge/bin/opencli)
    OPENCLI_CDP_ENDPOINT    Explicit anonymous Chrome CDP endpoint; unset means capability_missing
    OPENCLI_DAEMON_PORT     Bridge daemon port (default: 19825)
    OPENCLI_TIMEOUT         opencli subprocess timeout in seconds (default: 120)
    AGENT_CODEX_ISOLATED_RUNNER Absolute path to an administrator-owned, externally
                            isolated Codex-compatible runner. Direct CLI execution
                            is disabled.
    AGENT_CODEX_ALLOWED_ROOTS JSON array of server-owned roots allowed for Codex cwd.
                            Empty or invalid configuration disables Codex dispatch.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from urllib.request import proxy_bypass

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from backend.agent_runtime_dispatch import (
    AccountRuntimeContext,
    RuntimeInvokeRequest,
    apply_portal_control,
    capture_portal_frame,
    cleanup_cdp_tabs,
    invoke_runtime,
    observe_login_runtime,
    parse_output,
    prepare_portal_route,
    resolve_account_runtime_context,
    resolve_portal_target,
    snapshot_tab_ids,
)

# Imported directly from the registry submodule (not the `backend.agent_runtimes`
# package __init__) so this module's import graph is pinned to what registry.py
# itself pulls in — stdlib only, verified against pi_adapter.py's imports (also
# stdlib-only). agent_server.py runs standalone on edge nodes with a minimal
# dependency set (see module docstring); this avoids depending on the package
# __init__ staying lightweight as more adapters are added later.
from backend.agent_runtimes.base import AgentTask, RuntimeInvocationError
from backend.agent_runtimes.registry import (
    available_runtime_capabilities,
    available_runtimes,
    get_runtime,
)
from backend.browser_account_runtime import (
    BrowserRuntimeError,
    EpochStore,
    PortalRuntimeRegistration,
    ProfileRuntimePaths,
    RuntimeLeaseAdmission,
    account_runtime_allocator,
    close_account_runtime_allocator,
    runtime_lease_book,
    session_portal_registry,
    session_runtime_registry,
)
from backend.schemas.browser_account import (
    DurableCommandV1,
    NodeClaimV1,
    NodeIdentityV1,
    NodeResultV1,
    PortalOwnerRouteV1,
    PortalWireFrameV1,
    SessionEnvelopeV1,
)
from backend.services.browser_portal_contract import (
    decode_portal_wire_frame,
    encode_portal_wire_frame,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("agent_server")

_OPENCLI_BIN = os.environ.get("OPENCLI_BIN") or "opencli"


def _resolve_bin(mode: str) -> str:  # noqa: ARG001
    configured = _OPENCLI_BIN or "opencli"
    if os.path.isabs(configured) or os.path.dirname(configured):
        return configured
    if os.name == "nt" and not os.path.splitext(configured)[1]:
        for suffix in (".cmd", ".bat", ".exe", ".ps1"):
            resolved = shutil.which(f"{configured}{suffix}")
            if resolved:
                return resolved
    return shutil.which(configured) or configured


_DEFAULT_CDP = os.environ.get("OPENCLI_CDP_ENDPOINT", "")
_BROWSER_PROFILE_KIND = os.environ.get("OPENCLI_BROWSER_PROFILE_KIND", "authenticated")
_DAEMON_PORT = int(os.environ.get("OPENCLI_DAEMON_PORT", "19825"))
_AGENT_PORT = int(os.environ.get("AGENT_PORT", "19823"))
_CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "").rstrip("/")
_AGENT_ADVERTISE_URL = os.environ.get("AGENT_ADVERTISE_URL", "")
_AGENT_MODE = os.environ.get("AGENT_MODE", "cdp")
# Deployment/startup type reported to center:
# "docker" (container) | "shell" (native process).
_AGENT_DEPLOY_TYPE = os.environ.get("AGENT_DEPLOY_TYPE", "docker")
# Anonymous collection may use an explicitly supplied endpoint; account
# dispatch never reaches this legacy host-remapping path.
_AGENT_HAS_CHROME = os.environ.get("AGENT_HAS_CHROME", "false").lower() == "true"
_RUNTIME_BUNDLE_MANIFEST = os.environ.get(
    "BROWSER_RUNTIME_BUNDLE_MANIFEST",
    "/opt/browser-runtime-bundles/opencli-default/1/manifest.json",
)


def _bundle_declared_runtimes() -> set[str]:
    try:
        with open(_RUNTIME_BUNDLE_MANIFEST, encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list):
        return set()
    declared: set[str] = set()
    for capability in capabilities:
        if not isinstance(capability, dict):
            continue
        runtime = capability.get("runtime", "opentabs")
        if isinstance(runtime, str):
            declared.add(runtime)
    return declared


def _available_agent_runtimes() -> list[str]:
    runtimes = list(available_runtimes())
    declared = _bundle_declared_runtimes()
    if "script-host" in declared and "script-host" not in runtimes:
        runtimes.append("script-host")
    return runtimes


_EDGE_RUNTIME_TASK_CONFIG_KEYS = frozenset(
    {
        "action",
        "agent_id",
        "base_delay",
        "breaker_threshold",
        "chrome",
        "input_wait_seconds",
        "local",
        "max_attempts",
        "model",
        "pack",
        "permission_mode",
        "plugin",
        "poll_interval",
        "provider",
        "response_timeout_seconds",
        "settle_seconds",
        "suggested_wait_seconds",
        "tab_id",
        "timeout_seconds",
    }
)


_AGENT_LABEL = os.environ.get("AGENT_LABEL", socket.gethostname())
# Registration mode:
#   http — LAN mode: agent POSTs its URL to center, center calls back via HTTP (default)
#   ws   — NAT/reverse-channel mode: agent opens WS to center, then
#          registers through the WS handshake.
#   off  — disable auto-registration entirely
_AGENT_REGISTER = os.environ.get("AGENT_REGISTER", "http").lower()
# opencli subprocess execution timeout in seconds
_OPENCLI_TIMEOUT = int(os.environ.get("OPENCLI_TIMEOUT", "120"))
# Outbound proxy for agent → center communication (optional)
_HTTP_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
_AGENT_NODE_ID = os.environ.get("AGENT_NODE_ID", "").strip()
_AGENT_NODE_CREDENTIAL_ID = os.environ.get("AGENT_NODE_CREDENTIAL_ID", "").strip()
_AGENT_NODE_CREDENTIAL = os.environ.get("AGENT_NODE_CREDENTIAL", "")
_AGENT_BOOT_ID = os.environ.get("AGENT_BOOT_ID", "").strip()
_NODE_IDENTITY: NodeIdentityV1 | None = None


def _node_identity(*, required: bool = True) -> NodeIdentityV1 | None:
    """Return the persistent node identity for this process generation."""

    global _NODE_IDENTITY
    if _NODE_IDENTITY is not None:
        return _NODE_IDENTITY
    if not _AGENT_NODE_ID:
        if required:
            raise HTTPException(status_code=503, detail="node identity is not configured")
        return None
    try:
        identity_root = os.environ.get(
            "ACCOUNT_RUNTIME_STATE_ROOT",
            "/var/lib/opencli-account-runtime/state",
        )
        paths = ProfileRuntimePaths.from_profile_dir(
            os.path.join(identity_root, "node-identity-profile"),
            os.path.join(identity_root, "node-identity"),
        )
        boot_id = _AGENT_BOOT_ID or EpochStore(paths).begin_boot()
        _NODE_IDENTITY = NodeIdentityV1(node_id=_AGENT_NODE_ID, boot_id=boot_id)
    except (BrowserRuntimeError, ValueError) as exc:
        if required:
            raise HTTPException(status_code=503, detail="node identity is unavailable") from exc
        return None
    return _NODE_IDENTITY


def _account_runtime_prerequisite(
    agent_url: str | None = None,
) -> tuple[bool, str | None]:
    if not (
        _AGENT_HAS_CHROME
        and _BROWSER_PROFILE_KIND == "authenticated"
        and _AGENT_NODE_ID
        and _AGENT_NODE_CREDENTIAL_ID
        and _AGENT_NODE_CREDENTIAL
    ):
        return False, "capability_missing"
    if not _CENTRAL_API_URL.startswith("https://") or (
        agent_url is not None and not agent_url.startswith("https://")
    ):
        return False, "portal_tls_required"
    try:
        account_runtime_allocator().configuration.validate()
    except BrowserRuntimeError as exc:
        return False, exc.code
    return True, None


def _require_node_auth(
    authorization: str | None,
    node_id: str | None,
    boot_id: str | None,
    credential: str | None,
) -> NodeIdentityV1:
    """Authenticate a real node credential and exact boot generation."""
    if not _AGENT_HAS_CHROME or _BROWSER_PROFILE_KIND != "authenticated":
        raise HTTPException(status_code=503, detail="account runtime capability is unavailable")
    if not _AGENT_NODE_CREDENTIAL or not _AGENT_NODE_CREDENTIAL_ID:
        raise HTTPException(status_code=503, detail="account node credential is not configured")
    identity = _node_identity()
    if (
        not credential
        or not hmac.compare_digest(credential, _AGENT_NODE_CREDENTIAL)
        or node_id != identity.node_id
        or boot_id != identity.boot_id
    ):
        raise HTTPException(status_code=401, detail="invalid node credential identity")
    # Global fleet auth remains an independent center-to-node admission gate.
    _require_collect_auth(authorization)
    return identity


_HTTPS_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
# Fleet auth token (ADR-0005): AGENT_API_TOKEN preferred, API_AUTH_TOKEN accepted
# as a fallback for nodes that share the center's environment.
_AGENT_API_TOKEN = os.environ.get("AGENT_API_TOKEN") or os.environ.get("API_AUTH_TOKEN") or ""
_OHMYOPENCLI_ROOT = os.environ.get("OHMYOPENCLI_ROOT", "/opt/ohmyopencli")
_ACTIVE_COLLECTS: dict[str, asyncio.subprocess.Process] = {}


def _auth_headers() -> dict[str, str]:
    """Bearer-auth header for center requests, or {} when no token is configured.

    Reads the module global at call time (rather than closing over it) so
    tests can monkeypatch backend.agent_server._AGENT_API_TOKEN directly.
    """
    if not _AGENT_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {_AGENT_API_TOKEN}"}


def _node_headers() -> dict[str, str]:
    identity = _node_identity(required=False)
    if identity is None or not _AGENT_NODE_CREDENTIAL:
        return {}
    return {
        "X-Node-ID": identity.node_id,
        "X-Node-Boot-ID": identity.boot_id,
        "X-Node-Credential-ID": _AGENT_NODE_CREDENTIAL_ID,
        "X-Node-Credential": _AGENT_NODE_CREDENTIAL,
    }


def _require_collect_auth(authorization: str | None) -> None:
    """Fail closed when the edge node has no inbound fleet secret."""
    import hmac

    expected = f"Bearer {_AGENT_API_TOKEN}" if _AGENT_API_TOKEN else ""
    if not expected or not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or unconfigured agent bearer token")


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
    except (OSError, ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _runtime_lineage(bin_path: str) -> dict[str, str]:
    """Measure the binaries/source used by this node; never echo declarations."""

    async def output(*argv: str, cwd: str | None = None) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
            )
            stdout, _ = await proc.communicate()
            return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
        except (OSError, ValueError):
            return ""

    repo_commit = await output("git", "rev-parse", "HEAD", cwd=_OHMYOPENCLI_ROOT)
    source_commit = await output(
        "git",
        "log",
        "-1",
        "--format=%H",
        "--",
        "adapters/official-site/observe.js",
        cwd=_OHMYOPENCLI_ROOT,
    )
    version_text = await output(bin_path, "--version")
    match = re.search(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version_text)
    return {
        "ohmyopencli_repo_commit": repo_commit,
        "capability_source_commit": source_commit,
        "opencli_version": match.group(0) if match else version_text,
    }


def _detect_advertise_url() -> str:
    """Auto-detect the IP this node would use to reach the center, then build agent URL."""
    if _AGENT_ADVERTISE_URL:
        return _AGENT_ADVERTISE_URL.rstrip("/")
    try:
        # Use center host to detect outbound IP
        target = urlparse(_CENTRAL_API_URL).hostname or "8.8.8.8"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = socket.gethostbyname(socket.gethostname())
    return f"http://{ip}:{_AGENT_PORT}"


def _center_proxy() -> str | None:
    """Return the configured outbound proxy unless the center host is bypassed."""
    center_host = urlparse(_CENTRAL_API_URL).hostname
    if center_host and proxy_bypass(center_host):
        return None
    return _HTTPS_PROXY or _HTTP_PROXY or None


def _build_proxies() -> dict:
    """Build the legacy httpx proxy map for center registration."""
    proxy = _center_proxy()
    return {"https://": proxy, "http://": proxy} if proxy else {}


async def _register_with_center(advertise_url: str) -> None:
    """POST agent registration to the center API. Retries up to 5 times."""
    import httpx

    url = f"{_CENTRAL_API_URL}/api/v1/nodes/register"
    payload = {
        "agent_url": advertise_url,
        "mode": _AGENT_MODE,
        "node_type": _AGENT_DEPLOY_TYPE,
        "label": _AGENT_LABEL,
        "agent_protocol": "http",
        "runtimes": _available_agent_runtimes(),
        "runtime_capabilities": available_runtime_capabilities(),
        "profile_kind": _BROWSER_PROFILE_KIND,
        "node_id": _AGENT_NODE_ID or None,
        "boot_id": (
            _node_identity(required=False).boot_id if _node_identity(required=False) else None
        ),
        "credential_id": _AGENT_NODE_CREDENTIAL_ID or None,
        "account_capable": _account_runtime_prerequisite(advertise_url)[0],
    }
    proxies = _build_proxies()

    for attempt in range(1, 6):
        try:
            # httpx >= 0.28 removed 'proxies'; use 'proxy' (single URL) or mounts
            client_kwargs: dict = {"timeout": 10}
            headers = {**_auth_headers(), **_node_headers()}
            if proxies:
                proxy_url = proxies.get("https://") or proxies.get("http://")
                try:
                    client_kwargs["proxy"] = proxy_url
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        resp.raise_for_status()
                except TypeError:
                    # Older httpx: fall back to 'proxies'
                    client_kwargs.pop("proxy", None)
                    client_kwargs["proxies"] = proxies
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        resp.raise_for_status()
            else:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
            logger.info("Registered with center %s as %s", _CENTRAL_API_URL, advertise_url)
            return
        except Exception as exc:
            wait = attempt * 3
            logger.warning(
                "Registration attempt %d failed: %s — retrying in %ds",
                attempt,
                exc,
                wait,
            )
            await asyncio.sleep(wait)
    logger.error("Could not register with center after 5 attempts")


async def _handle_ws_collect(ws, msg: dict) -> None:
    """Execute a collect task received over the WS channel and send back the result."""
    request_id = msg.get("request_id", "")
    req = CollectRequest(
        site=msg.get("site", ""),
        command=msg.get("command", ""),
        args=msg.get("args", {}),
        positional_args=msg.get("positional_args", []),
        format=msg.get("format", "json"),
        mode=msg.get("mode", "bridge"),
        account_session=msg.get("account_session"),
        execution_id=request_id,
    )
    try:
        result = await collect(req)
    except Exception as exc:
        logger.exception("WS collect error for request_id=%s: %s", request_id, exc)
        result = {"success": False, "items": [], "error": str(exc)}
    result["type"] = "result"
    result["request_id"] = request_id
    try:
        await ws.send(json.dumps(result))
    except Exception as exc:
        logger.error("WS: failed to send result for request_id=%s: %s", request_id, exc)


class _PortalRuntime:
    """One short-lived route bound to one real CDP page target."""

    def __init__(
        self,
        *,
        portal_id: str,
        route: PortalOwnerRouteV1,
        cdp_endpoint: str,
        websocket_url: str,
    ) -> None:
        self.portal_id = portal_id
        self.route = route
        self.cdp_endpoint = cdp_endpoint
        self.websocket_url = websocket_url
        self.pixel_sequence = 0
        self.control_sequence = 0


_ACTIVE_PORTAL_RECORDS: dict[str, Any] = {}


async def _register_login_portal_runtime(
    session: SessionEnvelopeV1,
    *,
    agent_url: str,
    tunnel_handle: str,
    tunnel_auth_digest: str,
) -> None:
    """Bind the login session to the real page RecordSession before portal use."""

    if session.purpose != "login" or session.session_id in _ACTIVE_PORTAL_RECORDS:
        return
    owner_endpoint = _CENTRAL_API_URL
    if not agent_url.startswith("https://") or not owner_endpoint.startswith("https://"):
        raise BrowserRuntimeError(
            "portal_tls_required",
            "portal relay requires real TLS endpoints",
        )
    if not tunnel_handle or not re.fullmatch(r"[0-9a-f]{64}", tunnel_auth_digest):
        raise BrowserRuntimeError(
            "portal_connection_required",
            "portal relay is not bound to the authenticated node connection",
        )
    binding = session_runtime_registry().resolve(
        session_id=session.session_id,
        node_id=session.node_id,
        boot_id=session.node_boot_id,
        epoch=session.epoch,
    )
    from backend.skills.record import start_recording

    record_session = await start_recording(
        binding.cdp_endpoint,
        domain=session.target.origin or "controlled-login-fixture",
        capability="account-login",
    )
    registration = PortalRuntimeRegistration(
        session_id=session.session_id,
        node_id=session.node_id,
        boot_id=session.node_boot_id,
        epoch=session.epoch,
        agent_url=agent_url,
        owner_endpoint=owner_endpoint,
        tunnel_handle=tunnel_handle,
        tunnel_auth_digest=tunnel_auth_digest,
        record_session=record_session,
    )
    try:
        session_portal_registry().register(registration)
    except BaseException:
        await record_session.stop(status="failed", note="portal registration failed")
        raise
    _ACTIVE_PORTAL_RECORDS[session.session_id] = record_session


_ACTIVE_PORTALS: dict[str, _PortalRuntime] = {}
_PORTAL_MAX_WIRE_BYTES = 4_200_000


async def _send_ws_binary(ws, data: bytes) -> None:
    sender = getattr(ws, "send_bytes", None)
    if callable(sender):
        await sender(data)
    else:
        await ws.send(data)


async def _send_portal_pixel(ws, runtime: _PortalRuntime) -> None:
    runtime.pixel_sequence += 1
    frame = await capture_portal_frame(
        websocket_url=runtime.websocket_url,
        route=runtime.route,
        sequence=runtime.pixel_sequence,
    )
    encoded = encode_portal_wire_frame(frame)
    if len(encoded) > _PORTAL_MAX_WIRE_BYTES:
        raise RuntimeError("portal pixel exceeds transport limit")
    await _send_ws_binary(ws, encoded)


def _portal_frame_matches_route(
    frame: PortalWireFrameV1,
    route: PortalOwnerRouteV1,
) -> bool:
    binding = frame.transient.binding
    expected = route.binding
    return (
        binding.workspace_id == expected.account_ref.workspace_id
        and binding.account_id == expected.account_ref.account_id
        and binding.session_id == expected.session_id
        and binding.epoch == expected.epoch
        and binding.target == expected.target
        and binding.view_generation == expected.view_generation
    )


async def _handle_ws_portal(ws, msg: dict, authenticated_identity: NodeIdentityV1 | None) -> None:
    """Admit a route and bind it to the node's current real browser page."""
    portal_id = msg.get("portal_id", "")
    try:
        route = PortalOwnerRouteV1.model_validate(msg.get("route") or {})
        if route.binding.session_id != portal_id:
            raise ValueError("portal id does not match route session")
        if authenticated_identity is None or route.node_identity != authenticated_identity:
            raise ValueError("portal route node identity is not authenticated")
        binding = session_runtime_registry().resolve(
            session_id=route.binding.session_id,
            node_id=route.node_identity.node_id,
            boot_id=route.node_identity.boot_id,
            epoch=route.binding.epoch,
        )
        websocket_url = await resolve_portal_target(binding.cdp_endpoint, route)
        if portal_id in _ACTIVE_PORTALS:
            raise ValueError("portal route session is already active")
        runtime = _PortalRuntime(
            portal_id=portal_id,
            route=route,
            cdp_endpoint=binding.cdp_endpoint,
            websocket_url=websocket_url,
        )
        _ACTIVE_PORTALS[portal_id] = runtime
        await ws.send(json.dumps({"type": "portal_ready", "portal_id": portal_id}))
        await _send_portal_pixel(ws, runtime)
    except Exception as exc:
        _ACTIVE_PORTALS.pop(portal_id, None)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "portal_error",
                        "portal_id": portal_id,
                        "error": "portal route admission failed",
                    }
                )
            )
        except Exception:
            logger.debug("WS: failed to report portal admission error", exc_info=True)
        logger.warning("WS portal admission rejected: %s", exc)


async def _handle_ws_portal_binary(ws, data: bytes) -> None:
    """Apply one center control frame to the active real browser route."""
    if len(data) > _PORTAL_MAX_WIRE_BYTES:
        return
    try:
        frame = decode_portal_wire_frame(data)
    except ValueError:
        logger.warning("WS: invalid portal control frame")
        return
    portal_id = frame.transient.binding.session_id
    runtime = _ACTIVE_PORTALS.get(portal_id)
    if runtime is None:
        logger.warning("WS: control for inactive portal session=%s", portal_id)
    try:
        if runtime.route.route_expires_at <= datetime.now(UTC):
            raise ValueError("portal route expired")
        if (
            frame.encoding != "control-json"
            or not _portal_frame_matches_route(frame, runtime.route)
            or frame.sequence <= runtime.control_sequence
        ):
            raise ValueError("portal control is outside active route")
        control = frame.transient.control
        if control is None:
            raise ValueError("portal control payload is missing")
        current_target = await resolve_portal_target(runtime.cdp_endpoint, runtime.route)
        if current_target != runtime.websocket_url:
            raise ValueError("portal browser target changed")
        await apply_portal_control(
            websocket_url=runtime.websocket_url,
            cdp_endpoint=runtime.cdp_endpoint,
            route=runtime.route,
            control=control,
        )
        runtime.control_sequence = frame.sequence
        if control.kind == "request_view":
            await _send_portal_pixel(ws, runtime)
        else:
            await ws.send(
                json.dumps(
                    {"type": "portal_applied", "portal_id": portal_id, "sequence": frame.sequence}
                )
            )
    except Exception:
        logger.warning("WS: portal control rejected for session=%s", portal_id, exc_info=True)
        _ACTIVE_PORTALS.pop(portal_id, None)
        try:
            await ws.send(
                json.dumps(
                    {"type": "portal_error", "portal_id": portal_id, "error": "control rejected"}
                )
            )
        except Exception:
            logger.debug("WS: failed to report portal control error", exc_info=True)


async def _handle_ws_portal_prepare(
    ws,
    msg: dict,
    authenticated_identity: NodeIdentityV1 | None,
) -> None:
    """Build one short-lived route from this node's registered live runtime."""

    request_id = msg.get("request_id", "")
    try:
        if authenticated_identity is None:
            raise ValueError("portal node identity is unavailable")
        session = SessionEnvelopeV1.model_validate(msg.get("session"))
        if (
            session.node_id != authenticated_identity.node_id
            or session.node_boot_id != authenticated_identity.boot_id
        ):
            raise ValueError("portal session does not belong to this node")
        agent_url = msg.get("agent_url")
        session_revision = msg.get("session_revision")
        timeout = msg.get("timeout", 15)
        if (
            not isinstance(agent_url, str)
            or isinstance(session_revision, bool)
            or not isinstance(session_revision, int)
            or session_revision < 0
            or not isinstance(timeout, (int, float))
        ):
            raise ValueError("portal preparation request is invalid")
        route = await prepare_portal_route(
            agent_url,
            session,
            session_revision=session_revision,
            timeout=float(timeout),
        )
        await ws.send(
            json.dumps(
                {
                    "type": "portal_prepared",
                    "request_id": request_id,
                    "route": route.model_dump(mode="json"),
                }
            )
        )
    except Exception:
        logger.warning("WS portal preparation rejected", exc_info=True)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "portal_prepare_error",
                        "request_id": request_id,
                        "error": "portal preparation rejected",
                    }
                )
            )
        except Exception:
            logger.debug("WS: failed to report portal preparation error", exc_info=True)


async def _handle_ws_agent_task(
    ws,
    msg: dict,
    authenticated_identity: NodeIdentityV1 | None = None,
    *,
    agent_url: str = "",
    tunnel_handle: str = "",
    tunnel_auth_digest: str = "",
) -> None:
    """Execute an authenticated account task over the reverse channel.

    The command/claim/session envelope is validated before adapter side effects.
    """
    request_id = msg.get("request_id", "")

    async def _send_result(result: dict) -> None:
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "agent_result",
                        "request_id": request_id,
                        "result": result,
                    }
                )
            )
        except Exception as exc:
            logger.error("WS: failed to send agent_result for request_id=%s: %s", request_id, exc)

    runtime_type = msg.get("runtime", "")
    # Everything below is one outer try/except: get_runtime() lookup, adapter
    # construction of AgentTask, and the invoke() stream are all treated the
    # same way — any exception must resolve the center's pending future.
    task_config = dict(msg.get("config") or {})
    account_fields = ("command", "claim", "session", "node_identity")
    if any(msg.get(field) is not None for field in account_fields):
        if authenticated_identity is None:
            await _send_result(
                {
                    "type": "error",
                    "task_id": request_id,
                    "message": "WS node identity is unavailable",
                    "error_type": "NodeIdentityMissing",
                }
            )
            return
        try:
            runtime_request = RuntimeInvokeRequest.model_validate(
                {
                    "runtime": runtime_type,
                    "workflow": msg.get("workflow", ""),
                    "instructions": msg.get("instructions", ""),
                    "input": msg.get("input") or {},
                    "config": task_config,
                    "command": msg.get("command"),
                    "claim": msg.get("claim"),
                    "session": msg.get("session"),
                    "node_identity": msg.get("node_identity"),
                }
            )
            if runtime_request.node_identity != authenticated_identity:
                raise ValueError("WS node identity is not authenticated")
            assert runtime_request.command is not None
            assert runtime_request.claim is not None
            assert runtime_request.session is not None
            await _admit_claim(
                RuntimeClaimRequest(
                    command=runtime_request.command,
                    claim=runtime_request.claim,
                    session=runtime_request.session,
                    node_identity=authenticated_identity,
                ),
                authenticated_identity=authenticated_identity,
            )
            account_context = resolve_account_runtime_context(runtime_request)
            task_config = account_context.server_config | {
                key: value
                for key, value in task_config.items()
                if key
                not in {
                    "remote",
                    "binary",
                    "cdp_endpoint",
                    "endpoint",
                    "daemon_endpoint",
                    "profile_dir",
                    "home_dir",
                    "cache_dir",
                    "display",
                }
            }
            command_kind = runtime_request.command.kind.value
            if command_kind in {"stop_and_save", "close_session"}:
                try:
                    registration = session_portal_registry().resolve(
                        session_id=runtime_request.session.session_id,
                        node_id=runtime_request.claim.node_id,
                        boot_id=runtime_request.claim.boot_id,
                        epoch=runtime_request.claim.epoch,
                        agent_url=agent_url,
                    )
                    await registration.record_session.stop(status="success")
                except BrowserRuntimeError as exc:
                    if exc.code != "portal_record_missing":
                        raise
                except Exception:
                    logger.warning(
                        "Portal capture shutdown failed before process stop session_id=%s",
                        runtime_request.session.session_id,
                        exc_info=True,
                    )
                manifest = await account_runtime_allocator().stop(
                    session_id=runtime_request.session.session_id,
                    node_id=runtime_request.claim.node_id,
                    boot_id=runtime_request.claim.boot_id,
                    epoch=runtime_request.claim.epoch,
                    save=command_kind == "stop_and_save",
                    workspace_id=runtime_request.command.workspace_id,
                    account_id=runtime_request.command.account_id,
                    command_id=runtime_request.command.command_id,
                )
                session_portal_registry().revoke(
                    session_id=runtime_request.session.session_id,
                    node_id=runtime_request.claim.node_id,
                    boot_id=runtime_request.claim.boot_id,
                    epoch=runtime_request.claim.epoch,
                )
                runtime_lease_book().revoke(runtime_request.command.command_id)
                await _send_result(
                    {
                        "type": "done",
                        "task_id": request_id,
                        "result": {
                            "runtime_status": "stopped",
                            "session_id": runtime_request.session.session_id,
                            "profile_manifest": manifest,
                        },
                    }
                )
                return
            if command_kind == "start_login":
                running = account_runtime_allocator().get(runtime_request.session.session_id)
                if running is None:
                    raise BrowserRuntimeError(
                        "capability_missing",
                        "started account runtime is unavailable",
                    )
                await _register_login_portal_runtime(
                    runtime_request.session,
                    agent_url=agent_url,
                    tunnel_handle=tunnel_handle,
                    tunnel_auth_digest=tunnel_auth_digest,
                )
                observation = await observe_login_runtime(
                    cdp_endpoint=running.binding.cdp_endpoint,
                    session=runtime_request.session,
                    claim=runtime_request.claim,
                    node_identity=authenticated_identity,
                    expected_origin=running.bundle.login_origin,
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "login_observation",
                            "observation": observation.model_dump(mode="json"),
                        }
                    )
                )
                await _send_result(
                    {
                        "type": "done",
                        "task_id": request_id,
                        "result": {
                            "runtime_status": "healthy",
                            "session_id": running.binding.session_id,
                            "profile_id": running.profile_id,
                        },
                    }
                )
                return
        except (ValueError, HTTPException, BrowserRuntimeError) as exc:
            if isinstance(exc, BrowserRuntimeError):
                error_code = exc.code
            elif isinstance(exc, HTTPException) and isinstance(exc.detail, str):
                error_code = exc.detail
            else:
                error_code = "runtime_context_invalid"
            await _send_result(
                {
                    "type": "error",
                    "task_id": request_id,
                    "message": str(exc),
                    "error_type": "RuntimeContextInvalid",
                    "error_code": error_code,
                }
            )
            return
    try:
        # Fires this coroutine via asyncio.create_task, so an uncaught exception
        # here would otherwise vanish into an unretrieved task exception and the
        # center would hang until its own send_agent_task timeout.
        try:
            adapter = get_runtime(runtime_type)
        except ValueError as exc:
            logger.warning(
                "WS agent_task request_id=%s: unknown runtime %r: %s",
                request_id,
                runtime_type,
                exc,
            )
            await _send_result(
                {
                    "type": "error",
                    "task_id": request_id,
                    "message": str(exc),
                    "error_type": "ValueError",
                }
            )
            return

        task = AgentTask(
            task_id=request_id,
            workflow=msg.get("workflow", ""),
            instructions=msg.get("instructions", ""),
            input=msg.get("input") or {},
            config=task_config,
            session_id=msg.get("session_id"),
            provider=msg.get("provider"),
            model=msg.get("model"),
            required_capabilities=tuple(msg.get("required_capabilities") or ()),
            permissions=msg.get("permissions") or {},
            budget=msg.get("budget") or {},
            evidence_requirements=tuple(msg.get("evidence_requirements") or ()),
        )
        config_errors = adapter.validate_config(task.config)
        if config_errors:
            raise RuntimeInvocationError("; ".join(config_errors), error_type="ConfigError")
        readiness = await adapter.readiness(task.config)
        if readiness.status != "ready":
            raise RuntimeInvocationError(
                readiness.reason or f"runtime {runtime_type!r} is not ready",
                error_type=readiness.reason_code or "RuntimeNotReady",
            )

        terminal_event: dict | None = None
        async for event in adapter.invoke(task):
            terminal_event = event
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "agent_event",
                            "request_id": request_id,
                            "event": event,
                        }
                    )
                )
            except Exception as exc:
                logger.error(
                    "WS: failed to send agent_event for request_id=%s: %s",
                    request_id,
                    exc,
                )
        # Contract violation (adapter yielded nothing) — still must resolve
        # the center's pending future rather than hang it until timeout.
        if terminal_event is None:
            terminal_event = {
                "type": "error",
                "task_id": request_id,
                "message": f"runtime {runtime_type!r} adapter yielded no events",
                "error_type": "RuntimeInvocationError",
            }
        await _send_result(terminal_event)
        logger.info(
            "WS agent_task finished request_id=%s runtime=%s terminal=%s",
            request_id,
            runtime_type,
            terminal_event.get("type"),
        )
    except RuntimeInvocationError as exc:
        logger.exception(
            "WS agent_task request_id=%s: adapter invocation error: %s",
            request_id,
            exc,
        )
        await _send_result(
            {
                "type": "error",
                "task_id": request_id,
                "message": str(exc),
                "error_type": exc.error_type or type(exc).__name__,
            }
        )
    except Exception as exc:
        logger.exception("WS agent_task request_id=%s: unexpected error: %s", request_id, exc)
        await _send_result(
            {
                "type": "error",
                "task_id": request_id,
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
        )


_ACTIVE_AGENT_TASKS: dict[str, asyncio.Task[None]] = {}


def _forget_ws_agent_task(request_id: str, task: asyncio.Task[None]) -> None:
    if _ACTIVE_AGENT_TASKS.get(request_id) is task:
        _ACTIVE_AGENT_TASKS.pop(request_id, None)


def _start_ws_agent_task(
    ws,
    msg: dict,
    authenticated_identity: NodeIdentityV1 | None = None,
    *,
    agent_url: str = "",
    tunnel_handle: str = "",
    tunnel_auth_digest: str = "",
) -> None:
    request_id = msg.get("request_id", "")
    if authenticated_identity is None and not (agent_url or tunnel_handle or tunnel_auth_digest):
        coroutine = _handle_ws_agent_task(ws, msg)
    else:
        coroutine = _handle_ws_agent_task(
            ws,
            msg,
            authenticated_identity,
            agent_url=agent_url,
            tunnel_handle=tunnel_handle,
            tunnel_auth_digest=tunnel_auth_digest,
        )
    task = asyncio.create_task(coroutine)
    _ACTIVE_AGENT_TASKS[request_id] = task
    task.add_done_callback(lambda completed: _forget_ws_agent_task(request_id, completed))


async def _register_via_ws(advertise_url: str) -> None:
    """Initiate persistent reverse WebSocket to center and handle collect tasks.

    Keeps reconnecting with exponential back-off so transient outages are
    recovered automatically.  The loop exits only when the process shuts down.
    """
    import websockets  # requires: pip install websockets

    ws_url = (
        _CENTRAL_API_URL.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        + "/api/v1/nodes/ws"
    )
    _proxy = _HTTPS_PROXY or _HTTP_PROXY or None
    # Computed once (not per reconnect attempt): runtime availability includes
    # fixed-binary compatibility probes and does not change without a restart.
    runtimes = _available_agent_runtimes()
    runtime_capabilities = available_runtime_capabilities()
    for runtime in runtimes:
        runtime_capabilities.setdefault(runtime, [])
    register_payload = json.dumps(
        {
            "type": "register",
            "agent_url": advertise_url,
            "mode": _AGENT_MODE,
            "node_type": _AGENT_DEPLOY_TYPE,
            "label": _AGENT_LABEL,
            "node_id": (
                _node_identity(required=False).node_id if _node_identity(required=False) else None
            ),
            "runtimes": runtimes,
            "runtime_capabilities": runtime_capabilities,
            "profile_kind": _BROWSER_PROFILE_KIND,
            "account_capable": _account_runtime_prerequisite(advertise_url)[0],
            "boot_id": (
                _node_identity(required=False).boot_id if _node_identity(required=False) else None
            ),
            "credential_id": _AGENT_NODE_CREDENTIAL_ID or None,
        }
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info("WS connecting to center %s (attempt %d)", ws_url, attempt)
            connect_kwargs: dict = {"ping_interval": 30, "ping_timeout": 10}
            if _proxy:
                connect_kwargs["proxy"] = _proxy
            headers = {**_auth_headers(), **_node_headers()}
            if headers:
                try:
                    # websockets >= 14 renamed extra_headers -> additional_headers.
                    connector = websockets.connect(
                        ws_url, additional_headers=headers, **connect_kwargs
                    )
                except TypeError:
                    connector = websockets.connect(ws_url, extra_headers=headers, **connect_kwargs)
            else:
                connector = websockets.connect(ws_url, **connect_kwargs)
            async with connector as ws:
                attempt = 0  # reset on successful connect
                await ws.send(register_payload)

                ack_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                ack = json.loads(ack_raw)
                if ack.get("type") != "registered":
                    raise RuntimeError(f"Unexpected handshake response: {ack}")
                connection_tunnel_handle = ack.get("tunnel_handle")
                connection_tunnel_nonce = ack.get("tunnel_nonce")
                identity = _node_identity(required=False)
                if identity is not None:
                    if (
                        not isinstance(connection_tunnel_handle, str)
                        or not re.fullmatch(r"[0-9a-f]{32}", connection_tunnel_handle)
                        or not isinstance(connection_tunnel_nonce, str)
                        or not 32 <= len(connection_tunnel_nonce) <= 128
                    ):
                        raise RuntimeError(
                            "authenticated center did not issue a portal tunnel binding"
                        )
                    connection_tunnel_digest = hashlib.sha256(
                        (
                            f"{connection_tunnel_handle}:{connection_tunnel_nonce}:"
                            f"{identity.node_id}:{identity.boot_id}:{advertise_url}"
                        ).encode()
                    ).hexdigest()
                else:
                    connection_tunnel_handle = ""
                    connection_tunnel_digest = ""
                logger.info("WS registered with center as %s", advertise_url)

                # Main receive loop
                async for raw_msg in ws:
                    if isinstance(raw_msg, (bytes, bytearray)):
                        await _handle_ws_portal_binary(ws, bytes(raw_msg))
                        continue
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning("WS: invalid JSON from center: %r", raw_msg[:200])
                        continue
                    msg_type = msg.get("type")
                    if msg_type == "collect":
                        asyncio.create_task(_handle_ws_collect(ws, msg))
                    elif msg_type == "portal_open":
                        asyncio.create_task(
                            _handle_ws_portal(ws, msg, _node_identity(required=False))
                        )
                    elif msg_type == "portal_prepare":
                        asyncio.create_task(
                            _handle_ws_portal_prepare(ws, msg, _node_identity(required=False))
                        )
                    elif msg_type == "portal_close":
                        _ACTIVE_PORTALS.pop(msg.get("portal_id", ""), None)
                    elif msg_type == "agent_task":
                        _start_ws_agent_task(
                            ws,
                            msg,
                            _node_identity(required=False),
                            agent_url=advertise_url,
                            tunnel_handle=connection_tunnel_handle,
                            tunnel_auth_digest=connection_tunnel_digest,
                        )
                    elif msg_type == "cancel":
                        request_id = msg.get("request_id", "")
                        proc = _ACTIVE_COLLECTS.get(request_id)
                        if proc is not None:
                            asyncio.create_task(_kill_process_tree(proc))
                        agent_task = _ACTIVE_AGENT_TASKS.get(request_id)
                        if agent_task is not None:
                            agent_task.cancel()
                    elif msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                    elif msg_type == "pong":
                        pass
                    else:
                        logger.debug("WS: unknown message type %r", msg_type)

        except asyncio.CancelledError:
            logger.info("WS registration task cancelled — shutting down")
            return
        except Exception as exc:
            wait = min(attempt * 3, 60)
            logger.warning(
                "WS connection lost (attempt %d): %s — reconnecting in %ds", attempt, exc, wait
            )
            await asyncio.sleep(wait)
        finally:
            _ACTIVE_PORTALS.clear()
            for task in tuple(_ACTIVE_AGENT_TASKS.values()):
                task.cancel()


_ws_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ws_task
    if not _CENTRAL_API_URL or _AGENT_REGISTER == "off":
        logger.info(
            "Auto-registration disabled (CENTRAL_API_URL=%r AGENT_REGISTER=%s)",
            _CENTRAL_API_URL or "",
            _AGENT_REGISTER,
        )
    elif _AGENT_REGISTER == "http":
        advertise_url = _detect_advertise_url()
        logger.info(
            "LAN registration: advertise_url=%s → center=%s",
            advertise_url,
            _CENTRAL_API_URL,
        )
        asyncio.get_event_loop().create_task(_register_with_center(advertise_url))
    elif _AGENT_REGISTER == "ws":
        advertise_url = _detect_advertise_url()
        logger.info(
            "WS registration: advertise_url=%s → center=%s",
            advertise_url,
            _CENTRAL_API_URL,
        )
        _ws_task = asyncio.get_event_loop().create_task(_register_via_ws(advertise_url))
    try:
        yield
    finally:
        if _ws_task and not _ws_task.done():
            _ws_task.cancel()
            try:
                await _ws_task
            except asyncio.CancelledError:
                pass
        await close_account_runtime_allocator()


app = FastAPI(title="OpenCLI Agent Server", version="0.4.1", lifespan=lifespan)


class CollectRequest(BaseModel):
    site: str
    command: str
    args: dict[str, Any] = {}
    # Values passed as positional CLI arguments (no --key prefix), inserted
    # right after [site] [command] and before any named --options.
    positional_args: list[str] = []
    # CDP endpoint override is retained only for legacy anonymous collection.
    cdp_endpoint: str = ""
    execution_id: str = ""
    account_session: dict[str, Any] | None = None


class RuntimeClaimRequest(BaseModel):
    command: DurableCommandV1
    claim: NodeClaimV1
    session: SessionEnvelopeV1
    node_identity: NodeIdentityV1


class RuntimeRenewRequest(BaseModel):
    claim: NodeClaimV1
    node_identity: NodeIdentityV1


class RuntimeResultRequest(BaseModel):
    result: NodeResultV1
    node_identity: NodeIdentityV1


async def _admit_claim(
    body: RuntimeClaimRequest,
    *,
    authenticated_identity: NodeIdentityV1,
) -> RuntimeLeaseAdmission:
    if body.node_identity != authenticated_identity:
        raise HTTPException(status_code=401, detail="claim node identity is not authenticated")
    admitted = False
    try:
        from backend.schemas.browser_account import CommandExecutionGuardV1

        guard = CommandExecutionGuardV1(
            command=body.command,
            claim=body.claim,
            session=body.session,
        )
        admission = runtime_lease_book().claim(
            claim=guard.claim,
            session=guard.session,
            node_identity=authenticated_identity,
        )
        admitted = True
        if guard.command.kind.value == "start_login":
            await account_runtime_allocator().start(
                command=guard.command,
                claim=guard.claim,
                session=guard.session,
                node_identity=authenticated_identity,
            )
        else:
            await account_runtime_allocator().renew(
                claim=guard.claim,
                session=guard.session,
                node_identity=authenticated_identity,
            )
            session_runtime_registry().resolve(
                session_id=guard.session.session_id,
                node_id=guard.claim.node_id,
                boot_id=guard.claim.boot_id,
                epoch=guard.claim.epoch,
            )
        return admission
    except (ValueError, BrowserRuntimeError) as exc:
        if admitted:
            runtime_lease_book().revoke(body.claim.command_id)
        code = exc.code if isinstance(exc, BrowserRuntimeError) else "runtime_context_invalid"
        raise HTTPException(status_code=409, detail=code) from exc


@app.get("/health")
def health() -> dict:
    bin_path = _resolve_bin(_AGENT_MODE)
    account_capable, prerequisite = _account_runtime_prerequisite(_AGENT_ADVERTISE_URL or None)
    identity = _node_identity(required=False)
    return {
        "status": "ok",
        "opencli_bin": bin_path,
        "opencli_bin_exists": shutil.which(bin_path) is not None or os.path.isfile(bin_path),
        "account_capable": account_capable,
        "account_runtime_prerequisite": prerequisite,
        "node_identity": identity.model_dump(mode="json") if identity else None,
    }


@app.post("/runtime/invoke")
async def invoke_runtime_http(
    req: RuntimeInvokeRequest,
    authorization: str | None = Header(default=None),
    node_id: str | None = Header(default=None, alias="X-Node-ID"),
    boot_id: str | None = Header(default=None, alias="X-Node-Boot-ID"),
    node_credential: str | None = Header(default=None, alias="X-Node-Credential"),
) -> dict:
    account_dispatch = any(
        value is not None for value in (req.command, req.claim, req.session, req.node_identity)
    )
    context: AccountRuntimeContext | None = None
    if account_dispatch:
        identity = _require_node_auth(authorization, node_id, boot_id, node_credential)
        if req.node_identity != identity:
            raise HTTPException(
                status_code=401, detail="runtime node identity is not authenticated"
            )
        context = resolve_account_runtime_context(req)
    else:
        _require_collect_auth(authorization)
    if req.runtime == "codex":
        raise HTTPException(
            status_code=403,
            detail="Codex runtime is only available through controller WS dispatch",
        )
    if req.runtime not in _bundle_declared_runtimes():
        raise HTTPException(
            status_code=403,
            detail=f"runtime {req.runtime!r} is not declared by the installed bundle",
        )
    unsafe_keys = sorted(set(req.config) - _EDGE_RUNTIME_TASK_CONFIG_KEYS)
    if unsafe_keys:
        raise HTTPException(
            status_code=400,
            detail="edge runtime config cannot override: " + ", ".join(unsafe_keys),
        )
    if context is None and not _DEFAULT_CDP:
        raise HTTPException(status_code=503, detail="anonymous runtime has no configured endpoint")
    return await invoke_runtime(
        str(uuid.uuid4()),
        req,
        cdp_endpoint=context.cdp_endpoint if context is not None else _DEFAULT_CDP,
        account_context=context,
    )


@app.post("/runtime/claim")
async def claim_runtime(
    body: RuntimeClaimRequest,
    authorization: str | None = Header(default=None),
    node_id: str | None = Header(default=None, alias="X-Node-ID"),
    boot_id: str | None = Header(default=None, alias="X-Node-Boot-ID"),
    node_credential: str | None = Header(default=None, alias="X-Node-Credential"),
) -> dict[str, Any]:
    identity = _require_node_auth(authorization, node_id, boot_id, node_credential)
    admission = await _admit_claim(body, authenticated_identity=identity)
    return {
        "accepted": True,
        "node_identity": identity.model_dump(mode="json"),
        "claim": admission.claim.model_dump(mode="json"),
        "session": admission.session.model_dump(mode="json"),
    }


@app.post("/runtime/renew")
async def renew_runtime(
    body: RuntimeRenewRequest,
    authorization: str | None = Header(default=None),
    node_id: str | None = Header(default=None, alias="X-Node-ID"),
    boot_id: str | None = Header(default=None, alias="X-Node-Boot-ID"),
    node_credential: str | None = Header(default=None, alias="X-Node-Credential"),
) -> dict[str, Any]:
    identity = _require_node_auth(authorization, node_id, boot_id, node_credential)
    if body.node_identity != identity:
        raise HTTPException(status_code=401, detail="renewal node identity is not authenticated")
    try:
        admission = runtime_lease_book().renew(
            claim=body.claim,
            node_identity=identity,
            now=datetime.now(UTC),
        )
        await account_runtime_allocator().renew(
            claim=body.claim,
            session=admission.session,
            node_identity=identity,
        )
    except BrowserRuntimeError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return {
        "accepted": True,
        "command_id": admission.claim.command_id,
        "expires_at": admission.claim.expires_at,
    }


@app.post("/runtime/result")
async def result_runtime(
    body: RuntimeResultRequest,
    authorization: str | None = Header(default=None),
    node_id: str | None = Header(default=None, alias="X-Node-ID"),
    boot_id: str | None = Header(default=None, alias="X-Node-Boot-ID"),
    node_credential: str | None = Header(default=None, alias="X-Node-Credential"),
) -> dict[str, Any]:
    identity = _require_node_auth(authorization, node_id, boot_id, node_credential)
    if body.node_identity != identity:
        raise HTTPException(status_code=401, detail="result node identity is not authenticated")
    try:
        admission = runtime_lease_book().result(
            result=body.result,
            node_identity=identity,
            now=datetime.now(UTC),
        )
    except BrowserRuntimeError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return {
        "accepted": True,
        "command_id": admission.claim.command_id,
        "status": admission.result.status if admission.result is not None else None,
    }


async def collect(req: CollectRequest) -> dict:
    cdp_ep = req.cdp_endpoint.strip()
    if req.account_session is not None:
        try:
            account_session = SessionEnvelopeV1.model_validate(req.account_session)
            identity = _node_identity()
            if (
                account_session.node_id != identity.node_id
                or account_session.node_boot_id != identity.boot_id
            ):
                raise ValueError("account session belongs to another node")
            cdp_ep = (
                session_runtime_registry()
                .resolve(
                    session_id=account_session.session_id,
                    node_id=account_session.node_id,
                    boot_id=account_session.node_boot_id,
                    epoch=account_session.epoch,
                )
                .cdp_endpoint
            )
        except (ValueError, BrowserRuntimeError) as exc:
            return {
                "success": False,
                "items": [],
                "error": f"capability_missing: account runtime session unavailable ({exc})",
            }
    else:
        cdp_ep = cdp_ep or _DEFAULT_CDP
    if not cdp_ep:
        return {
            "success": False,
            "items": [],
            "error": "capability_missing: server-resolved runtime endpoint required",
        }
    mode = "cdp" if req.account_session is not None else req.mode
    bin_path = _resolve_bin(mode)

    cmd = [bin_path, req.site, req.command]
    cmd.extend([str(v) for v in req.positional_args])
    for k, v in req.args.items():
        cmd.extend([f"--{k}", str(v)])
    cmd.extend(["-f", req.format])

    env = os.environ.copy()
    if mode == "bridge":
        hostname = urlparse(cdp_ep).hostname or "localhost"
        # When running in a Docker agent WITHOUT bundled Chrome, localhost refers
        # to the container itself — remap to host.docker.internal so opencli
        # reaches the Chrome bridge daemon on the host machine.
        # Agents built with INSTALL_CHROME=true have their own Chrome and should
        # keep localhost as-is.
        if (
            _AGENT_DEPLOY_TYPE == "docker"
            and not _AGENT_HAS_CHROME
            and hostname in ("localhost", "127.0.0.1", "::1")
        ):
            hostname = "host.docker.internal"
        env.pop("OPENCLI_CDP_ENDPOINT", None)
        env["OPENCLI_DAEMON_HOST"] = hostname
        env["OPENCLI_DAEMON_PORT"] = str(_DAEMON_PORT)
        logger.info("bridge | cmd=%s daemon=%s:%s", " ".join(cmd), hostname, _DAEMON_PORT)
    else:
        # Same logic for CDP: remap localhost to host.docker.internal only when
        # running in Docker without bundled Chrome.
        if _AGENT_DEPLOY_TYPE == "docker" and not _AGENT_HAS_CHROME:
            cdp_ep = re.sub(r"(localhost|127\.0\.0\.1)", "host.docker.internal", cdp_ep)
        env["OPENCLI_CDP_ENDPOINT"] = cdp_ep
        logger.info("cdp | cmd=%s cdp=%s", " ".join(cmd), cdp_ep)

    pre_tab_ids: set[str] = set()
    if mode == "cdp":
        pre_tab_ids = await snapshot_tab_ids(cdp_ep)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **_process_group_kwargs(),
        )
        if req.execution_id:
            _ACTIVE_COLLECTS[req.execution_id] = proc
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_OPENCLI_TIMEOUT)
        rc = proc.returncode
    except TimeoutError:
        logger.error("timeout | cmd=%s", " ".join(cmd))
        if proc:
            await _kill_process_tree(proc)
        if mode == "cdp":
            await cleanup_cdp_tabs(cdp_ep, pre_tab_ids)
        return {"success": False, "items": [], "error": "opencli timed out after 120s"}
    except Exception as exc:
        logger.exception("subprocess error | %s", exc)
        if mode == "cdp":
            await cleanup_cdp_tabs(cdp_ep, pre_tab_ids)
        return {"success": False, "items": [], "error": str(exc)}
    finally:
        if req.execution_id:
            _ACTIVE_COLLECTS.pop(req.execution_id, None)

    if mode == "cdp":
        await cleanup_cdp_tabs(cdp_ep, pre_tab_ids)

    stderr_str = stderr.decode().strip()
    stdout_str = stdout.decode()

    if stderr_str:
        logger.warning("stderr | %s", stderr_str[:500])
    if rc != 0:
        logger.error("exit=%d | %s", rc, stderr_str[:500])
        return {"success": False, "items": [], "error": f"opencli exit {rc}: {stderr_str}"}

    try:
        items = parse_output(stdout_str, req.format)
    except Exception as exc:
        logger.error("parse error | %s", exc)
        return {"success": False, "items": [], "error": f"parse error: {exc}"}

    logger.info("done | site=%s cmd=%s items=%d", req.site, req.command, len(items))
    trace_match = re.search(r"OpenCLI trace artifact:\s*([^\r\n]+)", stderr_str)
    metadata: dict[str, Any] = {"runtime": await _runtime_lineage(bin_path)}
    if trace_match:
        metadata["trace_artifact"] = trace_match.group(1)
    return {
        "success": True,
        "items": items,
        "error": None,
        "runtime": metadata["runtime"],
        "trace_artifact": metadata.get("trace_artifact"),
        "metadata": metadata,
    }


@app.post("/collect")
async def collect_http(
    req: CollectRequest, authorization: str | None = Header(default=None)
) -> dict:
    _require_collect_auth(authorization)
    return await collect(req)


@app.post("/collect/{execution_id}/cancel")
async def cancel_collect(
    execution_id: str, authorization: str | None = Header(default=None)
) -> dict:
    _require_collect_auth(authorization)
    proc = _ACTIVE_COLLECTS.get(execution_id)
    if proc is not None:
        await _kill_process_tree(proc)
    return {"cancelled": True, "execution_id": execution_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_AGENT_PORT, log_level="info")
