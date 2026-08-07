"""Per-job state persisted in <job-dir>/state.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Full status enum. AWAITING_APPROVAL is defined now for the future semi-auto
# mode but is auto-passed in full-auto mode (the only mode implemented).
PENDING = "pending"
RUNNING = "running"
AWAITING_APPROVAL = "awaiting_approval"
CONVERGED = "converged"
STUCK = "stuck"
ERROR = "error"

ALL_STATUSES = {PENDING, RUNNING, AWAITING_APPROVAL, CONVERGED, STUCK, ERROR}
TERMINAL_STATUSES = {CONVERGED, STUCK, ERROR}

# run-once exit codes
EXIT_CONVERGED = 0
EXIT_CONTINUE = 10
EXIT_STUCK = 20
EXIT_ERROR = 30

STATUS_EXIT_CODES = {CONVERGED: EXIT_CONVERGED, STUCK: EXIT_STUCK, ERROR: EXIT_ERROR}


@dataclass
class State:
    status: str = PENDING
    iteration: int = 0
    consecutive_no_progress: int = 0
    last_gate_summary: dict | None = None
    error: str | None = None

    @classmethod
    def load(cls, job_dir: Path) -> "State":
        path = job_dir / "state.json"
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        status = raw.get("status", PENDING)
        if status not in ALL_STATUSES:
            raise ValueError(f"state.json: unknown status {status!r}")
        return cls(
            status=status,
            iteration=int(raw.get("iteration", 0)),
            consecutive_no_progress=int(raw.get("consecutive_no_progress", 0)),
            last_gate_summary=raw.get("last_gate_summary"),
            error=raw.get("error"),
        )

    def save(self, job_dir: Path) -> None:
        path = job_dir / "state.json"
        data = {
            "status": self.status,
            "iteration": self.iteration,
            "consecutive_no_progress": self.consecutive_no_progress,
            "last_gate_summary": self.last_gate_summary,
            "error": self.error,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
