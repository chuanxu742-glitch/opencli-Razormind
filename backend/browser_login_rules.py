"""Fixed, locally packaged login-rule registry shared by center and node.

Entries describe QR projection support, not successful platform authentication.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_platform_catalog() -> dict:
    """Read audited package metadata, not a remotely supplied rule registry."""
    return json.loads(Path(__file__).with_name("browser_login_catalog.json").read_text("utf-8"))


_OFFICIAL_ENTRIES = {
    (entry["rule_id"], entry["rule_version"]): entry
    for entry in load_platform_catalog()["items"]
    if (entry.get("rule_id") or "").startswith("official-")
}

PACKAGED_RULE_FILES = {
    ("controlled-login-fixture", "1.0.0"): "rules.json",
    ("xiaohongshu-qr", "0.1.0"): "xiaohongshu-qr.json",
    ("xiaohongshu-qr", "0.2.0"): "xiaohongshu-qr.json",
    ("bilibili-qr", "0.1.0"): "bilibili-qr.json",
    ("douyin-qr", "0.1.0"): "douyin-qr.json",
}
PACKAGED_RULE_FILES.update({key: key[0] + ".json" for key in _OFFICIAL_ENTRIES})


def load_packaged_rule(script_host: Path, rule_id: str, version: str) -> dict | None:
    name = PACKAGED_RULE_FILES.get((rule_id, version))
    if name is None:
        return None
    path = script_host / "packs" / "account-login" / name
    if path.is_symlink():
        return None
    try:
        rule = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rule, dict) or rule.get("id") != rule_id or rule.get("version") != version:
        return None
    if (entry := _OFFICIAL_ENTRIES.get((rule_id, version))) is not None:
        digest = hashlib.sha256(
            json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return rule if digest == entry["rule_sha256"] else None
    if rule_id == "controlled-login-fixture" and (
        not isinstance(rule.get("allowed_origins"), list)
        or not rule["allowed_origins"]
        or any(
            origin not in {"http://127.0.0.1:49906", "http://localhost:49906"}
            for origin in rule["allowed_origins"]
        )
        or rule.get("login_url") != "/login"
    ):
        return None
    if rule_id == "xiaohongshu-qr" and (
        rule.get("identity_probe_supported", False) is not (version == "0.2.0")
        or rule.get("platform") != "xiaohongshu"
        or rule.get("allowed_origins") != ["https://www.xiaohongshu.com"]
        or rule.get("allowed_redirect_origins") != ["https://www.xiaohongshu.com"]
        or rule.get("login_url") != "/explore"
        or rule.get("modes") != ["qr"]
        or rule.get("authentication_verified") is not False
    ):
        return None
    if rule_id in {"bilibili-qr", "douyin-qr"}:
        platform = rule_id.removesuffix("-qr")
        origins = (
            ["https://passport.bilibili.com", "https://www.bilibili.com"]
            if platform == "bilibili"
            else ["https://creator.douyin.com"]
        )
        selector = (
            '.login-scan__qrcode img[alt="Scan me!"]'
            if platform == "bilibili"
            else 'img[aria-label="二维码"]'
        )
        if (
            rule.get("platform") != platform
            or rule.get("allowed_origins") != origins
            or rule.get("allowed_redirect_origins") != origins
            or rule.get("login_url") != ("/login" if platform == "bilibili" else "/")
            or rule.get("modes") != ["qr"]
            or rule.get("authentication_verified") is not False
            or rule.get("identity_probe_supported") is not True
            or rule.get("selectors")
            != [{"id": "qr", "selector": selector, "kind": "qr", "frame": "main"}]
        ):
            return None
    return rule


def load_bundle_login_rule(
    repository_root: Path,
    *,
    bundle_name: str,
    bundle_version: str,
    bundle_manifest: dict,
    rule_id: str,
    rule_version: str,
) -> dict | None:
    """Resolve center-side rule assets for an exact, already trusted bundle.

    The caller still verifies DB trust, ownership and installed node evidence.
    A name/version alone cannot select overlay assets or enable an identity probe.
    """
    if bundle_name != "opencli-default" or bundle_version not in {"1", "2", "3", "4"}:
        return None
    manifest_path = (
        repository_root / "chrome/runtime-bundles" / bundle_name / bundle_version / "manifest.json"
    )
    try:
        packaged = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if packaged != bundle_manifest:
        return None
    if (
        (rule_id, rule_version) in _OFFICIAL_ENTRIES
        or rule_id in {"bilibili-qr", "douyin-qr"}
        or (rule_id == "xiaohongshu-qr" and (bundle_version == "4" or rule_version == "0.2.0"))
    ):
        if bundle_version != "4":
            return None
        source = repository_root / "chrome/platform-login-bundle4"
    else:
        source = repository_root / "chrome/script-host"
    return load_packaged_rule(source, rule_id, rule_version)
