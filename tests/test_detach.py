"""`--detach`: the run outlives the process that started it."""

import json
import time
from pathlib import Path

from agautolab.cli import main
from agautolab.detach import DETACH_LOG_NAME
from agautolab.state import EXIT_ERROR

from test_run_once import make_job, read_state


def wait_for(job_dir: Path, status: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = read_state(job_dir)
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        if state.get("status") == status:
            return state
        time.sleep(0.1)
    raise AssertionError(f"job never reached {status}: {read_state(job_dir)}")


def test_detached_run_once_returns_at_once_and_finishes_on_its_own(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test -f progress.log"])

    assert main(["run-once", str(job_dir), "--detach"]) == 0
    out = capsys.readouterr().out
    assert "detached pid" in out

    state = wait_for(job_dir, "converged")
    assert state["iteration"] == 1
    log = (job_dir / DETACH_LOG_NAME).read_text(encoding="utf-8")
    assert "run-once" in log and "iteration 1 done" in log


def test_detached_loop_runs_to_a_verdict(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test $(wc -l < progress.log) -ge 2"])

    assert main(["loop", str(job_dir), "--sleep", "0.05", "--detach"]) == 0
    assert wait_for(job_dir, "converged")["iteration"] == 2


def test_detach_on_a_missing_job_dir_is_an_error(tmp_path):
    assert main(["run-once", str(tmp_path / "nope"), "--detach"]) == EXIT_ERROR
