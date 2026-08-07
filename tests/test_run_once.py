"""End-to-end tests of the run-once contract using the fake adapter."""

import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from agautolab.run_once import run_once
from agautolab.state import (
    EXIT_CONTINUE,
    EXIT_CONVERGED,
    EXIT_ERROR,
    EXIT_STUCK,
    State,
)


def make_job(job_dir: Path, *, gates: list[str], adapter_config: dict | None = None,
             max_iterations: int = 30, no_progress_limit: int = 3) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    gate_lines = "\n".join(f'  - "{g}"' for g in gates)
    config_lines = ""
    if adapter_config:
        config_lines = "adapter_config:\n" + "\n".join(
            f'  {k}: "{v}"' for k, v in adapter_config.items()
        ) + "\n"
    (job_dir / "job.yaml").write_text(
        "goal: |\n  Toy goal for tests.\n"
        "adapter: fake\n"
        f"{config_lines}"
        f"gates:\n{gate_lines}\n"
        f"max_iterations: {max_iterations}\n"
        f"no_progress_limit: {no_progress_limit}\n",
        encoding="utf-8",
    )


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

    # NOTES.md handoff regenerated, and fed into iteration >= 2 prompts.
    assert (job_dir / "NOTES.md").is_file()
    prompt2 = (job_dir / "evidence" / "iter-0002" / "prompt.txt").read_text()
    assert "Handoff notes from the previous iteration" in prompt2
    assert "handoff after iteration 1" in prompt2

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


def test_stuck_on_no_progress(tmp_path):
    job_dir = tmp_path / "job"
    # progress.log is gitignored, so the fake adapter's writes never show in
    # the diff, and the gate keeps failing with the same set -> no progress.
    make_job(job_dir, gates=["false"], no_progress_limit=2)
    target = job_dir / "target"
    target.mkdir(parents=True)
    (target / ".gitignore").write_text("progress.log\n", encoding="utf-8")

    assert run_once(job_dir) == EXIT_CONTINUE  # iter 1 commits .gitignore -> progress
    assert run_once(job_dir) == EXIT_CONTINUE  # no-progress 1/2
    assert run_once(job_dir) == EXIT_STUCK     # no-progress 2/2 -> stuck

    state = read_state(job_dir)
    assert state["status"] == "stuck"
    assert state["consecutive_no_progress"] == 2
    assert "no progress" in state["error"]


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


def test_awaiting_approval_auto_passes(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test -f progress.log"])
    State(status="awaiting_approval", iteration=0).save(job_dir)
    assert run_once(job_dir) == EXIT_CONVERGED
    assert read_state(job_dir)["status"] == "converged"


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


def test_gate_failure_output_lands_in_notes(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["echo boom-details >&2; false"], no_progress_limit=5)
    assert run_once(job_dir) == EXIT_CONTINUE
    notes = (job_dir / "NOTES.md").read_text()
    assert "boom-details" in notes
    prompt2_gates = read_state(job_dir)["last_gate_summary"]["failing"]
    assert prompt2_gates == ["echo boom-details >&2; false"]
