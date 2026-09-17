import json
import shutil
from pathlib import Path

from backend.browser_login_rules import load_packaged_rule, load_platform_catalog

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "chrome/platform-login-bundle4"


def test_catalog_is_audited_and_overlay_copy_matches():
    catalog = load_platform_catalog()
    assert catalog == json.loads((OVERLAY / "platform-catalog.json").read_text("utf-8"))
    assert catalog["source"]["version"] == "1.8.7"
    assert len(catalog["items"]) == 176
    assert len({item["id"] for item in catalog["items"]}) == 176
    assert sum(item["official_login_command"] for item in catalog["items"]) == 67
    assert sum(item["browser_login_supported"] for item in catalog["items"]) == 66
    for item in catalog["items"]:
        if not item["browser_login_supported"]:
            continue
        rule = load_packaged_rule(OVERLAY, item["rule_id"], item["rule_version"])
        assert rule is not None
        if item["id"] not in {"xiaohongshu", "bilibili", "douyin"}:
            assert rule["modes"] == ["form"]
            assert not rule["qr_supported"] and not rule["identity_probe_supported"]
            assert not rule["authentication_verified"]


def test_modified_official_rule_is_not_admitted(tmp_path):
    target = tmp_path / "packs/account-login"
    target.mkdir(parents=True)
    source = OVERLAY / "packs/account-login/official-github.json"
    shutil.copy(source, target)
    assert load_packaged_rule(tmp_path, "official-github", "0.1.0")
    rule = json.loads(source.read_text("utf-8"))
    rule["identity_probe_supported"] = True
    (target / source.name).write_text(json.dumps(rule), encoding="utf-8")
    assert load_packaged_rule(tmp_path, "official-github", "0.1.0") is None
