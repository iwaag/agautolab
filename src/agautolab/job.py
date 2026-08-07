"""Job definition loaded from <job-dir>/job.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_MAX_ITERATIONS = 30
DEFAULT_NO_PROGRESS_LIMIT = 3
DEFAULT_ITERATION_TIMEOUT_SECONDS = 900
DEFAULT_GATE_TIMEOUT_SECONDS = 300


class JobError(Exception):
    """Invalid or unreadable job definition."""


@dataclass
class Job:
    goal: str
    adapter: str
    gates: list[str]
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT
    iteration_timeout_seconds: int = DEFAULT_ITERATION_TIMEOUT_SECONDS
    gate_timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    adapter_config: dict = field(default_factory=dict)

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
        adapter = raw.get("adapter")
        gates = raw.get("gates")
        if not isinstance(goal, str) or not goal.strip():
            raise JobError("job.yaml: 'goal' must be a non-empty string")
        if not isinstance(adapter, str) or not adapter.strip():
            raise JobError("job.yaml: 'adapter' must be a non-empty string")
        if not isinstance(gates, list) or not gates or not all(
            isinstance(g, str) and g.strip() for g in gates
        ):
            raise JobError("job.yaml: 'gates' must be a non-empty list of commands")

        def _pos_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise JobError(f"job.yaml: '{key}' must be a positive integer")
            return value

        adapter_config = raw.get("adapter_config", {})
        if not isinstance(adapter_config, dict):
            raise JobError("job.yaml: 'adapter_config' must be a mapping")

        return cls(
            goal=goal,
            adapter=adapter.strip(),
            gates=[g.strip() for g in gates],
            max_iterations=_pos_int("max_iterations", DEFAULT_MAX_ITERATIONS),
            no_progress_limit=_pos_int("no_progress_limit", DEFAULT_NO_PROGRESS_LIMIT),
            iteration_timeout_seconds=_pos_int(
                "iteration_timeout_seconds", DEFAULT_ITERATION_TIMEOUT_SECONDS
            ),
            gate_timeout_seconds=_pos_int(
                "gate_timeout_seconds", DEFAULT_GATE_TIMEOUT_SECONDS
            ),
            adapter_config=adapter_config,
        )
