"""One autolab iteration: the per-iteration contract.

Jobs run in two phases. Plan phase (job.yaml has no gates): the coding agent
receives the goal verbatim and must produce PLAN.md + proposed_gates.yaml in
target/; when both exist and parse, the job stops in awaiting_approval for a
reviewer to `autolab approve` or `autolab reject`. Implement phase (job.yaml
gates, or approved gates in state.json): the classic make-the-gates-pass loop.

1. flock <job-dir>/.lock; exit 0 silently if already held.
2. Read state.json; exit immediately on terminal states or awaiting_approval.
3. Build the phase-appropriate prompt (goal + gate failures + previous
   NOTES.md in implement phase; goal + plan deliverables in plan phase).
4. Run the adapter with a wall-clock timeout.
5. Implement phase only: run gates; pass = all exit 0.
6. Write evidence/iter-NNNN/, regenerate NOTES.md, update state.json,
   commit target/.
7. Exit codes: 0=converged, 10=continue, 20=stuck, 30=error,
   40=awaiting approval.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import adapters, gates as gates_mod
from .job import Job, JobError
from .state import (
    AWAITING_APPROVAL,
    CONVERGED,
    ERROR,
    EXIT_AWAITING_APPROVAL,
    EXIT_CONTINUE,
    EXIT_ERROR,
    IMPLEMENT_PHASE,
    PLAN_PHASE,
    RUNNING,
    STATUS_EXIT_CODES,
    STUCK,
    State,
    TERMINAL_STATUSES,
)

PLAN_FILE = "PLAN.md"
PROPOSED_GATES_FILE = "proposed_gates.yaml"

GIT_ENV_ARGS = ["-c", "user.name=autolab", "-c", "user.email=autolab@localhost"]
ADAPTER_OUTPUT_TAIL_CHARS = 2000


class IterationError(Exception):
    """Unrecoverable error inside one iteration."""


def _git(target: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *GIT_ENV_ARGS, *args],
        cwd=target,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise IterationError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def _ensure_target_repo(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        _git(target, "init", "-q")
        _git(target, "add", "-A")
        _git(target, "commit", "-q", "--allow-empty", "-m", "autolab: initial state")


def build_plan_prompt(job: Job, notes: str | None) -> str:
    parts = [
        "# Goal (the client's request, verbatim)",
        job.goal.strip(),
        "",
        "# Your task: plan, do not implement yet",
        "You are the engineer who will both design and build this. This "
        "iteration you produce the plan and the acceptance criteria; "
        "implementation starts only after a reviewer approves them.",
        "",
        "Write exactly these two files at the repo root:",
        "",
        f"1. `{PLAN_FILE}` — your implementation plan: what you will build, "
        "file layout, and the technical decisions you are making. Written "
        "for a reviewer who knows the goal but not the codebase.",
        f"2. `{PROPOSED_GATES_FILE}` — the acceptance gates you propose, as "
        "YAML:",
        "",
        "```yaml",
        "gates:",
        '  - "shell command run from the repo root"',
        "```",
        "",
        "Gate requirements:",
        "- Deterministic shell commands; all must exit 0 when the goal is met.",
        "- Every requirement in the goal maps to at least one gate; state the "
        f"mapping in {PLAN_FILE}.",
        "- Gates must not pass trivially against the current (empty or "
        "unmodified) repo.",
        "- Name the exact endpoint/process each gate verifies.",
        "",
        "You may also create test files the gates will run. Do not write "
        "product implementation code yet.",
    ]
    if notes:
        parts += ["", "# Notes from the previous iteration (including "
                  "reviewer feedback, if any)", notes.strip()]
    return "\n".join(parts) + "\n"


def build_implement_prompt(
    job: Job, gates: list[str], state: State, notes: str | None, plan: str | None
) -> str:
    parts = [
        "# Goal",
        job.goal.strip(),
    ]
    if plan:
        parts += ["", "# Approved plan (yours; the reviewer accepted it)", plan.strip()]
    parts += ["", "# Acceptance gates (all must exit 0, run from the repo root)"]
    parts += [f"- `{g}`" for g in gates]
    parts += ["", "# Current gate status"]
    if state.last_gate_summary is None:
        parts.append("No gates have been run yet (first iteration).")
    elif state.last_gate_summary.get("failing"):
        parts.append("Currently failing gates:")
        parts += [f"- `{g}`" for g in state.last_gate_summary["failing"]]
    else:
        parts.append("All gates passed last iteration.")
    if notes:
        parts += ["", "# Handoff notes from the previous iteration", notes.strip()]
    parts += [
        "",
        "Work in the current directory. Make the failing gates pass "
        "without weakening or deleting the gates themselves.",
    ]
    return "\n".join(parts) + "\n"


def load_proposed_gates(target: Path) -> list[str] | None:
    """Parse target/proposed_gates.yaml. None = absent or invalid.

    Accepts either a bare YAML list or a mapping with a `gates:` list
    (the prompted form), mirroring job.yaml.
    """
    path = target / PROPOSED_GATES_FILE
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if isinstance(raw, dict):
        raw = raw.get("gates")
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(g, str) and g.strip() for g in raw)
    ):
        return [g.strip() for g in raw]
    return None


ADAPTER_TIMEOUT_GRACE_SECONDS = 30


def _run_adapter_with_timeout(
    adapter: adapters.Adapter, prompt: str, target: Path, timeout: int
) -> tuple[adapters.AdapterResult, bool]:
    """Enforce the wall-clock timeout even for adapters that ignore it.

    The adapter gets `timeout` and is expected to enforce it itself (e.g. via
    a subprocess timeout); the outer guard fires slightly later so a
    well-behaved adapter's own timeout handling wins.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(adapter.run, prompt, target, timeout)
        try:
            return future.result(timeout=timeout + ADAPTER_TIMEOUT_GRACE_SECONDS), False
        except concurrent.futures.TimeoutError:
            future.cancel()
            return (
                adapters.AdapterResult(
                    output=f"adapter exceeded wall-clock timeout of {timeout}s",
                    exit_code=-1,
                ),
                True,
            )


