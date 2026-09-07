"""Validate static isolation invariants of the account-cluster Compose recipe.

This does not require Docker Engine and intentionally cannot establish browser,
control-plane, or site-account authentication readiness.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_PATH = ROOT / "docker-compose.account-cluster.yml"
NODE_NAMES = ("account-node-1", "account-node-2")
RUNTIME_STATE_DIR = "/var/lib/opencli/account-runtime"
PROFILE_DIR = "/home/agent/.config/chromium"


class DeploymentValidationError(ValueError):
    """The static deployment recipe violates account-isolation requirements."""


def _required_environment(service: dict[str, Any], node_number: int) -> dict[str, str]:
    environment = service.get("environment")
    if not isinstance(environment, dict):
        raise DeploymentValidationError(f"account-node-{node_number} has no environment mapping")
    normalized = {str(key): str(value) for key, value in environment.items()}
    required = {
        "CENTRAL_API_URL",
        "API_AUTH_TOKEN",
        "AGENT_NODE_ID",
        "AGENT_NODE_CREDENTIAL_ID",
        "AGENT_NODE_CREDENTIAL",
        "BROWSER_RUNTIME_BUNDLE_ID",
        "RUNTIME_STATE_DIR",
        "PROFILE_DIR",
    }
    missing = sorted(required - normalized.keys())
    if missing:
        raise DeploymentValidationError(
            f"account-node-{node_number} misses required environment: {', '.join(missing)}"
        )
    return normalized


def validate_compose_document(document: dict[str, Any]) -> None:
    """Raise when the recipe could share account identity, state, or host ports."""
    services = document.get("services")
    if not isinstance(services, dict):
        raise DeploymentValidationError("Compose recipe has no services mapping")
    if set(services) != set(NODE_NAMES):
        raise DeploymentValidationError(
            "Compose recipe must define exactly account-node-1 and account-node-2"
        )

    networks = document.get("networks")
    account_control = networks.get("account-control", {}) if isinstance(networks, dict) else {}
    if not isinstance(networks, dict) or account_control.get("external") is not True:
        raise DeploymentValidationError(
            "account-control must be an external control-plane network"
        )

    all_volumes: set[str] = set()
    node_ids: set[str] = set()
    for number, node_name in enumerate(NODE_NAMES, start=1):
        service = services[node_name]
        if not isinstance(service, dict):
            raise DeploymentValidationError(f"{node_name} is not a service mapping")
        if service.get("ports"):
            raise DeploymentValidationError(f"{node_name} must not publish host ports")
        if service.get("hostname") != node_name:
            raise DeploymentValidationError(f"{node_name} must retain its stable hostname")
        if service.get("networks") != ["account-control"]:
            raise DeploymentValidationError(f"{node_name} must use only account-control")
        if "deploy" in service:
            raise DeploymentValidationError(
                f"{node_name} must not use deploy/replicas for account isolation"
            )

        environment = _required_environment(service, number)
        if environment["RUNTIME_STATE_DIR"] != RUNTIME_STATE_DIR:
            raise DeploymentValidationError(f"{node_name} must use {RUNTIME_STATE_DIR}")
        if environment["PROFILE_DIR"] != PROFILE_DIR:
            raise DeploymentValidationError(f"{node_name} must use {PROFILE_DIR}")
        node_id = environment["AGENT_NODE_ID"]
        if f"ACCOUNT_NODE_{number}_ID" not in node_id:
            raise DeploymentValidationError(f"{node_name} must source its own stable node ID")
        if node_id in node_ids:
            raise DeploymentValidationError(f"{node_name} shares a node ID expression")
        node_ids.add(node_id)

        volumes = service.get("volumes")
        if not isinstance(volumes, list) or len(volumes) != 2:
            raise DeploymentValidationError(
                f"{node_name} must mount exactly profile and runtime-state volumes"
            )
        for volume in volumes:
            if not isinstance(volume, str) or ":" not in volume:
                raise DeploymentValidationError(f"{node_name} has an invalid named-volume mount")
            volume_name, container_path = volume.split(":", 1)
            if container_path not in {PROFILE_DIR, RUNTIME_STATE_DIR}:
                raise DeploymentValidationError(f"{node_name} mounts unexpected account state path")
            if volume_name in all_volumes:
                raise DeploymentValidationError(f"{node_name} shares volume {volume_name}")
            all_volumes.add(volume_name)

    declared_volumes = document.get("volumes")
    if not isinstance(declared_volumes, dict) or not all_volumes <= set(declared_volumes):
        raise DeploymentValidationError("all account volumes must be declared named volumes")


def validate_compose_file(path: Path = DEFAULT_COMPOSE_PATH) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise DeploymentValidationError("Compose document must be a mapping")
    validate_compose_document(document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_PATH)
    args = parser.parse_args()
    validate_compose_file(args.compose_file)
    print(f"static account-cluster deployment validation passed: {args.compose_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
