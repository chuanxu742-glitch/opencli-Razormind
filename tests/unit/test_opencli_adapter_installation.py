"""Consumer contracts for isolated official OpenCLI adapter installations."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install-opencli-adapters.mjs"
SOURCE = ROOT / "integrations/opencli"


@pytest.fixture(scope="module")
def official():
    node = shutil.which("node")
    archive = Path(os.environ.get("OPENCLI_TEST_ARCHIVE", "__missing__")).resolve()
    dependencies = Path(os.environ.get("OPENCLI_TEST_PACKAGE", "__missing__")).resolve() / "node_modules"
    if not node or not archive.is_file() or not dependencies.is_dir():
        pytest.fail("Node, OPENCLI_TEST_ARCHIVE and OPENCLI_TEST_PACKAGE with isolated runtime dependencies are required")
    expected = "2M+oPc70R1jNGzKzNrsm3fN4/gdvxCKlla7s9eaaTjkDjlzHpoZFN1YdV01A185kwCTN/ChOg+rbO4epO73c3w=="
    assert base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode() == expected
    return Path(node), archive, dependencies


@pytest.fixture
def runtime(tmp_path, official, request):
    node, archive, dependencies = official
    prefix = tmp_path / "prefix"
    layout = getattr(request, "param", "node_modules")
    package = prefix / layout / "@jackwener/opencli"
    unpack = tmp_path / "unpack"
    with tarfile.open(archive) as bundle:
        bundle.extractall(unpack, filter="data")
    package.parent.mkdir(parents=True)
    shutil.move(unpack / "package", package)
    shutil.copytree(dependencies, package / "node_modules")
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / node.name
    shutil.copy2(node, executable)
    cache = tmp_path / "cache"
    cache.mkdir()
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    source = tmp_path / "source"
    shutil.copytree(SOURCE, source)
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(home), USERPROFILE=str(home), PATH=str(bin_dir),
               NPM_CONFIG_PREFIX=str(prefix), NPM_CONFIG_CACHE=str(cache),
               XDG_CACHE_HOME=str(cache), TEMP=str(temporary), TMP=str(temporary))
    return {"node": executable, "package": package, "prefix": prefix, "home": home,
            "source": source, "env": env, "root": tmp_path, "installer": INSTALLER}


def run(runtime, *args):
    return subprocess.run([str(runtime["node"]), *map(str, args)], cwd=runtime["root"],
                          env=runtime["env"], capture_output=True, text=True,
                          encoding="utf-8", timeout=90, check=False)


def install(runtime):
    return run(runtime, runtime["installer"], "--source", runtime["source"],
               "--prefix", runtime["prefix"], "--home", runtime["home"])


def cli(runtime, *args):
    return run(runtime, runtime["package"] / "dist/src/main.js", *args)


def snapshot(root):
    if not root.exists():
        return {}
    return {file.relative_to(root).as_posix(): file.read_bytes()
            for file in root.rglob("*") if file.is_file() and not file.is_symlink()}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def user_command(runtime, *, site="fmhy", name="preserved", aliases=None, filename="user.js"):
    relative = f"{site}/{filename}"
    file = runtime["home"] / ".opencli/clis" / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        "import { cli } from '@jackwener/opencli/registry';\ncli({"
        f"site: '{site}', name: '{name}', aliases: {json.dumps(aliases or [])},"
        "access: 'read', browser: false, func: async () => [{marker: 'unmanaged-survives'}]});\n",
        encoding="utf-8",
    )
    return {"site": site, "name": name, "aliases": aliases or [], "access": "read", "browser": False,
            "modulePath": relative, "sourceFile": relative, "args": [], "customMetadata": {"preserve": True}}



def test_alias_collision_after_function_rejects_without_mutation_and_keeps_user_cli(runtime):
    file = runtime["home"] / ".opencli/clis/ebay/custom.js"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        "import { cli } from '@jackwener/opencli/registry';\n"
        "cli({\n"
        "  site: 'ebay', name: 'custom', access: 'read', browser: false,\n"
        "  func: async () => [{owner: 'unmanaged'}],\n"
        "  aliases: ['search'],\n"
        "});\n",
    )
    baseline = cli(runtime, "ebay", "search", "-f", "json")
    assert baseline.returncode == 0, baseline.stderr
    assert json.loads(baseline.stdout) == [{"owner": "unmanaged"}]
    before = snapshot(runtime["home"] / ".opencli")
    result = install(runtime)
    assert result.returncode != 0
    assert "Unmanaged command/alias conflict: ebay/search" in result.stderr
    assert snapshot(runtime["home"] / ".opencli") == before
    after = cli(runtime, "ebay", "search", "-f", "json")
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout) == [{"owner": "unmanaged"}]


def test_native_scan_matches_loader_scope_and_actual_fmhy_helpers(runtime):
    root = runtime["home"] / ".opencli"
    clis = root / "clis"
    shutil.copytree(ROOT / "integrations/opencli/fmhy", clis / "fmhy")
    (clis / "fmhy/helper.js").write_text(
        "import { cli } from '@jackwener/opencli/registry';\n"
        "export const helper = () => \"cli({site: 'ebay', name: 'search'})\";\n",
        encoding="utf-8",
    )
    nested = clis / "ebay/nested/ignored.js"
    nested.parent.mkdir(parents=True)
    nested.write_text(
        "import { cli } from '@jackwener/opencli/registry';\n"
        "cli({site: 'ebay', name: 'nested', aliases: ['search'], access: 'read', func: async () => []});\n",
        encoding="utf-8",
    )
    (clis / "ebay/ignored.yml").write_text(
        "site: ebay\nname: ignored\naliases: [search]\n",
        encoding="utf-8",
    )
    (clis / "ebay/ignored.yaml").write_text(
        "site: ebay\nname: ignored-yaml\naliases: [search]\n",
        encoding="utf-8",
    )
    result = install(runtime)
    assert result.returncode == 0, result.stderr
    help_result = cli(runtime, "fmhy", "search", "--help")
    assert help_result.returncode == 0, help_result.stderr
    ebay_help = cli(runtime, "ebay", "search", "--help")
    assert ebay_help.returncode == 0, ebay_help.stderr

@pytest.mark.parametrize("runtime", ["node_modules", "lib/node_modules"], indirect=True)
def test_both_layouts_discover_public_exports_without_package_business_mutation(runtime):
    package = runtime["package"]
    before = snapshot(package / "clis"), (package / "cli-manifest.json").read_bytes()
    result = install(runtime)
    assert result.returncode == 0, result.stderr
    catalog = cli(runtime, "list", "-f", "json")
    assert catalog.returncode == 0, catalog.stderr
    inventory = json.loads((runtime["source"] / "adapter-pack.json").read_text(encoding="utf-8"))
    commands = {(row["site"], row["name"]): row for row in json.loads(catalog.stdout)}
    for entry in inventory["commands"]:
        assert (entry["site"], entry["name"]) in commands
        help_result = cli(runtime, entry["site"], entry["name"], "--help")
        assert help_result.returncode == 0, help_result.stderr
    missing_credentials = cli(runtime, "ebay", "search", "camera", "-f", "json")
    assert missing_credentials.returncode != 0
    assert before == (snapshot(package / "clis"), (package / "cli-manifest.json").read_bytes())
    assert not (runtime["home"] / ".opencli/cli-manifest.json").exists()


@pytest.mark.parametrize("with_manifest", [False, True])
def test_repeat_preserves_unmanaged_commands_and_manifest_metadata(runtime, with_manifest):
    entry = user_command(runtime)
    root = runtime["home"] / ".opencli"
    if with_manifest:
        write_json(root / "cli-manifest.json", [entry])
    extra = root / "clis/fmhy/notes.txt"
    extra.write_bytes(b"user-owned bytes")
    for _ in range(2):
        result = install(runtime)
        assert result.returncode == 0, result.stderr
        result = cli(runtime, "fmhy", "preserved", "-f", "json")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [{"marker": "unmanaged-survives"}]
    assert extra.read_bytes() == b"user-owned bytes"
    if with_manifest:
        manifest = json.loads((root / "cli-manifest.json").read_text(encoding="utf-8"))
        assert next(command for command in manifest if command["site"] == "fmhy") == entry
        result = cli(runtime, "ebay", "search", "--help")
        assert result.returncode == 0, result.stderr
    else:
        assert not (root / "cli-manifest.json").exists()


@pytest.mark.parametrize("conflict", ["path", "scanned-key", "manifest-alias", "bad-manifest", "runtime-json", "runtime-directory"])
def test_conflicts_fail_without_overwriting_user_bytes(runtime, conflict):
    root = runtime["home"] / ".opencli"
    root.mkdir()
    if conflict == "path":
        target = root / "clis/ebay/search.js"
        target.parent.mkdir(parents=True)
        target.write_text("user owns this file", encoding="utf-8")
    elif conflict == "scanned-key":
        user_command(runtime, site="ebay", name="search", filename="other.js")
    elif conflict == "manifest-alias":
        entry = user_command(runtime, site="ebay", name="mine", aliases=["search"])
        write_json(root / "cli-manifest.json", [entry])
    elif conflict == "bad-manifest":
        (root / "cli-manifest.json").write_text("{broken", encoding="utf-8")
    elif conflict == "runtime-json":
        write_json(root / "package.json", {"type": "module", "name": "user-project"})
    else:
        target = root / "node_modules/@jackwener/opencli"
        target.mkdir(parents=True)
        (target / "user.txt").write_bytes(b"not a managed runtime link")
    before = snapshot(root)
    result = install(runtime)
    assert result.returncode != 0
    assert snapshot(root) == before


def test_owned_update_retires_only_recorded_files(runtime):
    source = runtime["source"]
    inventory_path = source / "adapter-pack.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    retired = "amazon/retired.js"
    inventory["files"].append(retired)
    (source / retired).write_text("export const retired = true;\n", encoding="utf-8")
    write_json(inventory_path, inventory)
    assert install(runtime).returncode == 0
    root = runtime["home"] / ".opencli"
    extra = root / "clis/amazon/unmanaged.txt"
    extra.write_bytes(b"keep")
    inventory["files"].remove(retired)
    write_json(inventory_path, inventory)
    (source / retired).unlink()
    changed = source / "ebay/search.js"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n// managed revision\n", encoding="utf-8")
    result = install(runtime)
    assert result.returncode == 0, result.stderr
    assert not (root / "clis" / retired).exists()
    assert extra.read_bytes() == b"keep"
    assert (root / "clis/ebay/search.js").read_bytes() == changed.read_bytes()
    assert cli(runtime, "ebay", "search", "--help").returncode == 0


@pytest.mark.parametrize("drift", ["file", "retired", "receipt", "manifest"])
def test_drift_blocks_update_before_any_publication(runtime, drift):
    root = runtime["home"] / ".opencli"
    if drift == "manifest":
        write_json(root / "cli-manifest.json", [])
    assert install(runtime).returncode == 0
    if drift in {"file", "retired"}:
        (root / "clis/ebay/search.js").write_bytes(b"user changed managed adapter")
        if drift == "retired":
            inventory_file = runtime["source"] / "adapter-pack.json"
            inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
            inventory["files"].remove("ebay/search.js")
            inventory["commands"] = [entry for entry in inventory["commands"] if entry["modulePath"] != "ebay/search.js"]
            write_json(inventory_file, inventory)
    elif drift == "receipt":
        (root / ".opencli-admin-adapters.json").write_text("{}", encoding="utf-8")
    else:
        manifest_file = root / "cli-manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest[0]["description"] = "user customization"
        write_json(manifest_file, manifest)
    before = snapshot(root)
    result = install(runtime)
    assert result.returncode != 0
    assert snapshot(root) == before


@pytest.mark.parametrize("failure", ["version", "missing-file", "escape", "dependencies", "helper-closure"])
def test_preflight_failure_does_not_create_runtime(runtime, failure):
    if failure == "version":
        file = runtime["package"] / "package.json"
        package = json.loads(file.read_text(encoding="utf-8"))
        package["version"] = "1.8.6"
        write_json(file, package)
    elif failure == "missing-file":
        (runtime["source"] / "ebay/search.js").unlink()
    elif failure == "escape":
        file = runtime["source"] / "adapter-pack.json"
        inventory = json.loads(file.read_text(encoding="utf-8"))
        inventory["files"].append("../outside.js")
        write_json(file, inventory)
    elif failure == "helper-closure":
        file = runtime["source"] / "adapter-pack.json"
        inventory = json.loads(file.read_text(encoding="utf-8"))
        inventory["files"].remove("ebay/shared.js")
        write_json(file, inventory)
    else:
        shutil.rmtree(runtime["package"] / "node_modules")
    result = install(runtime)
    assert result.returncode != 0
    assert not (runtime["home"] / ".opencli").exists()


def test_runtime_junction_conflict_preserves_other_package(runtime):
    root = runtime["home"] / ".opencli"
    link = root / "node_modules/@jackwener/opencli"
    link.parent.mkdir(parents=True)
    other = runtime["root"] / "other-runtime"
    other.mkdir()
    (other / "user.txt").write_bytes(b"do not delete")
    result = run(runtime, "--input-type=module", "-e",
                 "import fs from 'node:fs'; fs.symlinkSync(process.argv[1],process.argv[2],process.platform==='win32'?'junction':'dir');",
                 other, link)
    assert result.returncode == 0, result.stderr
    result = install(runtime)
    assert result.returncode != 0
    assert link.is_symlink()
    assert (other / "user.txt").read_bytes() == b"do not delete"
    assert not (root / "clis").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode-bit boundary; Windows junction behavior is exercised separately")
def test_unwritable_home_rejects_without_partial_pack(runtime):
    home = runtime["home"]
    home.chmod(0o555)
    try:
        result = install(runtime)
        assert result.returncode != 0
        assert not (home / ".opencli").exists()
    finally:
        home.chmod(0o755)


@pytest.mark.asyncio
async def test_downloaded_api_bundle_installs_and_executes_user_cli(client, runtime):
    response = await client.get("/api/v1/nodes/install/opencli-adapters.tar.gz")
    assert response.status_code == 200
    target = runtime["root"] / "download"
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as bundle:
        bundle.extractall(target, filter="data")
    runtime["installer"] = target / "scripts/install-opencli-adapters.mjs"
    runtime["source"] = target / "integrations/opencli"
    result = install(runtime)
    assert result.returncode == 0, result.stderr
    result = cli(runtime, "ebay", "product", "--help")
    assert result.returncode == 0, result.stderr
    result = cli(runtime, "list", "-f", "json")
    assert result.returncode == 0, result.stderr
    assert {"amazon", "taobao", "coupang", "ebay"} <= {row["site"] for row in json.loads(result.stdout)}


@pytest.mark.parametrize(
    "runtime", ["node_modules" if os.name == "nt" else "lib/node_modules"], indirect=True
)
def test_real_npm_same_version_restore_removes_old_business_pollution(runtime):
    npm = shutil.which("npm")
    if not npm:
        pytest.fail("npm is required for the isolated managed upgrade contract")
    executable = Path(npm).resolve()
    candidates = [executable, executable.parent / "node_modules/npm/bin/npm-cli.js"]
    npm_cli = next((file for file in candidates if file.name == "npm-cli.js" and file.is_file()), None)
    if npm_cli is None:
        pytest.fail("Cannot locate npm-cli.js for an isolated native npm invocation")
    package = runtime["package"]
    before = snapshot(package / "clis"), (package / "cli-manifest.json").read_bytes()
    for relative in ("clis/ebay/search.js", "clis/amazon/admin-locale.js", "old-commerce-receipt.json"):
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"legacy managed business pollution")
    (package / "cli-manifest.json").write_text("[]", encoding="utf-8")
    # This is the deployment sequence, not a fixture restore/copy shortcut.
    for operation in (
        ["uninstall", "-g", "--prefix", runtime["prefix"], "--ignore-scripts", "@jackwener/opencli"],
        ["install", "-g", "--prefix", runtime["prefix"], "--ignore-scripts",
         "--registry=https://registry.npmjs.org", "@jackwener/opencli@1.8.7"],
    ):
        result = subprocess.run(
            [str(runtime["node"]), str(npm_cli), *map(str, operation)],
            cwd=runtime["root"], env=runtime["env"], capture_output=True,
            text=True, encoding="utf-8", timeout=240, check=False,
        )
        assert result.returncode == 0, result.stderr
        if operation[0] == "uninstall":
            assert not package.exists()
    assert before == (snapshot(package / "clis"), (package / "cli-manifest.json").read_bytes())
    assert not (package / "old-commerce-receipt.json").exists()
    result = run(runtime, ROOT / "scripts/patch-opencli.js", runtime["prefix"])
    assert result.returncode == 0, result.stderr
    result = install(runtime)
    assert result.returncode == 0, result.stderr
    result = cli(runtime, "list", "-f", "json")
    assert result.returncode == 0, result.stderr
    assert {"search", "product"} == {
        row["name"] for row in json.loads(result.stdout) if row["site"] == "ebay"
    }
