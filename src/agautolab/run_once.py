"""One autolab iteration: the per-iteration contract.

1. flock <job-dir>/.lock; exit 0 silently if already held.
2. Read state.json; exit immediately on terminal states.
3. Build prompt: job.yaml goal + current gate failures + previous NOTES.md.
4. Run the adapter with a wall-clock timeout.
5. Run gates; pass = all exit 0.
6. Write evidence/iter-NNNN/, regenerate NOTES.md, update state.json,
   commit target/.
7. Exit codes: 0=converged, 10=continue, 20=stuck, 30=error.
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

from . import adapters, gates as gates_mod
from .job import Job, JobError
from .state import (
    AWAITING_APPROVAL,
    CONVERGED,
    ERROR,
    EXIT_CONTINUE,
    EXIT_ERROR,
    RUNNING,
    STATUS_EXIT_CODES,
    STUCK,
    State,
    TERMINAL_STATUSES,
)

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


def build_prompt(job: Job, state: State, notes: str | None) -> str:
    parts = [
        "# Goal",
        job.goal.strip(),
        "",
        "# Acceptance gates (all must exit 0, run from the repo root)",
    ]
    parts += [f"- `{g}`" for g in job.gates]
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
        # Full-auto mode: approval is auto-passed. The state exists now so the
        # future semi-auto hook has a defined place to stop.
        print("autolab: awaiting_approval auto-passed (full-auto mode)", file=sys.stderr)

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
        prompt = build_prompt(job, state, notes)

        adapter_result, adapter_timed_out = _run_adapter_with_timeout(
            adapter, prompt, target, job.iteration_timeout_seconds
        )

        gate_results = gates_mod.run_gates(job.gates, target, job.gate_timeout_seconds)
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
