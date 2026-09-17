"""Honest login catalog and read-only admission checks for interactive login.

These checks do not allocate resources or establish authentication. The fenced
scheduler and node runtime still validate deployment and target facts at use time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.browser_login_rules import (
    load_bundle_login_rule,
    load_packaged_rule,
    load_platform_catalog,
)
from backend.models.browser import (
    BrowserAccountLease,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode, EdgeNodeBoot, EdgeNodeCapacity
from backend.services import browser_account_service
from backend.services.browser_account_session_contract import ACTIVE_BROWSER_SESSION_STATUSES

_ROOT = Path(__file__).resolve().parents[2]
_RULE_PATH = _ROOT / "chrome/script-host/packs/account-login/rules.json"
_LOOPBACK_ORIGINS = frozenset({"http://127.0.0.1:49906", "http://localhost:49906"})


class LoginReadiness(BaseModel):
    ready: bool
    code: str
    message: str


def login_options() -> dict:
    """Separate official entry metadata from deployed resources and authentication."""
    catalog = load_platform_catalog()
    priority = {platform: index for index, platform in enumerate((
        "facebook", "instagram", "twitter", "youtube",
        "douyin", "bilibili", "xiaohongshu", "tiktok",
    ))}
    items = sorted(
        (item for item in catalog["items"] if item["id"] != "github"),
        key=lambda item: priority.get(item["id"], len(priority)),
    )
    for item in items:
        item.update(available=False, authentication_verified=False, requires_user_verification=True)
        if item["requires_configuration"]:
            item.update(
                reason_code="platform_configuration_required",
                message="需要配置企业部署地址；尚未提供固定受管登录入口。",
            )
        elif not item["browser_login_supported"]:
            item.update(
                reason_code="browser_login_entry_unavailable",
                message="此适配器未提供固定浏览器登录入口，请按其认证方式配置；不代表免登录。",
            )
        else:
            item.update(
                reason_code="login_resources_unavailable",
                message="等待实际安装此官方登录规则的在线账号节点。",
            )
    return {"source": catalog["source"], "items": items}


def _blocked(code: str, message: str) -> LoginReadiness:
    return LoginReadiness(ready=False, code=code, message=message)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _packaged_fixture_rule() -> dict | None:
    try:
        rule = json.loads(_RULE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(rule, dict)
        or rule.get("id") != "controlled-login-fixture"
        or rule.get("version") != "1.0.0"
        or rule.get("allowed_origins") != ["http://127.0.0.1:49906", "http://localhost:49906"]
        or rule.get("login_url") != "/login"
    ):
        return None
    return rule


def _bundle_contains_packaged_rule(bundle: BrowserRuntimeBundle) -> bool:
    # Only repository-packaged manifests are known here. A DB label alone cannot
    # turn a custom bundle (or a commercial platform) into supported login.
    if bundle.trust_level != "trusted":
        return False
    manifest = bundle.manifest
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != bundle.name
        or manifest.get("version") != bundle.version
    ):
        return False
    root = (_ROOT / "chrome/runtime-bundles").resolve()
    path = (root / bundle.name / bundle.version / "manifest.json").resolve()
    if root not in path.parents:
        return False
    try:
        packaged = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return manifest == packaged and any(
        isinstance(component, dict)
        and component.get("id") == "opencli-script-host"
        and component.get("kind") == "extension"
        for component in manifest.get("components", [])
    )


async def get_login_readiness(
    db: AsyncSession, workspace_id: str, account_id: str
) -> LoginReadiness:
    account = await browser_account_service.get_browser_account(db, workspace_id, account_id)
    if account.paused or account.status in {"closed", "expired"}:
        return _blocked("account_unavailable", "账号已暂停、关闭或过期，请先恢复账号。")
    if (
        not account.node_id
        and not account.runtime_bundle_id
        and not (account.profile_id or account.profile_version or account.profile_manifest_id)
    ):
        try:
            match = await match_new_account_login(db, account.site, require_free_slot=False)
        except browser_account_service.BrowserAccountError as error:
            return _blocked(error.code, str(error))
        if match and match.get("node_id"):
            return LoginReadiness(
                ready=True,
                code="assignment_available",
                message="可提交登录请求，系统将分配浏览器节点并按资源情况启动或排队。",
            )
        if match:
            from backend.services.browser_account_pool import configuration

            if configuration():
                return _blocked(
                    "pool_pending", "提交登录后将按需启动独立浏览器，容量满时自动排队。"
                )
            return _blocked(
                "login_resources_unavailable", "账号已保存，等待部署可用的浏览器节点后即可登录。"
            )
    if not account.login_rule_id or not account.login_rule_version:
        return _blocked(
            "login_rule_missing",
            "账号尚未配置登录规则，无法打开登录窗口；真实平台扫码规则仍待接入。",
        )
    rule = load_packaged_rule(
        (
            _ROOT / "chrome/platform-login-bundle4"
            if account.login_rule_id.startswith("official-")
            or account.login_rule_id in {"bilibili-qr", "douyin-qr"}
            or account.login_rule_version == "0.2.0"
            else _ROOT / "chrome/script-host"
        ),
        account.login_rule_id,
        account.login_rule_version,
    )
    if rule is None:
        return _blocked(
            "login_rule_unavailable", "账号指定的登录规则尚未受支持，无法打开登录窗口。"
        )
    allowed_sites = set(rule.get("allowed_origins", []))
    if rule["id"] in {"xiaohongshu-qr", "bilibili-qr", "douyin-qr"}:
        allowed_sites.add(rule["platform"] + ".com")
    allowed_sites.update(
        item["site"]
        for item in load_platform_catalog()["items"]
        if item["rule_id"] == account.login_rule_id
    )
    if account.site not in allowed_sites:
        return _blocked(
            "login_rule_site_mismatch", "现有登录规则与账号平台不匹配，不能用于此平台。"
        )
    if not account.runtime_bundle_id or not account.runtime_bundle_version:
        return _blocked(
            "runtime_bundle_missing", "尚未分配账号运行时包，请先由管理员部署并绑定运行资源。"
        )
    bundle = await db.get(BrowserRuntimeBundle, account.runtime_bundle_id)
    if account.login_rule_id in {"bilibili-qr", "douyin-qr"} and (
        bundle is None or bundle.name != "opencli-default" or bundle.version != "4"
    ):
        return _blocked("login_bundle_unavailable", "此平台需要实际部署账号运行时版本 4。")
    if (
        bundle is None
        or bundle.version != account.runtime_bundle_version
        or not _bundle_contains_packaged_rule(bundle)
    ):
        return _blocked(
            "runtime_bundle_unavailable", "运行时包不存在、版本不匹配或未包含受支持的登录规则。"
        )
    rule = load_bundle_login_rule(
        _ROOT,
        bundle_name=bundle.name,
        bundle_version=bundle.version,
        bundle_manifest=bundle.manifest,
        rule_id=account.login_rule_id,
        rule_version=account.login_rule_version,
    )
    if rule is None:
        return _blocked("login_rule_unavailable", "已部署运行时版本未包含此登录规则。")
    if not account.node_id:
        return _blocked("node_missing", "尚未分配账号节点，请先启动并绑定账号执行节点。")
    node = await db.get(EdgeNode, account.node_id)
    from backend.services.browser_account_pool import pool_node_state

    if (
        node
        and not node.quarantined
        and pool_node_state(node.id) in {"stopped", "starting", "stopping"}
    ):
        return _blocked(
            "pool_pending", "登录请求会自动唤醒此账号的独立浏览器，已有登录资料将保留。"
        )
    if node is None or node.status != "online":
        return _blocked("node_offline", "账号节点未上线，请先启动节点并等待连接。")
    if node.quarantined or not node.account_capable:
        return _blocked(
            "node_not_account_capable", "当前节点被隔离或不具备账号执行能力，请联系管理员。"
        )
    boot = await db.scalar(
        select(EdgeNodeBoot).where(
            EdgeNodeBoot.node_id == node.id,
            EdgeNodeBoot.boot_id == node.boot_id,
            EdgeNodeBoot.status == "active",
            EdgeNodeBoot.stopped_at.is_(None),
        )
    )
    if not node.boot_id or boot is None:
        return _blocked("node_boot_unavailable", "节点当前启动实例尚未就绪，请等待节点重新注册。")
    now = datetime.now(UTC)
    capacity = await db.scalar(
        select(EdgeNodeCapacity).where(
            EdgeNodeCapacity.node_id == node.id,
            EdgeNodeCapacity.boot_id == node.boot_id,
            EdgeNodeCapacity.valid.is_(True),
        )
    )
    if (
        capacity is None
        or _utc(capacity.expires_at) <= now
        or _utc(capacity.observed_at) > now
        or _utc(capacity.observed_at) < _utc(boot.started_at)
    ):
        return _blocked("capacity_unavailable", "节点容量信息缺失或已过期，请等待有效心跳。")
    if rule["id"] != "controlled-login-fixture" and not _capacity_reports_rule(
        capacity,
        account.runtime_bundle_id,
        account.runtime_bundle_version,
        account.login_rule_id,
        account.login_rule_version,
    ):
        return _blocked(
            "runtime_rule_not_deployed", "节点尚未报告此登录规则已实际安装，请等待部署完成。"
        )
    active = await db.scalar(
        select(BrowserLoginSession)
        .where(
            BrowserLoginSession.workspace_id == workspace_id,
            BrowserLoginSession.account_id == account_id,
            BrowserLoginSession.status.in_(ACTIVE_BROWSER_SESSION_STATUSES),
        )
        .order_by(BrowserLoginSession.updated_at.desc(), BrowserLoginSession.id.desc())
    )
    if active is not None:
        if active.status == "opening" and active.lease_id is None and active.command_id:
            queued = await db.get(BrowserDurableCommand, active.command_id)
            if (
                queued is not None
                and queued.status == "queued"
                and queued.kind == "start_login"
                and queued.account_id == account.id
                and queued.session_id == active.id
                and queued.workspace_id == workspace_id
                and _utc(queued.expires_at) > now
                and active.expires_at
                and _utc(active.expires_at) > now
            ):
                return _blocked(
                    "session_queued", "登录请求已排队，浏览器资源空闲后会自动启动，可取消排队。"
                )
        # A real session may own the last slot: validate its own lease instead
        # of demanding a second slot. Unbound legacy opening rows must not pass.
        lease = await db.scalar(
            select(BrowserAccountLease).where(
                BrowserAccountLease.workspace_id == workspace_id,
                BrowserAccountLease.account_id == account_id,
                BrowserAccountLease.lease_id == active.lease_id,
                BrowserAccountLease.status == "active",
            )
        )
        if (
            active.purpose == "login"
            and active.status != "saving"
            and active.node_id == node.id
            and active.node_boot_id == node.boot_id
            and active.login_rule_id == account.login_rule_id
            and active.login_rule_version == account.login_rule_version
            and active.tab_id is not None
            and active.frame_id is not None
            and active.document_id is not None
            and active.origin in rule["allowed_origins"]
            and active.command_id
            and active.expires_at
            and _utc(active.expires_at) > now
            and lease is not None
            and lease.node_id == node.id
            and lease.node_boot_id == node.boot_id
            and lease.epoch == active.epoch
            and _utc(lease.expires_at) > now
            and lease.released_at is None
        ):
            # Reuse the existing portal fence check as well, including the
            # highest active lease epoch and profile/session envelope validity.
            try:
                _, envelope, _ = await browser_account_service.get_portal_session_envelope(
                    db, workspace_id, account_id, active.id
                )
                if envelope.target.is_complete():
                    return LoginReadiness(
                        ready=True, code="session_reusable", message="可继续当前登录会话。"
                    )
            except browser_account_service.BrowserAccountError:
                pass
        return _blocked(
            "session_not_ready",
            "现有登录会话尚未建立有效浏览器目标，无法打开窗口；请关闭该会话并检查节点配置。",
        )
    if capacity.occupied_slots >= capacity.slot_limit:
        return _blocked(
            "capacity_full", "浏览器资源正在使用中，可以提交登录请求排队，空闲后自动启动。"
        )
    return LoginReadiness(
        ready=True,
        code="ready",
        message="登录资源检查通过，可以申请登录会话；实际启动仍由节点验证。",
    )


def _capacity_reports_rule(capacity, bundle_id, bundle_version, rule_id, rule_version) -> bool:
    facts = (
        capacity.capabilities.get("browser_login_bundles", [])
        if isinstance(capacity.capabilities, dict)
        else []
    )
    return isinstance(facts, list) and any(
        isinstance(bundle, dict)
        and bundle.get("id") == bundle_id
        and bundle.get("version") == bundle_version
        and isinstance(bundle.get("rules"), list)
        and any(
            isinstance(rule, dict)
            and rule.get("id") == rule_id
            and rule.get("version") == rule_version
            for rule in bundle["rules"]
        )
        for bundle in facts
    )


async def match_new_account_login(
    db: AsyncSession, site: str, *, require_free_slot: bool = True
) -> dict | None:
    """Select only measured live resources for a new, otherwise unbound account."""
    if not site:
        return None
    entry = next(
        (
            item
            for item in load_platform_catalog()["items"]
            if site in {item["id"], item["site"], item["login_url"]}
            or (
                item["id"] in {"xiaohongshu", "bilibili", "douyin"}
                and site in item["allowed_origins"]
            )
        ),
        None,
    )
    if entry is None:
        return None
    if not entry["browser_login_supported"]:
        raise browser_account_service.BrowserAccountError(
            "platform_configuration_required"
            if entry["requires_configuration"]
            else "browser_login_entry_unavailable",
            "此平台尚无固定受管登录入口，请按平台认证方式配置。",
            409,
        )
    platform, rule_id = entry["id"], entry["rule_id"]
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(EdgeNode, EdgeNodeCapacity, EdgeNodeBoot)
            .join(EdgeNodeCapacity, EdgeNodeCapacity.node_id == EdgeNode.id)
            .join(EdgeNodeBoot, EdgeNodeBoot.node_id == EdgeNode.id)
            .where(
                EdgeNode.status == "online",
                EdgeNode.account_capable.is_(True),
                EdgeNode.quarantined.is_(False),
                EdgeNodeCapacity.boot_id == EdgeNode.boot_id,
                EdgeNodeCapacity.valid.is_(True),
                EdgeNodeCapacity.expires_at > now,
                EdgeNodeBoot.boot_id == EdgeNode.boot_id,
                EdgeNodeBoot.status == "active",
                EdgeNodeBoot.stopped_at.is_(None),
            )
            .order_by(EdgeNodeCapacity.occupied_slots, EdgeNode.id)
        )
    ).all()
    busy = False
    for node, capacity, boot in rows:
        if _utc(capacity.observed_at) > now or _utc(capacity.observed_at) < _utc(boot.started_at):
            continue
        facts = (
            capacity.capabilities.get("browser_login_bundles", [])
            if isinstance(capacity.capabilities, dict)
            else []
        )
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("id"), str):
                continue
            bundle = await db.get(BrowserRuntimeBundle, fact["id"])
            rule_version = (
                "0.2.0"
                if platform == "xiaohongshu" and bundle and bundle.version == "4"
                else "0.1.0"
            )
            if (
                bundle
                and (platform == "xiaohongshu" or bundle.version == "4")
                and _bundle_contains_packaged_rule(bundle)
                and load_bundle_login_rule(
                    _ROOT,
                    bundle_name=bundle.name,
                    bundle_version=bundle.version,
                    bundle_manifest=bundle.manifest,
                    rule_id=rule_id,
                    rule_version=rule_version,
                )
                is not None
                and _capacity_reports_rule(
                    capacity, bundle.id, bundle.version, rule_id, rule_version
                )
            ):
                if require_free_slot and capacity.occupied_slots >= capacity.slot_limit:
                    busy = True
                    continue
                return {
                    "site": entry["site"],
                    "node_id": node.id,
                    "runtime_bundle_id": bundle.id,
                    "login_rule_id": rule_id,
                    "login_rule_version": rule_version,
                }
    if busy:
        raise browser_account_service.BrowserAccountError(
            "capacity_full",
            "浏览器登录槽位已被其他会话占用。请完成或关闭已有登录会话后再试；平台列表会自动更新。",
            409,
        )
    if not require_free_slot:
        return {
            "site": entry["site"],
            "login_rule_id": rule_id,
            "login_rule_version": "0.2.0" if platform == "xiaohongshu" else "0.1.0",
        }
    raise browser_account_service.BrowserAccountError(
        "login_resources_unavailable",
        "尚无实际安装此平台登录规则的在线账号节点，请等待管理员完成部署。",
        409,
    )


async def workspace_login_options(db: AsyncSession) -> dict:
    options = login_options()
    for option in options["items"]:
        if not option["browser_login_supported"]:
            continue
        try:
            await match_new_account_login(db, option["site"])
        except browser_account_service.BrowserAccountError as error:
            if error.code == "capacity_full":
                option.update(reason_code=error.code, message=str(error))
            elif error.code == "login_resources_unavailable":
                from backend.services.browser_account_pool import configuration

                if configuration():
                    option.update(
                        reason_code="pool_pending",
                        message="可提交登录请求，系统将按需启动独立浏览器。",
                    )
        else:
            option.update(
                available=True,
                reason_code="requires_scan_verification"
                if option["qr_supported"]
                else "official_window_available",
                message="可打开官方二维码；只有实时官方身份校验通过后才能保存。"
                if option["identity_probe_supported"]
                else "可打开官方登录窗口；尚无可信身份探针，登录后不能保存账号状态。",
            )
    return options
