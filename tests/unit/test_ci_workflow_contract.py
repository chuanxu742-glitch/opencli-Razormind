from pathlib import Path

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
    gate_condition = next(
        step["if"]
        for step in gate["steps"]
        if step.get("name") == "Reject incomplete CI"
    )
    expected_results = {
        job: (
            f"needs['{job}'].result == 'success'"
            if "-" in job
            else f"needs.{job}.result == 'success'"
        )
        for job in REQUIRED_CI_JOBS
    }
    assert all(expression in gate_condition for expression in expected_results.values())
    assert "node --test .github/scripts/require-release-ci.test.cjs" in "\n".join(
        step.get("run", "")
        for step in gate["steps"]
        if "run" in step
    )


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
