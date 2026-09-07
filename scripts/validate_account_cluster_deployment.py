"""Validate isolation invariants in the resolved account-cluster Compose model.

The CLI resolves Compose interpolation in memory, validates actual identities and
resource aliases, and never emits the resolved model because it contains secrets.
It cannot establish browser, control-plane, storage-encryption, or site-account
readiness.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_PATH = ROOT / "docker-compose.account-cluster.yml"
NODE_NAMES = ("account-node-1", "account-node-2")
RUNTIME_STATE_DIR = "/var/lib/opencli/account-runtime"
PROFILE_DIR = "/home/agent/.config/chromium"
EXPECTED_VOLUME_TARGETS = {PROFILE_DIR, RUNTIME_STATE_DIR}
IDENTITY_KEYS = ("AGENT_NODE_ID", "AGENT_NODE_CREDENTIAL_ID")
IMMUTABLE_IMAGE = re.compile(r"(?:sha256:|[^@\s]+@sha256:)[0-9a-f]{64}")


class DeploymentValidationError(ValueError):
    """The deployment recipe violates account-isolation requirements."""


def _required_environment(service: dict[str, Any], node_number: int) -> dict[str, str]:
    environment = service.get("environment")
    if not isinstance(environment, dict):
        raise DeploymentValidationError(f"account-node-{node_number} has no environment mapping")
    normalized = {
        str(key): "" if value is None else str(value)
        for key, value in environment.items()
    }
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
    missing = sorted(key for key in required if not normalized.get(key))
    if missing:
        raise DeploymentValidationError(
            f"account-node-{node_number} misses required environment: {', '.join(missing)}"
        )
    return normalized


def _network_names(service: dict[str, Any], node_name: str) -> set[str]:
    networks = service.get("networks")
    if isinstance(networks, list):
        return {str(network) for network in networks}
    if isinstance(networks, dict):
        return {str(network) for network in networks}
    raise DeploymentValidationError(f"{node_name} has an invalid networks declaration")


def _volume_mount(volume: Any, node_name: str) -> tuple[str, str]:
    if isinstance(volume, str):
        parts = volume.split(":", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise DeploymentValidationError(f"{node_name} has an invalid named-volume mount")
        return parts[0], parts[1]
    if isinstance(volume, dict):
        if volume.get("type") != "volume":
            raise DeploymentValidationError(f"{node_name} account state must use named volumes")
        source = volume.get("source")
        target = volume.get("target")
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            raise DeploymentValidationError(f"{node_name} has an invalid named-volume mount")
        return source, target
    raise DeploymentValidationError(f"{node_name} has an invalid named-volume mount")


def _actual_volume_name(source: str, declared_volumes: dict[str, Any]) -> str:
    declaration = declared_volumes.get(source)
    if source not in declared_volumes:
        aliases = [
            key
            for key, value in declared_volumes.items()
            if isinstance(value, dict) and value.get("name") == source
        ]
        if len(aliases) != 1:
            raise DeploymentValidationError("all account volumes must be declared named volumes")
        declaration = declared_volumes[aliases[0]]
    if declaration is None:
        return source
    if not isinstance(declaration, dict):
        raise DeploymentValidationError("account volume declarations must be mappings")
    actual_name = declaration.get("name", source)
    if not isinstance(actual_name, str) or not actual_name:
        raise DeploymentValidationError("account volume declarations need non-empty names")
    return actual_name


def validate_compose_document(document: dict[str, Any], *, require_resolved: bool = False) -> None:
    """Raise when a Compose model could share identity, state, or host ports."""
    services = document.get("services")
    if not isinstance(services, dict):
        raise DeploymentValidationError("Compose recipe has no services mapping")
    if set(services) != set(NODE_NAMES):
        raise DeploymentValidationError(
            "Compose recipe must define exactly account-node-1 and account-node-2"
        )

    networks = document.get("networks")
    account_control = networks.get("account-control", {}) if isinstance(networks, dict) else {}
    if not isinstance(account_control, dict) or account_control.get("external") is not True:
        raise DeploymentValidationError("account-control must be an external control-plane network")

    declared_volumes = document.get("volumes")
    if not isinstance(declared_volumes, dict):
        raise DeploymentValidationError("all account volumes must be declared named volumes")

    actual_volumes: set[str] = set()
    identities = {key: set() for key in IDENTITY_KEYS}
    for number, node_name in enumerate(NODE_NAMES, start=1):
        service = services[node_name]
        if not isinstance(service, dict):
            raise DeploymentValidationError(f"{node_name} is not a service mapping")
        if service.get("ports"):
            raise DeploymentValidationError(f"{node_name} must not publish host ports")
        if service.get("hostname") != node_name:
            raise DeploymentValidationError(f"{node_name} must retain its stable hostname")
        if _network_names(service, node_name) != {"account-control"}:
            raise DeploymentValidationError(f"{node_name} must use only account-control")
        if "deploy" in service:
            raise DeploymentValidationError(
                f"{node_name} must not use deploy/replicas for account isolation"
            )
        if service.get("pull_policy") != "never":
            raise DeploymentValidationError(f"{node_name} must never pull a fallback image")

        image = service.get("image")
        if not isinstance(image, str):
            raise DeploymentValidationError(f"{node_name} has no image reference")
        if require_resolved:
            if not IMMUTABLE_IMAGE.fullmatch(image):
                raise DeploymentValidationError(
                    f"{node_name} must use a resolved immutable image ID or registry digest"
                )
        elif not (image.startswith("${ACCOUNT_CLUSTER_AGENT_IMAGE:?") and image.endswith("}")):
            raise DeploymentValidationError(
                f"{node_name} must require ACCOUNT_CLUSTER_AGENT_IMAGE without a release fallback"
            )

        environment = _required_environment(service, number)
        if environment["RUNTIME_STATE_DIR"] != RUNTIME_STATE_DIR:
            raise DeploymentValidationError(f"{node_name} must use {RUNTIME_STATE_DIR}")
        if environment["PROFILE_DIR"] != PROFILE_DIR:
            raise DeploymentValidationError(f"{node_name} must use {PROFILE_DIR}")
        if require_resolved and not environment["CENTRAL_API_URL"].startswith("https://"):
            raise DeploymentValidationError(
                f"{node_name} must use an HTTPS control-plane URL in production preflight"
            )

        for key in IDENTITY_KEYS:
            value = environment[key]
            if require_resolved:
                if "${" in value:
                    raise DeploymentValidationError(f"{node_name} has unresolved {key}")
            elif f"ACCOUNT_NODE_{number}_" not in value:
                raise DeploymentValidationError(f"{node_name} must source its own {key}")
            if value in identities[key]:
                raise DeploymentValidationError(f"{node_name} duplicates actual {key}")
            identities[key].add(value)

        volumes = service.get("volumes")
        if not isinstance(volumes, list) or len(volumes) != 2:
            raise DeploymentValidationError(
                f"{node_name} must mount exactly profile and runtime-state volumes"
            )
        targets: set[str] = set()
        for volume in volumes:
            source, target = _volume_mount(volume, node_name)
            if target in targets:
                raise DeploymentValidationError(f"{node_name} has duplicate volume targets")
            targets.add(target)
            actual_name = _actual_volume_name(source, declared_volumes)
            if actual_name in actual_volumes:
                raise DeploymentValidationError(f"{node_name} shares an underlying named volume")
            actual_volumes.add(actual_name)
        if targets != EXPECTED_VOLUME_TARGETS:
            raise DeploymentValidationError(
                f"{node_name} must mount exactly the profile and runtime-state targets"
            )


def validate_compose_file(path: Path = DEFAULT_COMPOSE_PATH) -> None:
    """Validate the checked-in recipe without resolving secret interpolation."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise DeploymentValidationError("Compose document must be a mapping")
    validate_compose_document(document)


def load_resolved_compose_document(path: Path = DEFAULT_COMPOSE_PATH) -> dict[str, Any]:
    """Resolve Compose in memory without logging its secret-bearing output."""
    command = ["docker", "compose", "-f", str(path), "config", "--format", "json"]
    try:
        completed = subprocess.run(
            command,
            cwd=path.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError as exc:
        raise DeploymentValidationError(
            "docker compose is required for resolved preflight"
        ) from exc
    if completed.returncode != 0:
        raise DeploymentValidationError(
            "docker compose config failed; verify required deployment variables privately"
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentValidationError(
            "docker compose returned an invalid resolved model"
        ) from exc
    if not isinstance(document, dict):
        raise DeploymentValidationError("resolved Compose document must be a mapping")
    return document


def validate_resolved_compose_file(path: Path = DEFAULT_COMPOSE_PATH) -> None:
    validate_compose_document(load_resolved_compose_document(path), require_resolved=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_PATH)
    args = parser.parse_args()
    try:
        validate_compose_file(args.compose_file)
        validate_resolved_compose_file(args.compose_file)
    except (OSError, DeploymentValidationError, yaml.YAMLError) as exc:
        print(f"account-cluster deployment validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"resolved account-cluster deployment validation passed: {args.compose_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
