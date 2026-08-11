"""One autolab iteration.

Jobs run in two phases. Plan phase (job.yaml has no gates): the coding agent
receives the goal and the fact that a reviewer will approve or reject what it
produces; the iteration ends in awaiting_approval. Implement phase (job.yaml
gates, or approved gates in state.json): run the gates, record what happened.

1. flock <job-dir>/.lock; exit 0 silently if already held.
2. Read state.json; exit immediately on terminal states or awaiting_approval.
3. Build the phase-appropriate prompt from the goal, the gate results, and the
   handoff the previous iteration left in NOTES.md.
4. Run the adapter with a wall-clock timeout.
5. Implement phase only: run the gates.
6. Write evidence/iter-NNNN/, update state.json, commit target/.
7. Exit codes: 0=converged, 10=continue, 20=stuck, 30=error,
   40=awaiting approval.

NOTES.md is not written here. It is the coding agent's handoff to its next
iteration, written by the agent in the job directory (reachable via the
adapter's --add-dir grant); this module only reads it forward.
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
from agag.agent_config import AgentConfigError

from . import adapters, gates as gates_mod
from .agent_settings import resolve_project_role
from .job import Job, JobError
from .project_settings import ProjectSettingsError, load_project_roles
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
NOTES_FILE = "NOTES.md"

GIT_ENV_ARGS = ["-c", "user.name=autolab", "-c", "user.email=autolab@localhost"]


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


def _workspace_facts(job_dir: Path, iteration: int) -> list[str]:
    """Where things are. The adapter grants access to the job dir alongside
    target/, so these paths are reachable, not just nameable."""
    return [
        "",
        "# Where things are",
        f"- `{job_dir / 'target'}` — the repo you are working in (your cwd).",
        f"- `{job_dir / NOTES_FILE}` — the handoff between iterations. Yours to "
        "write; the next iteration is given whatever is there.",
        f"- `{job_dir / 'evidence'}` — one directory per iteration: the prompt, "
        "the diff, cost, and `gates.json` (every gate's command, exit code and "
        "output tail as it actually ran). `evidence/iter-"
        f"{iteration:04d}/` will hold this one when it ends.",
    ]


def build_plan_prompt(job: Job, job_dir: Path, iteration: int, notes: str | None) -> str:
    parts = [
        "# Goal (the client's request)",
        job.goal.strip(),
        "",
        "# This iteration",
        "This is the plan phase. A reviewer reads what you produce and either "
        "approves it or sends it back with feedback; implementation runs after "
        "an approval. The gates that are approved become the acceptance "
        "condition every later iteration is measured against.",
        "",
        f"`autolab approve` reads proposed gates from `target/{PROPOSED_GATES_FILE}` "
        "when that file holds a YAML list of shell commands (bare list, or under "
        "a `gates:` key), and takes them on the command line otherwise. A plan "
        f"at `target/{PLAN_FILE}` is what the reviewer reads to judge them.",
        "",
        "The goal above comes from `job.yaml` and heads every later iteration's "
        f"prompt unchanged, implement phase included. `target/{PLAN_FILE}` is the "
        "document that can speak to one phase only.",
    ]
    parts += _workspace_facts(job_dir, iteration)
    if notes:
        parts += ["", f"# {NOTES_FILE} from the previous iteration", notes.strip()]
    return "\n".join(parts) + "\n"


PROMPT_GATE_OUTPUT_CHARS = 2000


def load_gate_output(job_dir: Path, iteration: int) -> dict[str, str]:
    """One iteration's gate output, keyed by command. Empty = nothing readable.

    What a gate printed is the difference between "a command named `pytest -q`
    failed" and knowing why; the file is on disk either way, this just puts it
    where the next iteration is already reading.
    """
    path = job_dir / "evidence" / f"iter-{iteration:04d}" / "gates.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        return {}
    return {
        r["command"]: str(r.get("output_tail") or "")
        for r in raw
        if isinstance(r, dict) and isinstance(r.get("command"), str)
    }


def build_implement_prompt(
    job: Job,
    job_dir: Path,
    iteration: int,
    gates: list[str],
    state: State,
    notes: str | None,
    plan: str | None,
    gate_output: dict[str, str] | None = None,
) -> str:
    parts = [
        "# Goal",
        job.goal.strip(),
    ]
    if plan:
        parts += ["", "# Approved plan (yours; the reviewer accepted it)", plan.strip()]
    parts += ["", "# Acceptance gates (run from the repo root after every iteration)"]
    parts += [f"- `{g}`" for g in gates]
    parts += ["", "# Gate results last iteration"]
    if state.last_gate_summary is None:
        parts.append("No gates have been run yet (first iteration).")
    elif state.last_gate_summary.get("failing"):
        parts.append("Failing, with what each one printed:")
        for g in state.last_gate_summary["failing"]:
            parts.append(f"- `{g}`")
            tail = (gate_output or {}).get(g, "").strip()
            if tail:
                parts += ["", "```", tail[-PROMPT_GATE_OUTPUT_CHARS:], "```", ""]
    else:
        parts.append("All gates exited 0.")
    parts += _workspace_facts(job_dir, iteration)
    if notes:
        parts += ["", f"# {NOTES_FILE} from the previous iteration", notes.strip()]
    return "\n".join(parts) + "\n"


def load_proposed_gates(target: Path) -> list[str] | None:
    """Read target/proposed_gates.yaml for a reviewer. None = nothing readable.

    This is a convenience for `autolab approve` and `autolab status`, not a
    verdict on the iteration: nothing branches on the result. Accepts a bare
    YAML list or a mapping with a `gates:` list.
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


