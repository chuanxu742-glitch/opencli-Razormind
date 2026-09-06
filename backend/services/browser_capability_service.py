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
from backend.schemas.browser_account import BrowserAccountErrorCode, LoginObservationV1
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
_LOGIN_STATES = frozenset(
    {
        "opening",
        "presenting",
        "refreshing",
        "verifying",
        "challenge",
        "unknown",
        "saving",
        "saved",
        "error",
    }
)
_LOGIN_MODES = frozenset({"qr", "form", "native"})
_LOGIN_EVIDENCE = frozenset({"unknown", "valid", "invalid"})
_LOGIN_ERROR_CODES = frozenset(item.value for item in BrowserAccountErrorCode)
_LOGIN_TARGET_KEYS = frozenset({"tab_id", "frame_id", "document_id", "origin", "view_generation"})
_LOGIN_OBSERVATION_TARGET_KEYS = frozenset({"tab_id", "frame_id", "document_id", "origin"})
_LOGIN_REGION_KEYS = frozenset({"x", "y", "width", "height", "kind"})
_LOGIN_FOCUS_KEYS = frozenset({"x", "y", "width", "height"})
_LOGIN_REGION_KINDS = frozenset({"qr", "form", "approved"})
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
        "session_id",
        "epoch",
        "target",
        "observed_at",
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
    safe: dict[str, Any] = {}
    for key in _LOGIN_AUDIT_INPUT_KEYS:
        value = args.get(key)
        if key in {"session_id", "rule_id", "rule_version"}:
            if isinstance(value, str) and 0 < len(value) <= 255:
                safe[key] = value
        elif key in {"epoch", "view_generation"}:
            if type(value) is int and 0 <= value <= 2**63 - 1:
                safe[key] = value
        elif key == "state":
            if isinstance(value, str) and value in _LOGIN_STATES:
                safe[key] = value
        elif key == "mode":
            if isinstance(value, str) and value in _LOGIN_MODES:
                safe[key] = value
    return safe


def _safe_login_target(value: Any) -> dict[str, Any] | None:
    """Build a canonical snake_case target without retaining the source map."""
    if not isinstance(value, dict) or set(value) != _LOGIN_TARGET_KEYS:
        return None
    tab_id = value.get("tab_id")
    frame_id = value.get("frame_id")
    view_generation = value.get("view_generation")
    document_id = value.get("document_id")
    origin = value.get("origin")
    if type(tab_id) is not int or tab_id < 0:
        return None
    if type(frame_id) is not int or frame_id < 0:
        return None
    if type(view_generation) is not int or view_generation < 0:
        return None
    if not (
        (type(document_id) is int and document_id >= 0)
        or (isinstance(document_id, str) and 0 < len(document_id) <= 255)
    ):
        return None
    if not isinstance(origin, str) or not (1 <= len(origin) <= 2048):
        return None
    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    canonical_origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "tab_id": tab_id,
        "frame_id": frame_id,
        "document_id": document_id,
        "origin": canonical_origin,
        "view_generation": view_generation,
    }


def _safe_login_observation_target(value: Any) -> dict[str, Any] | None:
    """Copy the strict LoginObservation target (view is its top-level field)."""
    if not isinstance(value, dict) or set(value) != _LOGIN_OBSERVATION_TARGET_KEYS:
        return None
    tab_id = value.get("tab_id")
    frame_id = value.get("frame_id")
    document_id = value.get("document_id")
    origin = value.get("origin")
    if type(tab_id) is not int or tab_id < 0:
        return None
    if type(frame_id) is not int or frame_id < 0:
        return None
    if not (
        (type(document_id) is int and document_id >= 0)
        or (isinstance(document_id, str) and 0 < len(document_id) <= 255)
    ):
        return None
    if not isinstance(origin, str) or not (1 <= len(origin) <= 2048):
        return None
    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return {
        "tab_id": tab_id,
        "frame_id": frame_id,
        "document_id": document_id,
        "origin": f"{parsed.scheme}://{parsed.netloc}",
    }


