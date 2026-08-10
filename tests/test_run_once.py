"""End-to-end tests of the run-once contract using the fake adapter.

Jobs here carry gates in job.yaml, so they run the classic implement-phase
loop directly. The plan→approve→implement flow is covered in
test_plan_flow.py.
"""

import fcntl
import json
import subprocess
from pathlib import Path

import yaml

from agautolab.run_once import run_once
from agautolab.state import (
    EXIT_AWAITING_APPROVAL,
    EXIT_CONTINUE,
    EXIT_CONVERGED,
    EXIT_ERROR,
    EXIT_STUCK,
    State,
)


def make_job(job_dir: Path, *, gates: list[str] | None = None,
             adapter_config: dict | None = None,
             max_iterations: int = 30) -> None:
    """gates=None writes a job.yaml without gates: the job starts in the
    plan phase."""
    job_dir.mkdir(parents=True, exist_ok=True)
    doc: dict = {
        "goal": "Toy goal for tests.",
        "adapter": "fake",
        "max_iterations": max_iterations,
    }
    if gates is not None:
        doc["gates"] = gates
    if adapter_config:
        doc["adapter_config"] = adapter_config
    (job_dir / "job.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def read_state(job_dir: Path) -> dict:
    return json.loads((job_dir / "state.json").read_text(encoding="utf-8"))


def test_converges_after_three_iterations(tmp_path):
    job_dir = tmp_path / "job"
    # Fake adapter appends one line per iteration; gate passes at >= 3 lines.
    make_job(job_dir, gates=["test $(wc -l < progress.log) -ge 3"])

    assert run_once(job_dir) == EXIT_CONTINUE
    assert read_state(job_dir)["status"] == "running"
    assert run_once(job_dir) == EXIT_CONTINUE
    assert run_once(job_dir) == EXIT_CONVERGED

    state = read_state(job_dir)
    assert state["status"] == "converged"
    assert state["iteration"] == 3
    assert state["last_gate_summary"]["passed"] is True

    # Evidence written for every iteration with the full set of artifacts.
    for i in (1, 2, 3):
        ev = job_dir / "evidence" / f"iter-{i:04d}"
        for name in ("prompt.txt", "adapter_output.txt", "adapter_result.json",
                     "diff.patch", "gates.json"):
            assert (ev / name).is_file(), f"missing {name} in iter-{i:04d}"
        assert (ev / "diff.patch").read_text()  # fake adapter always changes target

    # Nothing here writes NOTES.md: it is the coding agent's document, and
    # the fake adapter does not write one.
    assert not (job_dir / "NOTES.md").exists()

    # target/ is a git repo with one commit per iteration + the initial commit.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=job_dir / "target",
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(log) == 4
    assert "autolab: iteration 0003" in log[0]


def test_terminal_state_exits_immediately(tmp_path):
    for status, expected in (("converged", EXIT_CONVERGED), ("stuck", EXIT_STUCK),
                             ("error", EXIT_ERROR)):
        job_dir = tmp_path / f"job-{status}"
        make_job(job_dir, gates=["true"])
        state = State(status=status, iteration=5)
        state.save(job_dir)
        assert run_once(job_dir) == expected
        assert read_state(job_dir)["iteration"] == 5  # nothing ran


def test_lock_held_exits_zero_silently(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    lock = (job_dir / ".lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run_once(job_dir) == 0
    finally:
        lock.close()
    assert capsys.readouterr().out == ""
    assert not (job_dir / "state.json").exists()  # no iteration ran


def test_no_progress_is_not_a_verdict(tmp_path):
    """An iteration that changes nothing and fails the same gate keeps
    running: whether that is progress is the agent's judgment, not the
    machine's. Only the iteration budget stops it."""
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["false"], max_iterations=4)
    target = job_dir / "target"
    target.mkdir(parents=True)
    (target / ".gitignore").write_text("progress.log\n", encoding="utf-8")

    assert run_once(job_dir) == EXIT_CONTINUE
    assert run_once(job_dir) == EXIT_CONTINUE
    assert run_once(job_dir) == EXIT_CONTINUE
    assert run_once(job_dir) == EXIT_STUCK
    state = read_state(job_dir)
    assert "max_iterations" in state["error"]


def test_stuck_on_max_iterations(tmp_path):
    job_dir = tmp_path / "job"
    # Fake adapter always makes a diff, so no-progress never triggers; the
    # always-failing gate must hit the max_iterations ceiling instead.
    make_job(job_dir, gates=["false"], max_iterations=2)

    assert run_once(job_dir) == EXIT_CONTINUE
    assert run_once(job_dir) == EXIT_STUCK
    state = read_state(job_dir)
    assert state["status"] == "stuck"
    assert "max_iterations" in state["error"]


def test_awaiting_approval_stops_without_running(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test -f progress.log"])
    State(status="awaiting_approval", iteration=2, phase="plan").save(job_dir)
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL
    state = read_state(job_dir)
    assert state["status"] == "awaiting_approval"
    assert state["iteration"] == 2  # nothing ran


def test_invalid_job_yaml_errors(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "job.yaml").write_text("goal: ''\nadapter: fake\ngates: []\n")
    assert run_once(job_dir) == EXIT_ERROR
    assert read_state(job_dir)["status"] == "error"


def test_unknown_adapter_errors(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    (job_dir / "job.yaml").write_text(
        (job_dir / "job.yaml").read_text().replace("adapter: fake", "adapter: nope")
    )
    assert run_once(job_dir) == EXIT_ERROR
    state = read_state(job_dir)
    assert state["status"] == "error"
    assert "unknown adapter" in state["error"]


def test_missing_job_dir_errors(tmp_path):
    assert run_once(tmp_path / "does-not-exist") == EXIT_ERROR


def test_gate_failure_output_lands_in_evidence(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["echo boom-details >&2; false"])
    assert run_once(job_dir) == EXIT_CONTINUE
    gates = json.loads((job_dir / "evidence" / "iter-0001" / "gates.json").read_text())
    assert "boom-details" in gates[0]["output_tail"]
    assert read_state(job_dir)["last_gate_summary"]["failing"] == [
        "echo boom-details >&2; false"
    ]


def test_agent_written_notes_are_carried_forward(tmp_path):
    """The handoff is whatever the agent left, passed on as written."""
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test $(wc -l < progress.log) -ge 2"])
    assert run_once(job_dir) == EXIT_CONTINUE
    (job_dir / "NOTES.md").write_text("appended once; gate wants two lines\n")
    assert run_once(job_dir) == EXIT_CONVERGED
    prompt2 = (job_dir / "evidence" / "iter-0002" / "prompt.txt").read_text()
    assert "appended once; gate wants two lines" in prompt2


def test_failing_gate_output_reaches_the_next_prompt(tmp_path):
    """A failing gate arrives as what it printed, not only as its name."""
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["echo 'SyntaxError: invalid syntax' >&2; false"])
    assert run_once(job_dir) == EXIT_CONTINUE
    assert run_once(job_dir) == EXIT_CONTINUE
    prompt2 = (job_dir / "evidence" / "iter-0002" / "prompt.txt").read_text()
    assert "SyntaxError: invalid syntax" in prompt2
