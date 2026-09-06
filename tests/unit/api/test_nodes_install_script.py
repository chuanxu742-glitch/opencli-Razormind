"""Install-script rendering tests for edge node bootstrap."""

import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tarfile

import pytest

from backend.api.v1.nodes import _install_script_template
from backend.config import Settings, get_settings


@pytest.mark.parametrize("provider", ["lan", "netbird", "wireguard", "ssh", "custom"])
def test_settings_accepts_reachability_fleet_providers(provider):
    settings = Settings(fleet_network_provider=provider)

    assert settings.fleet_network_provider == provider


@pytest.mark.asyncio
async def test_install_script_endpoint_injects_netbird_and_agent_auth(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "http://center.netbird:8031")
    monkeypatch.setenv("API_AUTH_TOKEN", "center-token")
    monkeypatch.setenv("IMAGE_TAG", "fleet-20260705")
    monkeypatch.setenv("FLEET_NETWORK_PROVIDER", "netbird")
    monkeypatch.setenv("NETBIRD_MODE", "host")
    monkeypatch.setenv("NETBIRD_SETUP_KEY", "nb-setup-key")
    monkeypatch.setenv("NETBIRD_MANAGEMENT_URL", "https://netbird.example:443")
    monkeypatch.setenv("NETBIRD_IMAGE_TAG", "0.58.0")
    get_settings.cache_clear()
    try:
        response = await client.get(
            "/api/v1/nodes/install/agent.sh",
            headers={"Authorization": "Bearer center-token"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.text
    assert "CENTRAL_API_URL=${CENTRAL_API_URL:-http://center.netbird:8031}" in body
    assert "AGENT_API_TOKEN=${AGENT_API_TOKEN:-${API_AUTH_TOKEN:-center-token}}" in body
    assert "FLEET_NETWORK_PROVIDER=${FLEET_NETWORK_PROVIDER:-netbird}" in body
    assert "NETBIRD_MODE=${NETBIRD_MODE:-host}" in body
    assert "NETBIRD_SETUP_KEY=${NETBIRD_SETUP_KEY:-nb-setup-key}" in body
    assert "NETBIRD_MANAGEMENT_URL=${NETBIRD_MANAGEMENT_URL:-https://netbird.example:443}" in body
    assert "NETBIRD_IMAGE_TAG=${NETBIRD_IMAGE_TAG:-0.58.0}" in body
    assert "netbird up" in body
    assert '-e AGENT_ADVERTISE_URL="$AGENT_ADVERTISE_URL"' in body


@pytest.mark.asyncio
async def test_install_script_endpoint_keeps_ssh_as_reachability_provider(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "http://center.ssh:8031")
    monkeypatch.setenv("API_AUTH_TOKEN", "center-token")
    monkeypatch.setenv("FLEET_NETWORK_PROVIDER", "ssh")
    get_settings.cache_clear()
    try:
        response = await client.get(
            "/api/v1/nodes/install/agent.sh",
            headers={"Authorization": "Bearer center-token"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    body = response.text
    assert "CENTRAL_API_URL=${CENTRAL_API_URL:-http://center.ssh:8031}" in body
    assert "FLEET_NETWORK_PROVIDER=${FLEET_NETWORK_PROVIDER:-ssh}" in body
    assert "NETBIRD_MODE=${NETBIRD_MODE:-off}" in body
    assert "ssh)" in body
    assert "SSH provider selected; assuming the SSH tunnel is already established." in body
    assert '-e AGENT_ADVERTISE_URL="$AGENT_ADVERTISE_URL"' in body


def test_inline_install_script_template_keeps_netbird_bootstrap():
    body = _install_script_template(
        "http://center.example:8031",
        image_tag="test-image",
        agent_api_token="center-token",
        fleet_network_provider="netbird",
        netbird_mode="docker",
        netbird_setup_key="setup-key",
        netbird_management_url="https://netbird.example:443",
        netbird_image_tag="0.58.0",
    )

    assert "NETBIRD_MODE=${NETBIRD_MODE:-docker}" in body
    assert "NETBIRD_SETUP_KEY=${NETBIRD_SETUP_KEY:-setup-key}" in body
    assert 'NB_MANAGEMENT_URL="$NETBIRD_MANAGEMENT_URL"' in body
    assert "netbirdio/netbird:${NETBIRD_IMAGE_TAG}" in body
    assert 'AGENT_API_TOKEN="$AGENT_API_TOKEN"' in body
    assert 'AGENT_ADVERTISE_URL="$AGENT_ADVERTISE_URL"' in body


def test_inline_install_script_template_keeps_wireguard_reachability_only():
    body = _install_script_template(
        "http://center.example:8031",
        fleet_network_provider="wireguard",
    )

    assert "FLEET_NETWORK_PROVIDER=${FLEET_NETWORK_PROVIDER:-wireguard}" in body
    assert "wireguard)" in body
    assert "WireGuard provider selected; assuming the WireGuard interface is already up." in body
    assert "install_fleet_network" in body
    assert "install_netbird" in body




@pytest.mark.asyncio
async def test_runtime_bundle_contains_native_agent_packages(client):
    response = await client.get("/api/v1/nodes/install/agent-runtime.tar.gz")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/gzip")
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        names = set(archive.getnames())

    assert "backend/agent_runtimes/registry.py" in names
    assert "backend/agent_runtimes/opentabs_adapter.py" in names
    assert "backend/agent_runtimes/prime_agent_adapter.py" in names
    assert "backend/miniflow/runner.py" in names
    assert "backend/security/url_guard.py" in names
    assert all(
        name.startswith(("backend/agent_runtimes/", "backend/miniflow/", "backend/security/"))
        for name in names
    )


@pytest.mark.asyncio
async def test_runtime_bundle_imports_in_native_agent_layout(client, tmp_path):
    response = await client.get("/api/v1/nodes/install/agent-runtime.tar.gz")
    assert response.status_code == 200

    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    (backend_root / "__init__.py").touch()
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from backend.agent_runtimes.registry import list_runtime_types; "
                "assert {'bbx', 'miniflow', 'opentabs', 'pi', 'prime-agent'} "
                "<= set(list_runtime_types())"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_adapter_bundle_requires_auth_and_contains_only_complete_inventory(client, monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "packaging-fixture-token")
    get_settings.cache_clear()
    try:
        unauthenticated = await client.get("/api/v1/nodes/install/opencli-adapters.tar.gz")
        assert unauthenticated.status_code == 401
        response = await client.get(
            "/api/v1/nodes/install/opencli-adapters.tar.gz",
            headers={"Authorization": "Bearer packaging-fixture-token"},
        )
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        inventory = json.load(archive.extractfile("integrations/opencli/adapter-pack.json"))
        expected = {
            "scripts/install-opencli-adapters.mjs",
            "integrations/opencli/adapter-pack.json",
            "integrations/opencli/LICENSE.opencli",
            *(f"integrations/opencli/{file}" for file in inventory["files"]),
        }
        assert set(archive.getnames()) == expected
        assert all(member.isfile() and member.size > 0 for member in archive.getmembers())
        assert all(entry["modulePath"] in inventory["files"] for entry in inventory["commands"])


@pytest.mark.asyncio
async def test_adapter_bundle_missing_payload_is_service_error_not_partial_success(client, monkeypatch):
    original = Path.read_bytes

    def missing_adapter(path):
        if path.as_posix().endswith("/integrations/opencli/ebay/search.js"):
            raise FileNotFoundError("fixture missing adapter")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", missing_adapter)
    response = await client.get("/api/v1/nodes/install/opencli-adapters.tar.gz")
    assert response.status_code == 503
    assert not response.headers["content-type"].startswith("application/gzip")