def _safe_login_geometry(value: Any, *, focus: bool) -> list[dict[str, int | str]] | None:
    """Copy only fixed geometry fields; never retain the source mapping."""
    if not isinstance(value, list) or len(value) > 32:
        return None
    allowed = _LOGIN_FOCUS_KEYS if focus else _LOGIN_REGION_KEYS
    cleaned: list[dict[str, int | str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != allowed:
            return None
        coordinates = {key: item.get(key) for key in ("x", "y", "width", "height")}
        if any(type(number) is not int for number in coordinates.values()):
            return None
        if not (
            0 <= coordinates["x"] <= 8192
            and 0 <= coordinates["y"] <= 8192
            and 0 < coordinates["width"] <= 4096
            and 0 < coordinates["height"] <= 4096
        ):
            return None
        if focus:
            cleaned.append(coordinates)
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in _LOGIN_REGION_KINDS:
            return None
        cleaned.append({**coordinates, "kind": kind})
    return cleaned


def _unwrap_login_result(result: Any) -> dict[str, Any] | None:
    """Unwrap only the known host/runtime envelopes, bounded by depth."""
    current = result
    for _ in range(4):
        if not isinstance(current, dict):
            return None
        nested = current.get("result")
        if isinstance(nested, dict) and ("result" in current or "ok" in current):
            current = nested
            continue
        return current
    return current if isinstance(current, dict) else None


def _safe_login_audit_output(result: Any) -> dict:
    payload = _unwrap_login_result(result)
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
            if key == "state" and value not in _LOGIN_STATES:
                continue
            if key == "evidence_kind" and value not in _LOGIN_EVIDENCE:
                continue
            if key in {"error_code", "reason_code"} and value not in _LOGIN_ERROR_CODES:
                continue
        elif key == "view_generation":
            if type(value) is not int or value < 0:
                continue
        elif key == "session_id":
            if not isinstance(value, str) or not (0 < len(value) <= 36):
                continue
        elif key == "epoch":
            if type(value) is not int or value < 0:
                continue
        elif key == "target":
            value = (
                _safe_login_target(value)
                if isinstance(value, dict) and "view_generation" in value
                else _safe_login_observation_target(value)
            )
        elif key == "observed_at":
            if not isinstance(value, str) or not (1 <= len(value) <= 64):
                continue
        elif key == "regions":
            value = _safe_login_geometry(value, focus=False)
        elif key == "focus":
            value = _safe_login_geometry(value, focus=True)
        if value is not None:
            safe[key] = value
    return safe


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    return any(fnmatch.fnmatch(host, pattern) for pattern in allowed_hosts)


def _safe_login_error(code: str) -> dict[str, str]:
    if code in _LOGIN_ERROR_CODES:
        return {"code": code}
    mapped = {
        "host_not_allowed": BrowserAccountErrorCode.PERMISSION_DENIED.value,
        "slot_not_ready": BrowserAccountErrorCode.NODE_UNAVAILABLE.value,
        "agent_route_unavailable": BrowserAccountErrorCode.NODE_UNAVAILABLE.value,
        "untrusted_agent_route": BrowserAccountErrorCode.PERMISSION_DENIED.value,
        "capability_component_unavailable": BrowserAccountErrorCode.CAPABILITY_MISSING.value,
        "unknown_capability": BrowserAccountErrorCode.CAPABILITY_MISSING.value,
        "login_capability_not_packaged": BrowserAccountErrorCode.LOGIN_RULE_UNKNOWN.value,
        "invalid_capability_args": BrowserAccountErrorCode.LOGIN_RULE_UNKNOWN.value,
        "gate_not_satisfied": BrowserAccountErrorCode.PERMISSION_DENIED.value,
        "agent_invocation_failed": BrowserAccountErrorCode.NODE_UNAVAILABLE.value,
    }
    return {"code": mapped.get(code, BrowserAccountErrorCode.CAPABILITY_MISSING.value)}


def _validate_login_observation(result: Any) -> dict[str, Any]:
    """Validate the sole canonical observation envelope before persistence."""
    payload = _unwrap_login_result(result)
    if not isinstance(payload, dict):
        raise BrowserRuntimeError("capability_invocation_failed", "login observation is not an object")
    if payload.get("ok") is False:
        code = payload.get("error_code")
        if not isinstance(code, str) or code not in _LOGIN_ERROR_CODES:
            code = "capability_invocation_failed"
        raise BrowserRuntimeError(code, "login action was rejected")
    target = _safe_login_observation_target(payload.get("target"))
    if target is None:
        raise BrowserRuntimeError("stale_generation", "login observation has no complete target")
    if type(payload.get("view_generation")) is not int or payload["view_generation"] < 0:
        raise BrowserRuntimeError("stale_generation", "login observation has no view generation")
    # LoginObservationV1 is intentionally strict: regions/focus and any other
    # unratified sibling fields must not be smuggled through the boundary.
    try:
        observation = LoginObservationV1.from_wire(payload)
    except Exception as exc:
        raise BrowserRuntimeError(
            "capability_invocation_failed", "login observation failed contract validation"
        ) from exc
    return observation.to_wire()


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
        if login_capability:
            expected_rule_id = capability.config.get("rule_id")
            expected_rule_version = capability.config.get("rule_version")
            for key, expected in (
                ("rule_id", expected_rule_id),
                ("rule_version", expected_rule_version),
            ):
                supplied = args.get(key)
                if expected is not None and supplied is not None and supplied != expected:
                    raise BrowserRuntimeError("login_rule_unknown", "login rule does not match the bundle")
            target = _safe_login_target(args.get("target"))
            if target is None:
                raise BrowserRuntimeError("stale_generation", "login actions require a complete target")
            configured_origins = capability.config.get("allowed_origins")
            if isinstance(configured_origins, list) and target["origin"] not in configured_origins:
                raise BrowserRuntimeError("host_not_allowed", "login target origin is not configured")
        try:
            validate(instance=args, schema=capability.args_schema)
        except ValidationError as exc:
            raise BrowserRuntimeError("invalid_capability_args", exc.message) from exc
        if capability.allowed_hosts:
            candidate_url = args.get("url")
            if login_capability:
                target_args = args.get("target")
                target_origin = target_args.get("origin") if isinstance(target_args, dict) else None
                candidate_url = args.get("login_url") or target_origin
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
            if capability.action == "login.observe":
                observation = _validate_login_observation(result)
                invocation.output_payload = _safe_login_audit_output(observation)
            else:
                payload = _unwrap_login_result(result)
                if isinstance(payload, dict) and payload.get("ok") is False:
                    code = payload.get("error_code")
                    if not isinstance(code, str) or code not in _LOGIN_ERROR_CODES:
                        code = "capability_invocation_failed"
                    raise BrowserRuntimeError(code, "login action was rejected")
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
