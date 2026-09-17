import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DockerStage:
    name: str
    base: str
    instructions: tuple[str, ...]


def _dockerfile_stages() -> dict[str, DockerStage]:
    stages: list[DockerStage] = []
    current_name: str | None = None
    current_base: str | None = None
    current_instructions: list[str] = []
    logical_line = ""

    def finish_instruction() -> None:
        nonlocal logical_line
        if logical_line:
            if current_name is not None:
                current_instructions.append(logical_line)
            logical_line = ""

    def finish_stage() -> None:
        nonlocal current_name, current_base, current_instructions
        finish_instruction()
        if current_name is not None and current_base is not None:
            stages.append(
                DockerStage(
                    name=current_name,
                    base=current_base,
                    instructions=tuple(current_instructions),
                )
            )
        current_name = None
        current_base = None
        current_instructions = []

    for raw_line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continuation = line.endswith("\\")
        line = line[:-1].rstrip() if continuation else line
        logical_line = f"{logical_line} {line}".strip()
        if continuation:
            continue

        from_match = re.fullmatch(
            r"FROM\s+(?P<base>\S+)(?:\s+AS\s+(?P<name>\S+))?",
            logical_line,
            flags=re.IGNORECASE,
        )
        if from_match:
            logical_line = ""
            finish_stage()
            current_base = from_match.group("base")
            current_name = from_match.group("name") or f"stage-{len(stages)}"
            continue
        finish_instruction()

    finish_stage()
    return {stage.name: stage for stage in stages}


def _effective_instructions(
    stages: dict[str, DockerStage], stage_name: str, seen: set[str] | None = None
) -> tuple[str, ...]:
    seen = set() if seen is None else seen
    if stage_name in seen:
        raise AssertionError(f"cyclic Docker stage inheritance: {stage_name}")
    seen.add(stage_name)
    stage = stages[stage_name]
    inherited = _effective_instructions(stages, stage.base, seen) if stage.base in stages else ()
    return inherited + stage.instructions


def test_default_docker_target_is_clean_production_runtime() -> None:
    stages = _dockerfile_stages()
    stage_names = list(stages)
    assert stage_names[-1] == "production"
    assert stages["production"].base == "runtime"

    runtime = "\n".join(stages["runtime"].instructions)
    production = "\n".join(_effective_instructions(stages, "production"))

    assert "EXPOSE 8000" in runtime
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in runtime
    assert 'CMD ["uvicorn", "backend.main:app"' in runtime
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in production
    assert 'CMD ["uvicorn", "backend.main:app"' in production
    assert "OPENCLI_BIN=/opt/non-bypass/opencli-proof" not in production
    assert "/opt/non-bypass/opencli-proof" not in production
    assert "/opt/iii/iii" not in production
    assert "tests/acceptance" not in production


def test_non_bypass_acceptance_target_keeps_verified_fixture_and_entrypoint() -> None:
    stages = _dockerfile_stages()
    acceptance = "\n".join(_effective_instructions(stages, "non-bypass-acceptance"))

    assert stages["non-bypass-acceptance"].base == "runtime"
    assert "COPY --from=iii-engine /app/iii /opt/iii/iii" in acceptance
    assert (
        "COPY tests/acceptance/fixtures/opencli-proof /opt/non-bypass/opencli-proof" in acceptance
    )
    assert "opencli-proof.sha256 /opt/non-bypass/opencli-proof.sha256" in acceptance
    assert "III_CLI_PATH=/opt/iii/iii" in acceptance
    assert "OPENCLI_BIN=/opt/non-bypass/opencli-proof" in acceptance
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in acceptance
    assert 'CMD ["uvicorn", "backend.main:app"' in acceptance
