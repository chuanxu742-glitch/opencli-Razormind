import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.account-cluster.yml"
VALIDATOR_PATH = ROOT / "scripts" / "validate_account_cluster_deployment.py"


spec = importlib.util.spec_from_file_location("account_cluster_validator", VALIDATOR_PATH)
assert spec and spec.loader
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def raw_document():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def resolved_document():
    compose = raw_document()
    values = {
        1: {
            "AGENT_NODE_ID": "browser-account-001",
            "AGENT_NODE_CREDENTIAL_ID": "browser-account-001-credential",
            "AGENT_NODE_CREDENTIAL": "node-1-secret",
        },
        2: {
            "AGENT_NODE_ID": "browser-account-002",
            "AGENT_NODE_CREDENTIAL_ID": "browser-account-002-credential",
            "AGENT_NODE_CREDENTIAL": "node-2-secret",
        },
    }
    for number, node_name in enumerate(validator.NODE_NAMES, start=1):
        service = compose["services"][node_name]
        service["image"] = "sha256:" + (str(number) * 64)
        service["networks"] = {"account-control": None}
        service["environment"].update(
            {
                "CENTRAL_API_URL": "https://control.example.com",
                "API_AUTH_TOKEN": "control-plane-token",
                "BROWSER_RUNTIME_BUNDLE_ID": "opencli-default",
                **values[number],
            }
        )
        service["volumes"] = [
            {
                "type": "volume",
                "source": f"account_node_{number}_profile",
                "target": validator.PROFILE_DIR,
                "volume": {},
            },
            {
                "type": "volume",
                "source": f"account_node_{number}_runtime_state",
                "target": validator.RUNTIME_STATE_DIR,
                "volume": {},
            },
        ]
    compose["networks"]["account-control"]["name"] = "production_control"
    compose["volumes"] = {
        key: {"name": f"production_{key}"} for key in compose["volumes"]
    }
    return compose


def test_account_cluster_recipe_has_two_isolated_managed_nodes():
    validator.validate_compose_file(COMPOSE_PATH)

    compose = raw_document()
    services = compose["services"]
    assert set(services) == {"account-node-1", "account-node-2"}
    assert all("ports" not in service for service in services.values())
    assert all(service["pull_policy"] == "never" for service in services.values())
    assert all(
        service["image"].startswith("${ACCOUNT_CLUSTER_AGENT_IMAGE:?")
        for service in services.values()
    )


def test_account_cluster_validator_accepts_resolved_long_syntax_compose():
    validator.validate_compose_document(resolved_document(), require_resolved=True)


@pytest.mark.parametrize("identity_key", validator.IDENTITY_KEYS)
def test_account_cluster_validator_rejects_duplicate_actual_identities(identity_key):
    compose = resolved_document()
    first = compose["services"]["account-node-1"]["environment"][identity_key]
    compose["services"]["account-node-2"]["environment"][identity_key] = first

    with pytest.raises(
        validator.DeploymentValidationError,
        match=f"duplicates actual {identity_key}",
    ):
        validator.validate_compose_document(compose, require_resolved=True)


def test_account_cluster_validator_rejects_duplicate_mount_targets():
    compose = resolved_document()
    compose["services"]["account-node-2"]["volumes"][1]["target"] = validator.PROFILE_DIR

    with pytest.raises(validator.DeploymentValidationError, match="duplicate volume targets"):
        validator.validate_compose_document(compose, require_resolved=True)


def test_account_cluster_validator_rejects_underlying_named_volume_aliases():
    compose = resolved_document()
    compose["volumes"]["account_node_2_runtime_state"]["name"] = compose["volumes"][
        "account_node_1_runtime_state"
    ]["name"]

    with pytest.raises(validator.DeploymentValidationError, match="underlying named volume"):
        validator.validate_compose_document(compose, require_resolved=True)


def test_account_cluster_validator_rejects_mutable_resolved_image():
    compose = resolved_document()
    compose["services"]["account-node-2"]["image"] = "opencli-account-agent:latest"

    with pytest.raises(validator.DeploymentValidationError, match="immutable image"):
        validator.validate_compose_document(compose, require_resolved=True)


def test_compose_resolution_failure_does_not_expose_subprocess_output(monkeypatch):
    secret = "never-print-this-node-secret"

    def failed_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr=secret)

    monkeypatch.setattr(validator.subprocess, "run", failed_run)

    with pytest.raises(validator.DeploymentValidationError) as error:
        validator.load_resolved_compose_document(COMPOSE_PATH)

    assert secret not in str(error.value)
