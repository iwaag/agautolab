"""Job definition loaded from <job-dir>/job.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_MAX_ITERATIONS = 30
DEFAULT_ITERATION_TIMEOUT_SECONDS = 900
DEFAULT_GATE_TIMEOUT_SECONDS = 300


class JobError(Exception):
    """Invalid or unreadable job definition."""


@dataclass
class Job:
    goal: str
    project: str | None = None
    profile: str | None = None
    adapter: str | None = None
    # Empty gates = the job starts in the plan phase: the coding agent's first
    # deliverable is PLAN.md + proposed_gates.yaml, confirmed via `autolab
    # approve` into state.json. Non-empty gates skip planning entirely.
    gates: list[str] = field(default_factory=list)
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    iteration_timeout_seconds: int = DEFAULT_ITERATION_TIMEOUT_SECONDS
    gate_timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    adapter_config: dict = field(default_factory=dict)
    # Push target/ to its `origin` remote after each iteration commit and on
    # reaching a terminal status. Non-fatal on failure (recorded in evidence).
    push: bool = False

    @classmethod
    def load(cls, job_dir: Path) -> "Job":
        path = job_dir / "job.yaml"
        if not path.is_file():
            raise JobError(f"job.yaml not found in {job_dir}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise JobError(f"job.yaml is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise JobError("job.yaml must be a mapping")

        goal = raw.get("goal")
        project = raw.get("project")
        profile = raw.get("profile")
        adapter = raw.get("adapter")
        gates = raw.get("gates") or []
        if not isinstance(goal, str) or not goal.strip():
            raise JobError("job.yaml: 'goal' must be a non-empty string")
        if project is not None and (not isinstance(project, str) or not project.strip()):
            raise JobError("job.yaml: 'project' must be a non-empty string when present")
        if profile is not None and (not isinstance(profile, str) or not profile.strip()):
            raise JobError("job.yaml: 'profile' must be a non-empty string when present")
        if adapter is not None and (not isinstance(adapter, str) or not adapter.strip()):
            raise JobError("job.yaml: 'adapter' must be a non-empty string when present")
        if not isinstance(gates, list) or not all(
            isinstance(g, str) and g.strip() for g in gates
        ):
            raise JobError("job.yaml: 'gates' must be a list of commands when present")

        def _pos_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise JobError(f"job.yaml: '{key}' must be a positive integer")
            return value

        adapter_config = raw.get("adapter_config", {})
        if not isinstance(adapter_config, dict):
            raise JobError("job.yaml: 'adapter_config' must be a mapping")

        push = raw.get("push", False)
        if not isinstance(push, bool):
            raise JobError("job.yaml: 'push' must be a boolean")

        return cls(
            goal=goal,
            project=project.strip() if project else None,
            profile=profile.strip() if profile else None,
            adapter=adapter.strip() if adapter else None,
            gates=[g.strip() for g in gates],
            max_iterations=_pos_int("max_iterations", DEFAULT_MAX_ITERATIONS),
            iteration_timeout_seconds=_pos_int(
                "iteration_timeout_seconds", DEFAULT_ITERATION_TIMEOUT_SECONDS
            ),
            gate_timeout_seconds=_pos_int(
                "gate_timeout_seconds", DEFAULT_GATE_TIMEOUT_SECONDS
            ),
            adapter_config=adapter_config,
            push=push,
        )
