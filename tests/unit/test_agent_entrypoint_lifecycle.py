"""POSIX lifecycle regression; run in Linux CI with pytest --no-cov on this file."""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "agent" / "entrypoint.sh"


@unittest.skipUnless(os.name == "posix", "requires POSIX process and signal semantics")
class AgentEntrypointLifecycleTests(unittest.TestCase):
    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _fixture(self, root: Path, *, embedded: bool) -> tuple[dict[str, str], Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        state = root / "state"
        state.mkdir()
        fake_node_root = root / "node-root"
        (fake_node_root / "@jackwener" / "opencli" / "dist" / "src").mkdir(parents=True)
        (fake_node_root / "@jackwener" / "opencli" / "dist" / "src" / "daemon.js").touch()

        self._write_executable(
            fake_bin / "node",
            r'''#!/bin/bash
set -e
if [ "$1" = "-e" ]; then
  case "$2" in
    *direct*restricted*) printf 'direct' ;;
    *'filter((target)=>target.type') printf '0' ;;
  esac
  exit 0
fi
case "$1" in
  */resolve-browser-runtime-bundle.mjs)
    [ "$3" = "--report" ] && printf '{}'
    ;;
  */resolve-browser-executable.mjs)
    printf '%s/chromium' "$FAKE_BIN"
    ;;
  */daemon.js)
    printf '%s\n' "$$" > "$FAKE_STATE/daemon.pid"
    trap 'printf term > "$FAKE_STATE/daemon.term"; exit 0' TERM INT
    while :; do sleep 0.05; done
    ;;
esac
''',
        )
        self._write_executable(
            fake_bin / "npm",
            'printf \'%s\\n\' "$FAKE_NODE_ROOT"\n',
        )
        self._write_executable(
            fake_bin / "curl",
            r'''#!/bin/bash
case "$*" in *'/json/list'*) printf '[]\n' ;; esac
exit 0
''',
        )
        self._write_executable(fake_bin / "envsubst", "cat\n")
        self._write_executable(fake_bin / "bbx", "exit 0\n")
        for name in ("xvfb", "nginx", "x11vnc", "websockify", "bbx-daemon"):
            self._write_executable(
                fake_bin / name,
                f'''#!/bin/bash
printf '%s\\n' "$$" > "$FAKE_STATE/{name}.pid"
trap 'printf term > "$FAKE_STATE/{name}.term"; exit 0' TERM INT
while :; do sleep 0.05; done
''',
            )
        self._write_executable(
            fake_bin / "chromium",
            r'''#!/bin/bash
printf '%s\n' "$$" >> "$FAKE_STATE/chromium.starts"
printf '%s\n' "$$" > "$FAKE_STATE/chromium.pid"
trap 'printf term > "$FAKE_STATE/chromium.term"; exit 0' TERM INT
while :; do sleep 0.05; done
''',
        )
        self._write_executable(
            fake_bin / "uvicorn",
            r'''#!/bin/bash
printf '%s\n' "$$" > "$FAKE_STATE/uvicorn.pid"
if [ -n "${FAKE_UVICORN_EXIT:-}" ]; then
  sleep 0.2
  exit "$FAKE_UVICORN_EXIT"
fi
trap 'printf term > "$FAKE_STATE/uvicorn.term"; exit 0' TERM INT
while :; do sleep 0.05; done
''',
        )

        if not embedded:
            (root / "host-bin").mkdir()
            for item in fake_bin.iterdir():
                if item.name != "chromium":
                    shutil.copy2(item, root / "host-bin" / item.name)
            fake_bin = root / "host-bin"

        policy = root / "policy.json"
        policy.write_text(
            '{"PasswordManagerEnabled":false,"AutofillAddressEnabled":false,"AutofillCreditCardEnabled":false}\n',
            encoding="utf-8",
        )
        template = root / "cdp.conf.template"
        template.write_text("server_name ${CHROME_HOSTNAME};\n", encoding="utf-8")
        extension_id = root / "extension-id"
        extension_id.write_text("\n", encoding="utf-8")
        runtime_report = root / "runtime-report.json"
        runtime_home = root / "home"
        runtime_cache = runtime_home / "cache"
        runtime_state = runtime_home / "state"
        profile = root / "profile"
        for path in (runtime_home, runtime_cache, runtime_state, profile):
            path.mkdir(parents=True)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_BIN": str(fake_bin),
                "FAKE_STATE": str(state),
                "FAKE_NODE_ROOT": str(fake_node_root),
                "AGENT_HAS_CHROME": "true" if embedded else "false",
                "PROFILE_DIR": str(profile),
                "RUNTIME_HOME": str(runtime_home),
                "RUNTIME_CACHE_DIR": str(runtime_cache),
                "RUNTIME_STATE_DIR": str(runtime_state),
                "CHROMIUM_POLICY_FILE": str(policy),
                "BROWSER_RUNTIME_REPORT_FILE": str(runtime_report),
                "CHROME_CDP_TEMPLATE_FILE": str(template),
                "CHROME_CDP_CONFIG_FILE": str(root / "cdp.conf"),
                "BROWSER_BRIDGE_EXTENSION_ID_FILE": str(extension_id),
                "BROWSER_RUNTIME_BUNDLE_RESOLVER": "/unused/resolve-browser-runtime-bundle.mjs",
                "AGENT_PORT": "19823",
            }
        )
        return env, state

    def _wait_for(self, path: Path, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {path}")

    def _assert_dead(self, pid: int) -> None:
        with self.subTest(pid=pid):
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            self.fail(f"process {pid} survived coordinated shutdown")

    def test_embedded_stack_forwards_term_and_does_not_restart_chromium(self) -> None:
        with TemporaryDirectory(prefix="agent-entrypoint-lifecycle-") as temp:
            env, state = self._fixture(Path(temp), embedded=True)
            process = subprocess.Popen(
                ["bash", str(ENTRYPOINT)],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                self._wait_for(state / "uvicorn.pid")
                self._wait_for(state / "chromium.starts")
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
            self.assertEqual(process.returncode, 0, output)
            self.assertTrue((state / "uvicorn.term").exists(), output)
            self.assertTrue((state / "chromium.term").exists(), output)
            for name in ("xvfb", "nginx", "x11vnc", "websockify", "bbx-daemon", "daemon"):
                self.assertTrue((state / f"{name}.term").exists(), output)
            self.assertEqual(len((state / "chromium.starts").read_text().splitlines()), 1, output)
            self._assert_dead(int((state / "chromium.pid").read_text()))

    def test_host_mode_preserves_uvicorn_exit_status_and_cleans_children(self) -> None:
        with TemporaryDirectory(prefix="agent-entrypoint-host-") as temp:
            env, state = self._fixture(Path(temp), embedded=False)
            env["FAKE_UVICORN_EXIT"] = "7"
            process = subprocess.run(
                ["bash", str(ENTRYPOINT)],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, 7, process.stdout + process.stderr)
            self.assertTrue((state / "uvicorn.pid").exists())
            self.assertFalse((state / "chromium.starts").exists())


if __name__ == "__main__":
    unittest.main()
