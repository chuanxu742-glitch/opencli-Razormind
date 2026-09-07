import importlib.util
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


def test_account_cluster_recipe_has_two_isolated_managed_nodes():
    validator.validate_compose_file(COMPOSE_PATH)

    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"account-node-1", "account-node-2"}
    assert all("ports" not in service for service in services.values())
    first_node_id = services["account-node-1"]["environment"]["AGENT_NODE_ID"]
    second_node_id = services["account-node-2"]["environment"]["AGENT_NODE_ID"]
    assert first_node_id != second_node_id


def test_account_cluster_validator_rejects_shared_account_state():
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    compose["services"]["account-node-2"]["volumes"][1] = (
        "account_node_1_runtime_state:/var/lib/opencli/account-runtime"
    )

    with pytest.raises(validator.DeploymentValidationError, match="shares volume"):
        validator.validate_compose_document(compose)

