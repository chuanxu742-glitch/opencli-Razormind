"""Check the agent image's Python COPY closure outside the source checkout."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


def test_agent_image_copied_sources_import_without_checkout(tmp_path):
    root = Path(__file__).resolve().parents[2]
    stage = tmp_path / "image"
    stage.mkdir()
    for line in (root / "agent/Dockerfile").read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY backend/"):
            continue
        _, source, destination = shlex.split(line)
        source_path = root / source
        target = stage / destination.removeprefix("./")
        if source_path.is_dir():
            shutil.copytree(source_path, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
    (stage / "backend/__init__.py").touch()
    # -I -S bypasses cwd, PYTHONPATH and editable-install .pth files. Supply
    # only declared site packages and the copied image source tree.
    code = """
import json, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import backend.agent_server
import backend.skills.record
import playwright.async_api
import PIL.Image
print(json.dumps({name: module.__file__ for name, module in sys.modules.items()
                  if name.startswith('backend.') and getattr(module, '__file__', None)}))
"""
    env = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///:memory:", "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(stage), sysconfig.get_paths()["purelib"]],
        cwd=stage,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    modules = json.loads(result.stdout.splitlines()[-1])
    assert "backend.agent_server" in modules
    assert "backend.skills.record" in modules
    assert all(Path(path).is_relative_to(stage) for path in modules.values())
