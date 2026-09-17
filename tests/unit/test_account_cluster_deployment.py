import copy
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
    for number, node_name in enumerate(compose["services"], start=1):
        service = compose["services"][node_name]
        service["image"] = "sha256:" + (str(number) * 64)
        service["networks"] = {"account-control": None}
        service["environment"].update(
            {
                "CENTRAL_API_URL": "https://control.example.com",
                "AGENT_ADVERTISE_URL": f"https://account-node-{number}.example.com",
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
    compose["volumes"] = {key: {"name": f"production_{key}"} for key in compose["volumes"]}
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


@pytest.mark.parametrize("key", ["network_mode", "pid", "ipc"])
def test_shared_namespace_is_rejected(key):
    compose = resolved_document()
    compose["services"]["account-node-1"][key] = "host"
    with pytest.raises(validator.DeploymentValidationError, match="independent namespaces"):
        validator.validate_compose_document(compose, require_resolved=True)


def test_recipe_requires_https_entry_and_current_bundle():
    for number, service in enumerate(raw_document()["services"].values(), 1):
        assert service["environment"]["AGENT_ADVERTISE_URL"].startswith(
            f"${{ACCOUNT_NODE_{number}_ADVERTISE_URL:?"
        )
        assert service["environment"]["BROWSER_RUNTIME_BUNDLE_MANIFEST"] == (
            "/opt/browser-runtime-bundles/opencli-default/4/manifest.json"
        )


@pytest.mark.parametrize("count", [1, 3, 5])
def test_any_positive_number_of_isolated_nodes(count):
    compose = resolved_document()
    first = copy.deepcopy(compose["services"]["account-node-1"])
    compose["services"] = {}
    for number in range(1, count + 1):
        node = copy.deepcopy(first)
        node["hostname"] = f"account-node-{number}"
        for key in validator.IDENTITY_KEYS:
            node["environment"][key] = f"{key}-{number}"
        node["environment"]["AGENT_ADVERTISE_URL"] = f"https://node-{number}.example.com"
        for index, suffix in enumerate(("profile", "runtime_state")):
            name = f"account_node_{number}_{suffix}"
            node["volumes"][index]["source"] = name
            compose["volumes"][name] = {"name": f"deployment_{name}"}
        compose["services"][node["hostname"]] = node
    validator.validate_compose_document(compose, require_resolved=True)


@pytest.mark.parametrize("name", [None, "account-node-0", "account-node-01", "proxy"])
def test_empty_or_non_node_recipe_is_rejected(name):
    compose = resolved_document()
    compose["services"] = {} if name is None else {name: compose["services"]["account-node-1"]}
    with pytest.raises(validator.DeploymentValidationError, match="one or more"):
        validator.validate_compose_document(compose, require_resolved=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://node.local",
        "https://",
        "https://user:secret@node.local",
        "https://node.local/path",
        "https://node.local?x=1",
        "https://node.local:bad",
        "https://node.local/#fragment",
    ],
)
def test_invalid_advertised_url_is_rejected_without_echoing_url(url):
    compose = resolved_document()
    compose["services"]["account-node-1"]["environment"]["AGENT_ADVERTISE_URL"] = url
    with pytest.raises(validator.DeploymentValidationError, match="valid HTTPS") as error:
        validator.validate_compose_document(compose, require_resolved=True)
    assert url not in str(error.value)


def test_shared_https_endpoint_is_rejected_even_with_alias_syntax():
    compose = resolved_document()
    compose["services"]["account-node-1"]["environment"]["AGENT_ADVERTISE_URL"] = (
        "https://NODE.example.com"
    )
    compose["services"]["account-node-2"]["environment"]["AGENT_ADVERTISE_URL"] = (
        "https://node.example.com:443/"
    )
    with pytest.raises(validator.DeploymentValidationError, match="duplicates HTTPS"):
        validator.validate_compose_document(compose, require_resolved=True)


def test_nonsequential_raw_node_numbers_keep_own_variable_contract():
    compose = raw_document()
    node = copy.deepcopy(compose["services"].pop("account-node-2"))
    node["hostname"] = "account-node-9"
    node["environment"] = {
        key: str(value).replace("ACCOUNT_NODE_2_", "ACCOUNT_NODE_9_")
        for key, value in node["environment"].items()
    }
    compose["services"]["account-node-9"] = node
    validator.validate_compose_document(compose)


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
