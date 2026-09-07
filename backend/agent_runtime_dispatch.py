"""Browser helpers and structured runtime dispatch for the edge agent."""

import asyncio
import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
    session_runtime_registry,
)
from backend.schemas.browser_account import (
    CommandExecutionGuardV1,
    DurableCommandV1,
    NodeClaimV1,
    NodeIdentityV1,
    SessionEnvelopeV1,
)

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
    return AccountRuntimeContext(guard=guard, binding=binding)

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


async def _evaluate_cdp_target(websocket_url: str, expression: str) -> Any:
    """Evaluate one expression in an existing CDP target and return its value."""
    import websockets

    async with websockets.connect(websocket_url, open_timeout=5) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=30))
            if message.get("id") != 1:
                continue
            if "error" in message or message.get("result", {}).get("exceptionDetails"):
                raise RuntimeError(
                    message.get("error")
                    or message["result"]["exceptionDetails"].get("text", "CDP evaluation failed")
                )
            return message.get("result", {}).get("result", {}).get("value")


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