def _push_target(target: Path, evidence_dir: Path) -> None:
    """Push target/ to origin. Non-fatal: transient push failures must not
    turn a healthy iteration into an error verdict; the result is evidence."""
    if _git(target, "remote", "get-url", "origin", check=False).returncode != 0:
        result = {"pushed": False, "reason": "no origin remote"}
    else:
        proc = _git(target, "push", "origin", "HEAD", check=False)
        result = {
            "pushed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-2000:],
        }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "push.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    if not result["pushed"]:
        print(f"autolab: push skipped/failed: {result}", file=sys.stderr)


def _made_progress(old_summary: dict | None, new_failing: set[str], diff_nonempty: bool) -> bool:
    """No progress = failing-gate set didn't shrink AND diff effectively empty."""
    if diff_nonempty:
        return True
    if old_summary is None:
        return True  # first gate run: nothing to compare against
    old_failing = set(old_summary.get("failing", []))
    return new_failing < old_failing or len(new_failing) < len(old_failing)


def _write_notes(
    job_dir: Path,
    iteration: int,
    status: str,
    gate_results: list[gates_mod.GateResult],
    adapter_result: adapters.AdapterResult,
    diff_stat: str,
) -> None:
    summary = gates_mod.summarize(gate_results)
    lines = [
        f"# NOTES — handoff after iteration {iteration}",
        "",
        f"- status after this iteration: {status}",
        f"- gates: {summary['total'] - len(summary['failing'])}/{summary['total']} passing",
    ]
    for r in gate_results:
        mark = "PASS" if r.passed else ("TIMEOUT" if r.timed_out else f"FAIL({r.exit_code})")
        lines.append(f"  - [{mark}] `{r.command}`")
    lines += ["", "## Diff this iteration", "```", diff_stat.strip() or "(no changes)", "```"]
    lines += [
        "",
        "## Adapter output (tail)",
        "```",
        adapter_result.output[-ADAPTER_OUTPUT_TAIL_CHARS:].strip() or "(empty)",
        "```",
        "",
        "## Failing gate output (tails)",
    ]
    failing = [r for r in gate_results if not r.passed]
    if not failing:
        lines.append("(none)")
    else:
        for r in failing:
            lines += [f"### `{r.command}`", "```", r.output_tail.strip() or "(empty)", "```"]
    (job_dir / "NOTES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_evidence(
    evidence_dir: Path,
    prompt: str,
    adapter_result: adapters.AdapterResult,
    adapter_timed_out: bool,
    gate_results: list[gates_mod.GateResult],
    diff_text: str,
    started_at: str,
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (evidence_dir / "adapter_output.txt").write_text(adapter_result.output, encoding="utf-8")
    (evidence_dir / "adapter_result.json").write_text(
        json.dumps(
            {
                "exit_code": adapter_result.exit_code,
                "timed_out": adapter_timed_out,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                **adapter_result.meta,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for name, content in adapter_result.artifacts.items():
        (evidence_dir / Path(name).name).write_text(content, encoding="utf-8")
    (evidence_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
    (evidence_dir / "gates.json").write_text(
        json.dumps([asdict(r) for r in gate_results], indent=2) + "\n", encoding="utf-8"
    )


def _write_plan_notes(
    job_dir: Path,
    iteration: int,
    status: str,
    plan_exists: bool,
    proposed: list[str] | None,
    adapter_result: adapters.AdapterResult,
    diff_stat: str,
) -> None:
    lines = [
        f"# NOTES — handoff after plan iteration {iteration}",
        "",
        f"- status after this iteration: {status}",
        f"- {PLAN_FILE}: {'present' if plan_exists else 'MISSING'}",
        f"- {PROPOSED_GATES_FILE}: "
        + (f"{len(proposed)} gate(s) proposed" if proposed else "missing or invalid"),
    ]
    if proposed:
        lines += [f"  - `{g}`" for g in proposed]
    lines += ["", "## Diff this iteration", "```", diff_stat.strip() or "(no changes)", "```"]
    lines += [
        "",
        "## Adapter output (tail)",
        "```",
        adapter_result.output[-ADAPTER_OUTPUT_TAIL_CHARS:].strip() or "(empty)",
        "```",
    ]
    (job_dir / "NOTES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_plan_iteration(
    job_dir: Path,
    job: Job,
    state: State,
    adapter: adapters.Adapter,
    target: Path,
    evidence_dir: Path,
    iteration: int,
    notes: str | None,
    started_at: str,
) -> int:
    prompt = build_plan_prompt(job, notes)

    adapter_result, adapter_timed_out = _run_adapter_with_timeout(
        adapter, prompt, target, job.iteration_timeout_seconds
    )

    plan_exists = (target / PLAN_FILE).is_file()
    proposed = load_proposed_gates(target)

    _git(target, "add", "-A")
    diff_text = _git(target, "diff", "--cached").stdout
    diff_stat = _git(target, "diff", "--cached", "--stat").stdout
    diff_nonempty = bool(diff_text.strip())
    if diff_nonempty:
        _git(target, "commit", "-q", "-m", f"autolab: plan iteration {iteration:04d}")

    # No no-progress tracking here: a good plan can be a small diff. Only the
    # iteration ceiling bounds the plan phase.
    if plan_exists and proposed:
        state.status = AWAITING_APPROVAL
    elif iteration >= job.max_iterations:
        state.status = STUCK
        state.error = f"max_iterations ({job.max_iterations}) reached in plan phase"
    else:
        state.status = RUNNING

    _write_evidence(
        evidence_dir, prompt, adapter_result, adapter_timed_out,
        [], diff_text, started_at,
    )
    if job.push and (
        diff_nonempty
        or state.status in TERMINAL_STATUSES
        or state.status == AWAITING_APPROVAL
    ):
        _push_target(target, evidence_dir)
    _write_plan_notes(
        job_dir, iteration, state.status, plan_exists, proposed,
        adapter_result, diff_stat,
    )
    state.save(job_dir)

    detail = (
        f"{len(proposed)} gate(s) proposed" if proposed
        else f"plan deliverables incomplete ({PLAN_FILE}: {plan_exists}, "
             f"{PROPOSED_GATES_FILE}: invalid/missing)"
    )
    print(f"autolab: plan iteration {iteration} done — status={state.status}, {detail}")
    return STATUS_EXIT_CODES.get(state.status, EXIT_CONTINUE)


def run_once(job_dir: Path) -> int:
    job_dir = job_dir.resolve()
    if not job_dir.is_dir():
        print(f"autolab: job dir not found: {job_dir}", file=sys.stderr)
        return EXIT_ERROR

    lock_path = job_dir / ".lock"
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another run-once holds the job; exit silently per contract

    try:
        return _run_locked(job_dir)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _run_locked(job_dir: Path) -> int:
    try:
        state = State.load(job_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"autolab: unreadable state.json: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if state.status in TERMINAL_STATUSES:
        print(f"autolab: job already terminal: {state.status}", file=sys.stderr)
        return STATUS_EXIT_CODES[state.status]

    if state.status == AWAITING_APPROVAL:
        print(
            "autolab: plan awaits review — run `autolab approve <job-dir>` or "
            "`autolab reject <job-dir> --feedback ...`",
            file=sys.stderr,
        )
        return EXIT_AWAITING_APPROVAL

    try:
        job = Job.load(job_dir)
    except JobError as exc:
        print(f"autolab: {exc}", file=sys.stderr)
        state.status = ERROR
        state.error = str(exc)
        state.save(job_dir)
        return EXIT_ERROR

    try:
        adapter = adapters.create(job.adapter, job.adapter_config)
    except adapters.AdapterError as exc:
        print(f"autolab: {exc}", file=sys.stderr)
        state.status = ERROR
        state.error = str(exc)
        state.save(job_dir)
        return EXIT_ERROR

    # Resolve phase: sticky in state; first iteration derives it from job.yaml.
    if state.phase is None:
        state.phase = IMPLEMENT_PHASE if job.gates else PLAN_PHASE
    effective_gates = state.approved_gates or job.gates
    if state.phase == IMPLEMENT_PHASE and not effective_gates:
        msg = "implement phase with no gates (approve a plan or add gates to job.yaml)"
        print(f"autolab: {msg}", file=sys.stderr)
        state.status = ERROR
        state.error = msg
        state.save(job_dir)
        return EXIT_ERROR

    iteration = state.iteration + 1
    state.status = RUNNING
    state.iteration = iteration
    state.error = None
    state.save(job_dir)

    target = job_dir / "target"
    evidence_dir = job_dir / "evidence" / f"iter-{iteration:04d}"
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        _ensure_target_repo(target)

        notes_path = job_dir / "NOTES.md"
        notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else None

        if state.phase == PLAN_PHASE:
            return _run_plan_iteration(
                job_dir, job, state, adapter, target, evidence_dir,
                iteration, notes, started_at,
            )

        plan_path = target / PLAN_FILE
        plan = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else None
        prompt = build_implement_prompt(job, effective_gates, state, notes, plan)

        adapter_result, adapter_timed_out = _run_adapter_with_timeout(
            adapter, prompt, target, job.iteration_timeout_seconds
        )

        gate_results = gates_mod.run_gates(
            effective_gates, target, job.gate_timeout_seconds
        )
        summary = gates_mod.summarize(gate_results)

        _git(target, "add", "-A")
        diff_text = _git(target, "diff", "--cached").stdout
        diff_stat = _git(target, "diff", "--cached", "--stat").stdout
        diff_nonempty = bool(diff_text.strip())
        if diff_nonempty:
            _git(target, "commit", "-q", "-m", f"autolab: iteration {iteration:04d}")

        progressed = _made_progress(state.last_gate_summary, set(summary["failing"]), diff_nonempty)
        state.consecutive_no_progress = 0 if progressed else state.consecutive_no_progress + 1
        state.last_gate_summary = summary

        if summary["passed"]:
            state.status = CONVERGED
        elif state.consecutive_no_progress >= job.no_progress_limit:
            state.status = STUCK
            state.error = (
                f"no progress for {state.consecutive_no_progress} consecutive iterations"
            )
        elif iteration >= job.max_iterations:
            state.status = STUCK
            state.error = f"max_iterations ({job.max_iterations}) reached"
        else:
            state.status = RUNNING

        _write_evidence(
            evidence_dir, prompt, adapter_result, adapter_timed_out,
            gate_results, diff_text, started_at,
        )
        if job.push and (diff_nonempty or state.status in TERMINAL_STATUSES):
            _push_target(target, evidence_dir)
        _write_notes(job_dir, iteration, state.status, gate_results, adapter_result, diff_stat)
        state.save(job_dir)

        print(
            f"autolab: iteration {iteration} done — status={state.status}, "
            f"gates {summary['total'] - len(summary['failing'])}/{summary['total']} passing"
        )
        return STATUS_EXIT_CODES.get(state.status, EXIT_CONTINUE)

    except IterationError as exc:
        print(f"autolab: {exc}", file=sys.stderr)
        state.status = ERROR
        state.error = str(exc)
        state.save(job_dir)
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "error.txt").write_text(str(exc) + "\n", encoding="utf-8")
        except OSError:
            pass
        return EXIT_ERROR
