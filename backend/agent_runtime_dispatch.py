"""Browser helpers and structured runtime dispatch for the edge agent."""

import asyncio
import base64
import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from backend.agent_runtimes.base import AgentTask, RuntimeInvocationError
from backend.agent_runtimes.registry import get_runtime
from backend.browser_account_runtime import (
    BrowserRuntimeError,
    SessionRuntimeBinding,
    runtime_lease_book,
    session_portal_registry,
    session_runtime_registry,
)
from backend.schemas.browser_account import (
    CommandExecutionGuardV1,
    DurableCommandV1,
    NodeClaimV1,
    NodeIdentityV1,
    PortalControlMessageV1,
    PortalOuterBindingV1,
    PortalOwnerRouteV1,
    PortalPixelFrameV1,
    PortalRegionFocusV1,
    PortalTransientV1,
    PortalWireFrameV1,
    PortalWireLayoutV1,
    SensitiveSessionBindingV1,
    SessionEnvelopeV1,
)
from backend.services.browser_portal_contract import portal_wire_metadata_length

logger = logging.getLogger(__name__)


class RuntimeInvokeRequest(BaseModel):
    """Structured runtime action forwarded only from an allowlisted bundle."""

    model_config = ConfigDict(extra="forbid")

    runtime: str
    workflow: str
    instructions: str = ""
    input: dict[str, Any] = {}
    config: dict[str, Any] = {}
    # Account dispatch is admitted only with the existing typed claim/session
    # envelope.  The edge never accepts a client endpoint as a substitute.
    command: DurableCommandV1 | None = None
    claim: NodeClaimV1 | None = None
    session: SessionEnvelopeV1 | None = None
    node_identity: NodeIdentityV1 | None = None


@dataclass(frozen=True)
class AccountRuntimeContext:
    guard: CommandExecutionGuardV1
    binding: SessionRuntimeBinding

    @property
    def cdp_endpoint(self) -> str:
        return self.binding.cdp_endpoint

    @property
    def server_config(self) -> dict[str, Any]:
        # Only non-secret process routing facts are projected into the adapter.
        # The caller's config is never allowed to override these values.
        return {
            "remote": self.binding.bbx_remote,
            "daemon_endpoint": self.binding.daemon_endpoint,
            "profile_dir": str(self.binding.profile_dir),
            "home_dir": str(self.binding.home_dir),
            "cache_dir": str(self.binding.cache_dir),
            "display": self.binding.display,
            "cdp_endpoint": self.binding.cdp_endpoint,
        }


