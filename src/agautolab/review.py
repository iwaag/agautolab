"""`autolab approve` / `autolab reject`: the reviewer's verdict on a plan.

The reviewer (the autolab agent) reads target/PLAN.md and
target/proposed_gates.yaml while the job sits in awaiting_approval, then:

- approve: the proposed gates become the job's official gates (stored in
  state.json `approved_gates`; job.yaml stays untouched as the input record)
  and the job moves to the implement phase.
- reject: feedback is appended to NOTES.md — the same channel every prompt
  already merges — and the job returns to the plan phase for another
  iteration.

Both commands take the job lock non-blocking: awaiting_approval means no
iteration is running, so a held lock is a real conflict worth surfacing.
"""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

from .run_once import PLAN_FILE, PROPOSED_GATES_FILE, load_proposed_gates
from .state import AWAITING_APPROVAL, IMPLEMENT_PHASE, PLAN_PHASE, RUNNING, State

EXIT_OK = 0
EXIT_USAGE = 2


def _locked_state(job_dir: Path):
    """Yield (state, release) or print the problem and return None."""
    if not job_dir.is_dir():
        print(f"autolab: job dir not found: {job_dir}", file=sys.stderr)
        return None
    lock_file = (job_dir / ".lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print("autolab: job lock is held (iteration in flight?)", file=sys.stderr)
        return None
    try:
        state = State.load(job_dir)
    except Exception as exc:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        print(f"autolab: unreadable state.json: {exc}", file=sys.stderr)
        return None
    if state.status != AWAITING_APPROVAL:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        print(
            f"autolab: job is {state.status!r}, not awaiting approval",
            file=sys.stderr,
        )
        return None
    return state, lock_file


def approve(job_dir: Path) -> int:
    job_dir = job_dir.resolve()
    got = _locked_state(job_dir)
    if got is None:
        return EXIT_USAGE
    state, lock_file = got
    try:
        gates = load_proposed_gates(job_dir / "target")
        if gates is None:
            print(
                f"autolab: target/{PROPOSED_GATES_FILE} is missing or invalid; "
                "nothing to approve",
                file=sys.stderr,
            )
            return EXIT_USAGE
        state.approved_gates = gates
        state.phase = IMPLEMENT_PHASE
        state.status = RUNNING
        state.consecutive_no_progress = 0
        state.last_gate_summary = None
        state.error = None
        state.save(job_dir)
        print(f"autolab: plan approved — {len(gates)} gate(s) now official:")
        for g in gates:
            print(f"  - {g}")
        return EXIT_OK
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _resolve_feedback(feedback: str) -> str:
    """`--feedback` accepts inline text or a path to a text file."""
    path = Path(feedback)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return feedback


def reject(job_dir: Path, feedback: str) -> int:
    job_dir = job_dir.resolve()
    if not feedback.strip():
        print("autolab: reject requires non-empty --feedback", file=sys.stderr)
        return EXIT_USAGE
    got = _locked_state(job_dir)
    if got is None:
        return EXIT_USAGE
    state, lock_file = got
    try:
        text = _resolve_feedback(feedback).strip()
        notes_path = job_dir / "NOTES.md"
        existing = (
            notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
        )
        block = (
            "\n## Reviewer feedback — plan REJECTED\n\n"
            f"Revise {PLAN_FILE} and {PROPOSED_GATES_FILE} to address this:\n\n"
            f"{text}\n"
        )
        notes_path.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
        state.phase = PLAN_PHASE
        state.status = RUNNING
        state.error = None
        state.save(job_dir)
        print("autolab: plan rejected — feedback appended to NOTES.md; "
              "next iteration replans")
        return EXIT_OK
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