def _write_evidence(
    evidence_dir: Path,
    prompt: str,
    adapter_result: adapters.AdapterResult,
    adapter_timed_out: bool,
    gate_results: list[gates_mod.GateResult],
    diff_text: str,
    started_at: str,
    project: str | None,
    resolved_profile: str,
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
                "project": project,
                "profile": resolved_profile,
            },
            indent=2,
            default=str,
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


def _commit_target(target: Path, message: str) -> tuple[str, bool]:
    """Stage everything, return the diff, and commit it if there is one.

    The commit is the recording device: it is what makes diff.patch per
    iteration possible and what keeps an iteration's work recoverable when
    the next one overwrites it.
    """
    _git(target, "add", "-A")
    diff_text = _git(target, "diff", "--cached").stdout
    if diff_text.strip():
        _git(target, "commit", "-q", "-m", message)
        return diff_text, True
    return diff_text, False


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
    project: str | None,
    resolved_profile: str,
) -> int:
    prompt = build_plan_prompt(job, job_dir, iteration, notes)

    adapter_result, adapter_timed_out = _run_adapter_with_timeout(
        adapter, prompt, target, job.iteration_timeout_seconds
    )

    diff_text, diff_nonempty = _commit_target(
        target, f"autolab: plan iteration {iteration:04d}"
    )

    # One plan iteration is one review opportunity: the job stops for a
    # reviewer regardless of what the agent left behind. Nothing here inspects
    # the agent's files to decide whether planning "happened".
    if iteration >= job.max_iterations:
        state.status = STUCK
        state.error = f"max_iterations ({job.max_iterations}) reached in plan phase"
    else:
        state.status = AWAITING_APPROVAL

    _write_evidence(
        evidence_dir, prompt, adapter_result, adapter_timed_out,
        [], diff_text, started_at, project, resolved_profile,
    )
    if job.push and (
        diff_nonempty
        or state.status in TERMINAL_STATUSES
        or state.status == AWAITING_APPROVAL
    ):
        _push_target(target, evidence_dir)
    state.save(job_dir)

    print(f"autolab: plan iteration {iteration} done — status={state.status}")
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
        project_roles = load_project_roles(job.project)
        profile_override = job.profile or project_roles.get("coding")
        agent = resolve_project_role("coding", profile_override=profile_override,
                                     check_available=False)
        if agent.harness != "fake":
            agent = resolve_project_role("coding", profile_override=profile_override)
        if job.adapter is not None and job.adapter != agent.harness:
            raise adapters.AdapterError(
                f"job adapter {job.adapter!r} disagrees with profile harness {agent.harness!r}"
            )
        adapter = adapters.create(agent.harness, job.adapter_config,
                                  job_dir=job_dir, agent=agent)
    except (adapters.AdapterError, AgentConfigError, ProjectSettingsError) as exc:
        print(f"autolab: {exc}", file=sys.stderr)
        state.status = ERROR
        state.error = str(exc)
        state.save(job_dir)
        return EXIT_ERROR

    # Resolve phase: sticky in state; first iteration derives it from job.yaml.
    # This is the operator's style choice, made before any agent runs.
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

        notes_path = job_dir / NOTES_FILE
        notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else None

        if state.phase == PLAN_PHASE:
            return _run_plan_iteration(
                job_dir, job, state, adapter, target, evidence_dir,
                iteration, notes, started_at, job.project, agent.profile,
            )

        plan_path = target / PLAN_FILE
        plan = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else None
        prompt = build_implement_prompt(
            job, job_dir, iteration, effective_gates, state, notes, plan,
            load_gate_output(job_dir, iteration - 1),
        )

        adapter_result, adapter_timed_out = _run_adapter_with_timeout(
            adapter, prompt, target, job.iteration_timeout_seconds
        )

        gate_results = gates_mod.run_gates(
            effective_gates, target, job.gate_timeout_seconds
        )
        summary = gates_mod.summarize(gate_results)

        diff_text, diff_nonempty = _commit_target(
            target, f"autolab: iteration {iteration:04d}"
        )

        state.last_gate_summary = summary

        # `converged` restates the observation (every gate exited 0); `stuck`
        # means the iteration budget ran out. Neither is an opinion about how
        # the iteration went — that is the agent's to write in NOTES.md.
        if summary["passed"]:
            state.status = CONVERGED
        elif iteration >= job.max_iterations:
            state.status = STUCK
            state.error = f"max_iterations ({job.max_iterations}) reached"
        else:
            state.status = RUNNING

        _write_evidence(
            evidence_dir, prompt, adapter_result, adapter_timed_out,
            gate_results, diff_text, started_at, job.project, agent.profile,
        )
        if job.push and (diff_nonempty or state.status in TERMINAL_STATUSES):
            _push_target(target, evidence_dir)
        state.save(job_dir)

        print(
            f"autolab: iteration {iteration} done — status={state.status}, "
            f"gates {summary['total'] - len(summary['failing'])}/{summary['total']} "
            "exited 0"
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