def resolve_account_runtime_context(req: RuntimeInvokeRequest) -> AccountRuntimeContext:
    """Validate a server-resolved command/claim/session tuple before side effects."""

    if req.command is None or req.claim is None or req.session is None or req.node_identity is None:
        raise HTTPException(
            status_code=409,
            detail="account runtime dispatch requires command, claim, session, and node identity",
        )
    try:
        guard = CommandExecutionGuardV1(
            command=req.command,
            claim=req.claim,
            session=req.session,
        )
        binding = session_runtime_registry().resolve(
            session_id=guard.session.session_id,
            node_id=guard.claim.node_id,
            boot_id=guard.claim.boot_id,
            epoch=guard.claim.epoch,
        )
        runtime_lease_book().renew(
            claim=guard.claim,
            node_identity=req.node_identity,
            now=datetime.now(UTC),
        )
    except (ValueError, BrowserRuntimeError) as exc:
        code = exc.code if isinstance(exc, BrowserRuntimeError) else "runtime_context_invalid"
        raise HTTPException(status_code=409, detail=code) from exc
    if (
        req.node_identity.node_id != guard.claim.node_id
        or req.node_identity.boot_id != guard.claim.boot_id
    ):
        raise HTTPException(status_code=401, detail="authenticated node identity does not own claim")
    forbidden = {
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
    if forbidden.intersection(req.config):
        raise HTTPException(status_code=400, detail="client runtime routing override is forbidden")


async def prepare_portal_route(
    agent_url: str,
    session_envelope: SessionEnvelopeV1,
    *,
    session_revision: int,
    timeout: float = 15,
) -> PortalOwnerRouteV1:
    """Resolve one live edge page, RecordSession and L-approved focus into a route."""

    if isinstance(session_revision, bool) or not isinstance(session_revision, int) or session_revision < 0:
        raise ValueError("portal route requires an authorized session revision")
    if not 0 < timeout <= 30:
        raise ValueError("portal route timeout must be between zero and thirty seconds")
    if session_envelope.purpose != "login":
        raise ValueError("portal routes require a login session")
    target = session_envelope.target
    target.require_complete()
    if session_envelope.login_rule_id is None or session_envelope.login_rule_version is None:
        raise ValueError("portal routes require a fixed login rule")
    try:
        runtime = session_runtime_registry().resolve(
            session_id=session_envelope.session_id,
            node_id=session_envelope.node_id,
            boot_id=session_envelope.node_boot_id,
            epoch=session_envelope.epoch,
        )
        registration = session_portal_registry().resolve(
            session_id=session_envelope.session_id,
            node_id=session_envelope.node_id,
            boot_id=session_envelope.node_boot_id,
            epoch=session_envelope.epoch,
            agent_url=agent_url,
        )
    except BrowserRuntimeError as exc:
        raise RuntimeError(exc.code) from exc

    request = RuntimeInvokeRequest(
        runtime="script-host",
        workflow="login.observe",
        input={
            "session_id": session_envelope.session_id,
            "epoch": session_envelope.epoch,
            "target": target.model_dump(mode="json") | {
                "view_generation": session_envelope.view_generation
            },
        },
        config={
            "pack": "account-login",
            "action": "login.observe",
            "tab_id": target.tab_id,
        },
    )
    try:
        observation_response = await asyncio.wait_for(
            invoke_script_host(request, cdp_endpoint=runtime.cdp_endpoint),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise RuntimeError("login observe timed out") from exc
    if observation_response.get("ok") is not True:
        raise RuntimeError("login observe rejected the live target")
    observation = observation_response.get("result")
    if not isinstance(observation, dict):
        raise RuntimeError("login observe returned no observation")
    if (
        observation.get("session_id") != session_envelope.session_id
        or observation.get("epoch") != session_envelope.epoch
        or observation.get("rule_id") != session_envelope.login_rule_id
        or observation.get("rule_version") != session_envelope.login_rule_version
        or observation.get("target") != target.model_dump(mode="json")
        or observation.get("view_generation") != session_envelope.view_generation
    ):
        raise RuntimeError("login observe lineage does not match the authorized session")
    try:
        focus = PortalRegionFocusV1.model_validate(observation.get("region_focus"))
    except ValueError as exc:
        raise RuntimeError("login observe returned no approved portal focus") from exc
    if (
        focus.target != target
        or focus.view_generation != session_envelope.view_generation
    ):
        raise RuntimeError("login observe focus lineage changed")

    binding = SensitiveSessionBindingV1(
        account_ref={
            "workspace_id": session_envelope.workspace_id,
            "account_id": session_envelope.account_id,
        },
        session_id=session_envelope.session_id,
        epoch=session_envelope.epoch,
        target=target,
        view_generation=session_envelope.view_generation,
        record_session_id=getattr(registration.record_session, "session_id", None),
    )
    from backend.services.browser_portal_contract import (
        freeze_portal_record_session,
        register_portal_record_session,
    )

    register_portal_record_session(binding, registration.record_session)
    await freeze_portal_record_session(binding, registration.record_session)
    return PortalOwnerRouteV1(
        binding=binding,
        node_identity=NodeIdentityV1(
            node_id=session_envelope.node_id,
            boot_id=session_envelope.node_boot_id,
        ),
        owner_endpoint=registration.owner_endpoint,
        tunnel_handle=registration.tunnel_handle,
        tunnel_auth_digest=registration.tunnel_auth_digest,
        region_focus=focus,
        session_revision=session_revision,
        route_expires_at=datetime.now(UTC) + timedelta(seconds=timeout),
        max_frame_bytes=4_000_000,
        max_input_bytes=4_096,
    )

async def snapshot_tab_ids(cdp_endpoint: str) -> set[str]:
    """Return the set of tab IDs currently open in Chrome."""

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{cdp_endpoint}/json/list")
            return {target["id"] for target in resp.json() if "id" in target}
    except Exception:
        return set()


async def cleanup_cdp_tabs(cdp_endpoint: str, pre_existing_ids: set[str]) -> None:
    """Close tabs opened during collection while preserving existing tabs."""

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{cdp_endpoint}/json/list")
            tabs = resp.json()
            remaining_pages = sum(1 for target in tabs if target.get("type") == "page")
            for tab in tabs:
                tab_id = tab.get("id", "")
                if tab.get("type") == "page" and tab_id not in pre_existing_ids:
                    try:
                        await client.get(f"{cdp_endpoint}/json/close/{tab_id}")
                        logger.info(
                            "cleanup: closed new tab %s url=%s",
                            tab_id,
                            tab.get("url", "")[:80],
                        )
                        remaining_pages -= 1
                    except Exception:
                        pass
            if remaining_pages == 0:
                try:
                    await client.put(f"{cdp_endpoint}/json/new")
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("cleanup: could not close CDP tabs at %s: %s", cdp_endpoint, exc)


def parse_output(raw: str, fmt: str) -> list[dict]:
    if fmt == "json":
        start = next((index for index, char in enumerate(raw) if char in "{["), None)
        if start is None:
            raise ValueError(f"No JSON found: {raw[:200]!r}")
        data = json.loads(raw[start:])
        return data if isinstance(data, list) else [data]
    if fmt == "yaml":
        data = yaml.safe_load(raw)
        if isinstance(data, list):
            return data
        return [data] if isinstance(data, dict) else [{"content": str(data)}]
    if fmt == "csv":
        return list(csv.DictReader(io.StringIO(raw.strip())))
    return [{"content": raw}]
async def _cdp_command(
    websocket_url: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one CDP command against the already-resolved real page target."""
    import websockets

    async with websockets.connect(websocket_url, open_timeout=5) as websocket:
        await websocket.send(
            json.dumps({"id": 1, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=30))
            if message.get("id") != 1:
                continue
            if "error" in message:
                raise RuntimeError(message["error"])
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"CDP {method} returned no result")
            return result


async def _evaluate_cdp_target(websocket_url: str, expression: str) -> Any:
    """Evaluate one expression in an existing CDP target and return its value."""
    result = await _cdp_command(
        websocket_url,
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        },
    )
    if result.get("exceptionDetails"):
        raise RuntimeError(result["exceptionDetails"].get("text", "CDP evaluation failed"))
    return result.get("result", {}).get("value")


def _script_host_action(req: RuntimeInvokeRequest) -> tuple[str, str]:
    pack = req.config.get("pack")
    action = req.config.get("action") or req.workflow
    if not isinstance(pack, str) or not pack or not isinstance(action, str) or not action:
        raise HTTPException(
            status_code=400,
            detail="script-host capabilities require config.pack and an action",
        )
    return pack, action


async def invoke_script_host(req: RuntimeInvokeRequest, *, cdp_endpoint: str) -> dict:
    """Invoke only an action present in the packaged Script Host registry."""

    pack, action = _script_host_action(req)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{cdp_endpoint.rstrip('/')}/json/list")
            response.raise_for_status()
            targets = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Chrome CDP unavailable: {exc}") from exc

    for target in targets:
        websocket_url = target.get("webSocketDebuggerUrl")
        target_url = target.get("url", "")
        is_script_host_target = target.get("type") == "service_worker" or (
            target.get("type") == "page" and target_url.endswith("/host.html")
        )
        if (
            not is_script_host_target
            or not isinstance(websocket_url, str)
            or not target_url.startswith("chrome-extension://")
        ):
            continue
        try:
            manifest_name = await _evaluate_cdp_target(
                websocket_url, "chrome.runtime.getManifest().name"
            )
        except Exception:
            continue
        if manifest_name != "OpenCLI Script Host":
            continue
        invocation = {
            "pack": pack,
            "action": action,
            "args": req.input,
            "tabId": req.config.get("tab_id"),
        }
        expression = (
            f"globalThis.opencliScriptHost.invoke({json.dumps(invocation, separators=(',', ':'))})"
        )
        try:
            result = await _evaluate_cdp_target(websocket_url, expression)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Script Host invocation failed: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise HTTPException(status_code=502, detail="Script Host returned a non-object result")
        return result
    raise HTTPException(status_code=503, detail="OpenCLI Script Host target is unavailable")


async def resolve_portal_target(cdp_endpoint: str, route: PortalOwnerRouteV1) -> str:
    """Resolve the exact live page target; never accept a caller endpoint."""
    target = route.binding.target
    target.require_complete()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{cdp_endpoint.rstrip('/')}/json/list")
            response.raise_for_status()
            targets = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Chrome CDP target listing is unavailable") from exc
    matches = [
        item
        for item in targets
        if item.get("type") == "page"
        and str(item.get("id")) == str(target.tab_id)
        and isinstance(item.get("webSocketDebuggerUrl"), str)
    ]
    if len(matches) != 1:
        raise RuntimeError("portal page target is missing or ambiguous")
    page_url = matches[0].get("url", "")
    actual = urlparse(page_url)
    expected = urlparse(target.origin or "")
    if (
        not actual.scheme
        or not actual.netloc
        or (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc)
    ):
        raise RuntimeError("portal page origin does not match server target")
    return matches[0]["webSocketDebuggerUrl"]


def _portal_binding(route: PortalOwnerRouteV1) -> PortalOuterBindingV1:
    binding = route.binding
    return PortalOuterBindingV1(
        workspace_id=binding.account_ref.workspace_id,
        account_id=binding.account_ref.account_id,
        session_id=binding.session_id,
        epoch=binding.epoch,
        target=binding.target,
        view_generation=binding.view_generation,
    )


async def capture_portal_frame(
    *,
    websocket_url: str,
    route: PortalOwnerRouteV1,
    sequence: int,
) -> PortalWireFrameV1:
    """Capture only the first L-approved region from the real page target."""
    region = route.region_focus.approved_regions[0]
    result = await _cdp_command(
        websocket_url,
        "Page.captureScreenshot",
        {
            "format": "png",
            "fromSurface": True,
            "captureBeyondViewport": False,
            "clip": {
                "x": region.x,
                "y": region.y,
                "width": region.width,
                "height": region.height,
                "scale": 1,
            },
        },
    )
    raw = base64.b64decode(result.get("data", ""), validate=True)
    pixel = PortalPixelFrameV1(
        workspace_id=route.binding.account_ref.workspace_id,
        account_id=route.binding.account_ref.account_id,
        session_id=route.binding.session_id,
        epoch=route.binding.epoch,
        target=route.binding.target,
        view_generation=route.binding.view_generation,
        sequence=sequence,
        region_kind=route.region_focus.region_kind,
        mime_type="image/png",
        expires_at=datetime.now(UTC) + timedelta(seconds=5),
        clip=region,
        byte_length=len(raw),
        frame_bytes=raw,
    )
    binding = _portal_binding(route)
    frame = PortalWireFrameV1(
        sequence=sequence,
        encoding="pixel-binary",
        content_type="application/octet-stream",
        mime_type="image/png",
        byte_length=len(raw),
        layout=PortalWireLayoutV1(metadata_bytes=1, payload_bytes=len(raw)),
        transient=PortalTransientV1(binding=binding, pixel=pixel),
    )
    metadata_bytes = portal_wire_metadata_length(frame)
    return frame.model_copy(
        update={
            "layout": PortalWireLayoutV1(
                metadata_bytes=metadata_bytes,
                payload_bytes=len(raw),
            )
        }
    )


def _point_in_regions(route: PortalOwnerRouteV1, x: int, y: int) -> bool:
    return any(
        region.x <= x < region.x + region.width
        and region.y <= y < region.y + region.height
        for region in route.region_focus.approved_regions
    )


async def apply_portal_control(
    *,
    websocket_url: str,
    cdp_endpoint: str,
    route: PortalOwnerRouteV1,
    control: PortalControlMessageV1,
) -> bool:
    """Apply one approved control through CDP or the packaged Script Host."""

    payload = control.sensitive_payload
    if control.kind == "request_view":
        return True
    if control.kind == "field_input":
        # The fixed account-login pack has no guarded input action. Do not
        # route a credential through a generic capability or arbitrary CDP
        # evaluation; a dedicated pack action must prove current focus.
        raise RuntimeError("guarded portal field input adapter is unavailable")
    if payload is None:
        raise ValueError("portal control requires transient payload")
    if control.kind == "pointer":
        if payload.x is None or payload.y is None or not _point_in_regions(route, payload.x, payload.y):
            raise ValueError("portal pointer is outside approved regions")
        for event_type in ("mousePressed", "mouseReleased"):
            await _cdp_command(
                websocket_url,
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": payload.x, "y": payload.y, "button": "left", "clickCount": 1},
            )
        return True
    if control.kind == "key":
        if payload.key is None:
            raise ValueError("portal key control has no key")
        key = payload.key
        await _cdp_command(websocket_url, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        await _cdp_command(websocket_url, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        return True
    if control.kind == "takeover":
        return True
    raise ValueError("unsupported portal control")


async def invoke_runtime(
    request_id: str,
    req: RuntimeInvokeRequest,
    *,
    cdp_endpoint: str | None = None,
    account_context: AccountRuntimeContext | None = None,
) -> dict:
    if req.runtime == "codex":
        raise HTTPException(
            status_code=403,
            detail="Codex runtime is only available through controller WS dispatch",
        )
    if account_context is not None:
        cdp_endpoint = account_context.cdp_endpoint
        server_config = account_context.server_config
        # The account binding is the only authority for process routing.  The
        # client request can carry action arguments but never endpoint/binary
        # selection or BBX remote targets.
        merged_config = dict(server_config)
        merged_config.update({key: value for key, value in req.config.items() if key not in {
            "remote",
            "binary",
            "cdp_endpoint",
            "endpoint",
            "daemon_endpoint",
            "profile_dir",
            "home_dir",
            "cache_dir",
            "display",
        }})
        req = req.model_copy(update={"config": merged_config})
    if not cdp_endpoint:
        raise HTTPException(status_code=503, detail="runtime session has no server-resolved CDP binding")
    if req.runtime == "script-host":
        return await invoke_script_host(req, cdp_endpoint=cdp_endpoint)
    try:
        adapter = get_runtime(req.runtime)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unsupported runtime: {req.runtime}") from exc
    task = AgentTask(
        task_id=request_id,
        workflow=req.workflow,
        instructions=req.instructions,
        input=req.input,
        config=req.config,
    )
    config_errors = adapter.validate_config(task.config)
    if config_errors:
        raise HTTPException(status_code=400, detail="; ".join(config_errors))
    terminal_event: dict | None = None
    try:
        async for event in adapter.invoke(task):
            if not isinstance(event, dict):
                raise HTTPException(status_code=502, detail="runtime produced an invalid event")
            if terminal_event is not None:
                raise HTTPException(
                    status_code=502,
                    detail="runtime produced events after its terminal event",
                )
            if event.get("type") in {"done", "error"}:
                terminal_event = event
    except RuntimeInvocationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if terminal_event is None:
        raise HTTPException(
            status_code=502,
            detail=f"runtime {req.runtime!r} produced no terminal event",
        )
    return terminal_event
