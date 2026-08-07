"""Tests of `autolab loop` using the fake adapter."""

import json
from pathlib import Path

from agautolab import loop as loop_mod
from agautolab.loop import loop
from agautolab.state import EXIT_CONVERGED, EXIT_ERROR, EXIT_STUCK, State

from test_run_once import make_job, read_state


def test_loop_runs_until_converged(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    # Fake adapter appends one line per iteration; gate passes at >= 3 lines.
    make_job(job_dir, gates=["test $(wc -l < progress.log) -ge 3"])

    sleeps: list[float] = []
    monkeypatch.setattr(loop_mod.time, "sleep", sleeps.append)

    assert loop(job_dir, sleep_seconds=0.01) == EXIT_CONVERGED
    state = read_state(job_dir)
    assert state["status"] == "converged"
    assert state["iteration"] == 3
    assert sleeps == [0.01, 0.01]  # between iterations only, not after the last


def test_loop_stops_on_stuck(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["false"], max_iterations=2)
    monkeypatch.setattr(loop_mod.time, "sleep", lambda _s: None)

    assert loop(job_dir) == EXIT_STUCK
    assert read_state(job_dir)["status"] == "stuck"


def test_loop_on_terminal_job_exits_immediately(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    State(status="error", iteration=5).save(job_dir)

    def no_sleep(_s):
        raise AssertionError("loop must not sleep on an already-terminal job")

    monkeypatch.setattr(loop_mod.time, "sleep", no_sleep)
    assert loop(job_dir) == EXIT_ERROR
    assert read_state(job_dir)["iteration"] == 5  # nothing ran


def test_loop_returns_130_on_keyboard_interrupt(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["false"])

    def interrupt(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(loop_mod.time, "sleep", interrupt)
    assert loop(job_dir) == loop_mod.EXIT_INTERRUPTED
    # The interrupted iteration itself completed and persisted its state.
    assert read_state(job_dir)["status"] == "running"


def test_cli_loop_subcommand(tmp_path, monkeypatch):
    from agautolab.cli import main

    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test -f progress.log"])
    monkeypatch.setattr(loop_mod.time, "sleep", lambda _s: None)
    assert main(["loop", str(job_dir), "--sleep", "0"]) == EXIT_CONVERGED
    assert read_state(job_dir)["status"] == "converged"
