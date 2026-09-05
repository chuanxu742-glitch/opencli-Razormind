import fnmatch
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from jsonschema import ValidationError, validate
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.browser import (
    BrowserCapabilityInvocation,
    BrowserInstance,
    BrowserRuntimeBundle,
)
from backend.schemas.browser import RuntimeBundleManifest
from backend.services.browser_service import (
    BrowserRuntimeError,
    get_runtime_bundle,
    get_runtime_deployment,
)

_LOGIN_CAPABILITY_PREFIXES = ("account.login.", "account-login.")
_LOGIN_ALLOWED_ACTIONS = frozenset(
    {"login.observe", "login.open", "login.refresh", "login.switch-mode"}
)
_LOGIN_AUDIT_INPUT_KEYS = frozenset(
    {"session_id", "epoch", "rule_id", "rule_version", "view_generation", "state", "mode"}
)
_LOGIN_AUDIT_OUTPUT_KEYS = frozenset(
    {
        "state",
        "evidence_kind",
        "rule_id",
        "rule_version",
        "view_generation",
        "external_identity",
        "error_code",
        "reason_code",
        "regions",
        "focus",
    }
)


def _is_login_capability(capability_name: str, capability: Any) -> bool:
    return capability_name.startswith(_LOGIN_CAPABILITY_PREFIXES) or bool(
        isinstance(getattr(capability, "config", None), dict)
        and capability.config.get("sensitive_audit") == "login"
    )


