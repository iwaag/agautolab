"""Per-job state persisted in <job-dir>/state.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Full status enum. AWAITING_APPROVAL means a plan-phase iteration produced
# PLAN.md + proposed_gates.yaml and the job is stopped until a reviewer runs
# `autolab approve` or `autolab reject`.
PENDING = "pending"
RUNNING = "running"
AWAITING_APPROVAL = "awaiting_approval"
CONVERGED = "converged"
STUCK = "stuck"
ERROR = "error"

ALL_STATUSES = {PENDING, RUNNING, AWAITING_APPROVAL, CONVERGED, STUCK, ERROR}
TERMINAL_STATUSES = {CONVERGED, STUCK, ERROR}

# Job phases. A job with no gates in job.yaml starts in the plan phase; the
# implement phase begins once proposed gates are approved (or immediately when
# job.yaml carries gates itself).
PLAN_PHASE = "plan"
IMPLEMENT_PHASE = "implement"
ALL_PHASES = {PLAN_PHASE, IMPLEMENT_PHASE}

# run-once exit codes
EXIT_CONVERGED = 0
EXIT_CONTINUE = 10
EXIT_STUCK = 20
EXIT_ERROR = 30
EXIT_AWAITING_APPROVAL = 40

STATUS_EXIT_CODES = {
    CONVERGED: EXIT_CONVERGED,
    STUCK: EXIT_STUCK,
    ERROR: EXIT_ERROR,
    AWAITING_APPROVAL: EXIT_AWAITING_APPROVAL,
}


@dataclass
class State:
    status: str = PENDING
    iteration: int = 0
    last_gate_summary: dict | None = None
    error: str | None = None
    # Which phase the next iteration runs in. None = not decided yet (derived
    # from job.yaml on the first iteration: gates present -> implement).
    phase: str | None = None
    # Gates confirmed by `autolab approve`. Once set they override job.yaml
    # gates; job.yaml stays the human/agent-authored input, state the machine's.
    approved_gates: list[str] | None = None

    @classmethod
    def load(cls, job_dir: Path) -> "State":
        path = job_dir / "state.json"
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        status = raw.get("status", PENDING)
        if status not in ALL_STATUSES:
            raise ValueError(f"state.json: unknown status {status!r}")
        phase = raw.get("phase")
        if phase is not None and phase not in ALL_PHASES:
            raise ValueError(f"state.json: unknown phase {phase!r}")
        return cls(
            status=status,
            iteration=int(raw.get("iteration", 0)),
            last_gate_summary=raw.get("last_gate_summary"),
            error=raw.get("error"),
            phase=phase,
            approved_gates=raw.get("approved_gates"),
        )

    def save(self, job_dir: Path) -> None:
        path = job_dir / "state.json"
        data = {
            "status": self.status,
            "iteration": self.iteration,
            "last_gate_summary": self.last_gate_summary,
            "error": self.error,
            "phase": self.phase,
            "approved_gates": self.approved_gates,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
