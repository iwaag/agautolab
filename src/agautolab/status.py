"""`autolab status`: compact job status for humans and agents.

Read-only: loads state.json (and job.yaml when present) and prints either a
short human/agent-readable text block or, with --json, one structured object.
Never takes the job lock, so it is safe to run while an iteration is live.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .job import Job, JobError
from .run_once import (
    NOTES_FILE,
    PLAN_FILE,
    PROPOSED_GATES_FILE,
    load_proposed_gates,
)
from .state import AWAITING_APPROVAL, State, TERMINAL_STATUSES


def _last_evidence_dir(job_dir: Path) -> str | None:
    evidence = job_dir / "evidence"
    if not evidence.is_dir():
        return None
    iters = sorted(d.name for d in evidence.iterdir() if d.is_dir() and d.name.startswith("iter-"))
    return f"evidence/{iters[-1]}" if iters else None


def collect_status(job_dir: Path) -> dict:
    """Build the status document. Raises ValueError on unreadable state."""
    state = State.load(job_dir)

    doc: dict = {
        "job_dir": str(job_dir),
        "status": state.status,
        "terminal": state.status in TERMINAL_STATUSES,
        "phase": state.phase,
        "awaiting_approval": state.status == AWAITING_APPROVAL,
        "approved_gates": state.approved_gates,
        "iteration": state.iteration,
        "last_gate_summary": state.last_gate_summary,
        "error": state.error,
        "last_evidence": _last_evidence_dir(job_dir),
        "has_notes": (job_dir / NOTES_FILE).is_file(),
    }
    if doc["awaiting_approval"]:
        doc["proposed_gates"] = load_proposed_gates(job_dir / "target")
        doc["plan_file"] = (
            f"target/{PLAN_FILE}"
            if (job_dir / "target" / PLAN_FILE).is_file()
            else None
        )
    try:
        job = Job.load(job_dir)
        doc["job"] = {
            "adapter": job.adapter,
            "gates": job.gates,
            "max_iterations": job.max_iterations,
            "push": job.push,
        }
    except JobError as exc:
        doc["job"] = None
        doc["job_error"] = str(exc)
    return doc


def _render_text(doc: dict) -> str:
    lines = [
        f"job: {doc['job_dir']}",
        f"status: {doc['status']}" + (" (terminal)" if doc["terminal"] else ""),
        f"phase: {doc['phase'] or '(undecided)'}",
        f"iteration: {doc['iteration']}"
        + (
            f" / max {doc['job']['max_iterations']}"
            if doc.get("job")
            else ""
        ),
    ]
    summary = doc["last_gate_summary"]
    if summary is None:
        lines.append("gates: not run yet")
    else:
        total = summary.get("total", 0)
        failing = summary.get("failing", [])
        lines.append(f"gates: {total - len(failing)}/{total} exited 0")
        for g in failing:
            lines.append(f"  failing: {g}")
    if doc["awaiting_approval"]:
        lines.append("awaiting approval: `autolab approve` or "
                     "`autolab reject --feedback ...`")
        proposed = doc.get("proposed_gates")
        if proposed:
            for g in proposed:
                lines.append(f"  proposed: {g}")
        else:
            lines.append(f"  proposed: (nothing readable in target/{PROPOSED_GATES_FILE})")
    if doc["error"]:
        lines.append(f"error: {doc['error']}")
    if doc["last_evidence"]:
        lines.append(f"last_evidence: {doc['last_evidence']}")
    if doc.get("job_error"):
        lines.append(f"job.yaml problem: {doc['job_error']}")
    return "\n".join(lines)


def status(job_dir: Path, as_json: bool = False) -> int:
    job_dir = job_dir.resolve()
    if not job_dir.is_dir():
        print(f"autolab: job dir not found: {job_dir}", file=sys.stderr)
        return 2
    try:
        doc = collect_status(job_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"autolab: unreadable state.json: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        print(_render_text(doc))
    return 0