def _safe_identity(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    identity: dict[str, str] = {}
    for key in ("provider", "subject", "label"):
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= 255:
            identity[key] = item
    return identity if "provider" in identity and "subject" in identity else None


def _safe_login_audit_input(args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    return {
        key: value
        for key, value in args.items()
        if key in _LOGIN_AUDIT_INPUT_KEYS and isinstance(value, (str, int, bool, type(None)))
    }


def _safe_login_audit_output(result: Any) -> dict:
    payload = result.get("result") if isinstance(result, dict) else result
    if not isinstance(payload, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in _LOGIN_AUDIT_OUTPUT_KEYS:
        value = payload.get(key)
        if key == "external_identity":
            value = _safe_identity(value)
        elif key in {"state", "evidence_kind", "rule_id", "rule_version", "error_code", "reason_code"}:
            if not isinstance(value, str) or len(value) > 255:
                continue
        elif key == "view_generation":
            if not isinstance(value, int) or value < 0:
                continue
        elif key in {"regions", "focus"}:
            if not isinstance(value, list):
                continue
            value = [
                item
                for item in value[:32]
                if isinstance(item, dict)
                and all(
                    isinstance(item.get(coord), int) and 0 <= item[coord] <= 8192
                    for coord in ("x", "y", "width", "height")
                    if coord in item
                )
            ]
        if value is not None:
            safe[key] = value
    return safe


def _safe_login_error(code: str) -> dict[str, str]:
    return {"code": code}


def _capability_for(bundle: BrowserRuntimeBundle, capability_name: str) -> Any:
    manifest = RuntimeBundleManifest.model_validate(bundle.manifest)
    return next((item for item in manifest.capabilities if item.name == capability_name), None)

async def _dispatch_capability(
    instance: BrowserInstance,
    capability: Any,
    args: dict,
) -> dict:
    if not instance.agent_url:
        raise BrowserRuntimeError(
            "agent_route_unavailable",
            "the selected browser slot has no agent route for capability invocation",
        )
    endpoint_host = urlparse(instance.endpoint).hostname
    agent_host = urlparse(instance.agent_url).hostname
    if not endpoint_host or agent_host != endpoint_host:
        raise BrowserRuntimeError(
            "untrusted_agent_route",
            "agent route must use the registered browser endpoint host",
        )
    payload = {
        "runtime": capability.runtime,
        "workflow": capability.action,
        "instructions": capability.action,
        "input": args,
        "config": capability.config,
    }
    if instance.agent_protocol == "ws":
        from backend import ws_agent_manager

        return await ws_agent_manager.send_agent_task(
            instance.agent_url, payload, on_event=lambda _: None
        )
    if instance.agent_protocol != "http":
        raise BrowserRuntimeError(
            "agent_route_unavailable", "the selected browser slot has no supported agent protocol"
        )
    token = os.environ.get("AGENT_API_TOKEN") or os.environ.get("API_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{instance.agent_url.rstrip('/')}/runtime/invoke",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict):
        raise BrowserRuntimeError("invalid_agent_response", "agent returned a non-object result")
    return result


async def invoke_capability(
    session: AsyncSession,
    instance: BrowserInstance,
    capability_name: str,
    args: dict,
    gate: str | None,
    *,
    gate_authorized: bool = False,
    audit_input_payload: dict | None = None,
) -> BrowserCapabilityInvocation:
    deployment = await get_runtime_deployment(session, instance.id)
    bundle = (
        await get_runtime_bundle(session, instance.runtime_bundle_id)
        if instance.runtime_bundle_id
        else None
    )
    manifest = RuntimeBundleManifest.model_validate(bundle.manifest) if bundle else None
    component_versions = list(deployment.loaded_components) if deployment is not None else []
    capability = _capability_for(bundle, capability_name) if bundle else None
    login_capability = _is_login_capability(capability_name, capability)
    invocation = BrowserCapabilityInvocation(
        browser_instance_id=instance.id,
        capability=capability_name,
        desired_bundle_name=bundle.name if bundle else None,
        desired_bundle_version=bundle.version if bundle else None,
        loaded_bundle_version=deployment.loaded_bundle_version if deployment else None,
        component_versions=component_versions,
        input_payload=(
            _safe_login_audit_input(args)
            if login_capability
            else (audit_input_payload if audit_input_payload is not None else args)
        ),
        risk=capability.risk if capability else "high",
        gate=gate,
    )
    session.add(invocation)
    await session.flush()
    started = time.perf_counter()
    try:
        if deployment is None or deployment.state != "READY":
            raise BrowserRuntimeError(
                "slot_not_ready", "capability invocation requires a READY browser slot"
            )
        if capability is None or manifest is None:
            raise BrowserRuntimeError(
                "unknown_capability",
                f"capability {capability_name!r} is not exposed by the desired bundle",
            )
        if login_capability and (
            capability.runtime != "script-host"
            or capability.config.get("pack") != "account-login"
            or capability.action not in _LOGIN_ALLOWED_ACTIONS
        ):
            raise BrowserRuntimeError(
                "login_capability_not_packaged",
                "account login capabilities must use the fixed account-login Script Host pack",
            )
        try:
            validate(instance=args, schema=capability.args_schema)
        except ValidationError as exc:
            raise BrowserRuntimeError("invalid_capability_args", exc.message) from exc
        if capability.allowed_hosts:
            candidate_url = args.get("url")
            host = urlparse(candidate_url).hostname if isinstance(candidate_url, str) else None
            if not host or not _host_allowed(host, capability.allowed_hosts):
                raise BrowserRuntimeError(
                    "host_not_allowed",
                    f"capability {capability_name!r} is not allowed for the requested host",
                )
        loaded_component_ids = {
            item.get("id")
            for item in component_versions
            if isinstance(item, dict) and item.get("healthy") is True
        }
        if capability.component_id not in loaded_component_ids:
            raise BrowserRuntimeError(
                "capability_component_unavailable",
                f"component {capability.component_id!r} is not loaded and healthy",
            )
        if capability.required_gate is not None and (
            capability.required_gate != gate or not gate_authorized
        ):
            raise BrowserRuntimeError(
                "gate_not_satisfied",
                f"capability {capability_name!r} requires an authorized "
                f"{capability.required_gate!r} gate",
            )
        result = await _dispatch_capability(instance, capability, args)
        if login_capability:
            invocation.output_payload = _safe_login_audit_output(result)
            invocation.page_before = None
            invocation.page_after = None
        else:
            invocation.output_payload = result
            invocation.page_before = result.get("page_before")
            invocation.page_after = result.get("page_after")
    except BrowserRuntimeError as exc:
        invocation.error = _safe_login_error(exc.code) if login_capability else {
            "code": exc.code,
            "message": str(exc),
        }
        raise
    except httpx.HTTPError as exc:
        invocation.error = (
            _safe_login_error("agent_invocation_failed")
            if login_capability
            else {"code": "agent_invocation_failed", "message": str(exc)}
        )
        raise BrowserRuntimeError(
            "agent_invocation_failed",
            "login capability dispatch failed" if login_capability else str(exc),
        ) from exc
    except Exception as exc:
        invocation.error = (
            _safe_login_error("capability_invocation_failed")
            if login_capability
            else {"code": "capability_invocation_failed", "message": str(exc)}
        )
        raise BrowserRuntimeError(
            "capability_invocation_failed",
            "login capability invocation failed" if login_capability else str(exc),
        ) from exc
    finally:
        invocation.duration_ms = int((time.perf_counter() - started) * 1000)
        await session.flush()
    return invocation
