import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RUNTIME_SMOKE_WORKFLOW = (
    ROOT / ".github" / "workflows" / "account-runtime-smoke.yml"
)

REQUIRED_CI_JOBS = {
    "extension",
    "frontend",
    "release-contract",
    "backend",
    "frontend-workflow-checks",
    "migrations",
    "cargo",
}

RUNTIME_SMOKE_STEPS = {
    "Checkout",
    "Build the current agent image",
    "Run POSIX process-tree regression",
    "Run POSIX entrypoint lifecycle regression",
    "Run isolated managed account runtimes",
    "Show bounded verification reports",
    "Remove ephemeral runtime containers and image",
}


def load_workflow(path: Path) -> dict:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver treats the YAML 1.2 `on` key as boolean True.
    if True in parsed and "on" not in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def test_ci_keeps_existing_jobs_and_adds_a_fail_closed_gate() -> None:
    workflow = load_workflow(CI_WORKFLOW)
    events = workflow["on"]

    assert workflow["name"] == "ci"
    assert set(events) == {"push", "pull_request", "workflow_dispatch"}
    assert events["push"]["branches"] == ["main", "develop", "codex/**"]
    assert events["pull_request"] is None
    assert events["workflow_dispatch"] is None

    concurrency = workflow["concurrency"]
    assert all(
        token in concurrency["group"]
        for token in ("github.workflow", "github.event_name", "github.ref")
    )
    assert concurrency["cancel-in-progress"] is True

    jobs = workflow["jobs"]
    assert REQUIRED_CI_JOBS <= set(jobs)
    assert set(jobs) == REQUIRED_CI_JOBS | {"ci-gate"}
    assert all(jobs[job]["timeout-minutes"] > 0 for job in jobs)
    gate = jobs["ci-gate"]
    assert gate["name"] == "CI Gate"
    assert gate["if"].strip() == "${{ always() }}"
    assert set(gate["needs"]) == REQUIRED_CI_JOBS
    gate_decision = next(
        step
        for step in gate["steps"]
        if step.get("name") == "Reject incomplete CI"
    )
    assert gate_decision["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}"
    assert "python - <<'PY'" in gate_decision["run"]
    assert "node --test .github/scripts/require-release-ci.test.cjs" in "\n".join(
        step.get("run", "")
        for step in gate["steps"]
        if "run" in step
    )


def gate_decision_script() -> str:
    workflow = load_workflow(CI_WORKFLOW)
    decision = next(
        step
        for step in workflow["jobs"]["ci-gate"]["steps"]
        if step.get("name") == "Reject incomplete CI"
    )
    marker = "python - <<'PY'\n"
    _, found, body = decision["run"].partition(marker)
    assert found
    script, _, _ = body.partition("\nPY")
    assert script
    return script


def run_gate(needs: dict[str, dict[str, str]]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", gate_decision_script()],
        env={**os.environ, "NEEDS_JSON": json.dumps(needs)},
        capture_output=True,
        text=True,
        check=False,
    )


def successful_needs() -> dict[str, dict[str, str]]:
    return {job: {"result": "success"} for job in REQUIRED_CI_JOBS}


def test_ci_gate_script_accepts_all_successful_dependencies() -> None:
    result = run_gate(successful_needs())

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped"])
def test_ci_gate_script_rejects_any_non_success_dependency(status: str) -> None:
    needs = successful_needs()
    needs["backend"]["result"] = status

    result = run_gate(needs)

    assert result.returncode != 0


def test_ci_gate_script_rejects_missing_dependency() -> None:
    needs = successful_needs()
    del needs["cargo"]

    result = run_gate(needs)

    assert result.returncode != 0


def test_runtime_smoke_preserves_steps_and_adds_pr_scoped_cancellation() -> None:
    workflow = load_workflow(RUNTIME_SMOKE_WORKFLOW)
    events = workflow["on"]
    push_paths = events["push"]["paths"]

    assert events["workflow_dispatch"] is None
    assert events["pull_request"]["paths"] == push_paths

    concurrency = workflow["concurrency"]
    assert all(token in concurrency["group"] for token in ("github.event_name", "github.ref"))
    assert concurrency["cancel-in-progress"] is True

    job = workflow["jobs"]["managed-account-runtime"]
    assert job["timeout-minutes"] == 25
    assert {step["name"] for step in job["steps"]} == RUNTIME_SMOKE_STEPS
