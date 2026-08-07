"""Tests for `autolab status` and push-on-commit/terminal."""

import json
import subprocess
from pathlib import Path

from agautolab.cli import main
from agautolab.run_once import run_once
from agautolab.state import EXIT_CONTINUE, EXIT_CONVERGED, State


def make_job(job_dir: Path, *, gates: list[str], push: bool = False,
             no_progress_limit: int = 3) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    gate_lines = "\n".join(f'  - "{g}"' for g in gates)
    (job_dir / "job.yaml").write_text(
        "goal: |\n  Toy goal for tests.\n"
        "adapter: fake\n"
        f"gates:\n{gate_lines}\n"
        f"no_progress_limit: {no_progress_limit}\n"
        + ("push: true\n" if push else ""),
        encoding="utf-8",
    )


def test_status_before_first_run(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    assert main(["status", str(job_dir)]) == 0
    out = capsys.readouterr().out
    assert "status: pending" in out
    assert "gates: not run yet" in out


def test_status_text_and_json_after_runs(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test $(wc -l < progress.log) -ge 2"])
    assert run_once(job_dir) == EXIT_CONTINUE
    capsys.readouterr()

    assert main(["status", str(job_dir)]) == 0
    out = capsys.readouterr().out
    assert "status: running" in out
    assert "iteration: 1" in out
    assert "gates: 0/1 passing" in out
    assert "failing: test $(wc -l < progress.log) -ge 2" in out
    assert "last_evidence: evidence/iter-0001" in out

    assert run_once(job_dir) == EXIT_CONVERGED
    capsys.readouterr()
    assert main(["status", "--json", str(job_dir)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "converged"
    assert doc["terminal"] is True
    assert doc["iteration"] == 2
    assert doc["last_gate_summary"]["passed"] is True
    assert doc["last_evidence"] == "evidence/iter-0002"
    assert doc["job"]["adapter"] == "fake"


def test_status_missing_dir_and_bad_state(tmp_path, capsys):
    assert main(["status", str(tmp_path / "nope")]) == 2
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    (job_dir / "state.json").write_text("{not json", encoding="utf-8")
    assert main(["status", str(job_dir)]) == 2


def test_status_does_not_touch_state(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    State(status="stuck", iteration=4, error="max_iterations (4) reached").save(job_dir)
    before = (job_dir / "state.json").read_text()
    assert main(["status", str(job_dir)]) == 0
    assert (job_dir / "state.json").read_text() == before
    out = capsys.readouterr().out
    assert "status: stuck (terminal)" in out
    assert "error: max_iterations (4) reached" in out


def _init_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    return remote


def test_push_on_commit_and_terminal(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test $(wc -l < progress.log) -ge 2"], push=True)
    remote = _init_bare_remote(tmp_path)
    target = job_dir / "target"
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=target, check=True)

    assert run_once(job_dir) == EXIT_CONTINUE
    push1 = json.loads((job_dir / "evidence" / "iter-0001" / "push.json").read_text())
    assert push1["pushed"] is True

    assert run_once(job_dir) == EXIT_CONVERGED
    push2 = json.loads((job_dir / "evidence" / "iter-0002" / "push.json").read_text())
    assert push2["pushed"] is True

    # Remote actually received the iteration commits.
    log = subprocess.run(
        ["git", "log", "--oneline", "--all"], cwd=remote,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "autolab: iteration 0002" in log


def test_push_without_remote_is_nonfatal(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test -f progress.log"], push=True)
    assert run_once(job_dir) == EXIT_CONVERGED  # iteration still succeeds
    push = json.loads((job_dir / "evidence" / "iter-0001" / "push.json").read_text())
    assert push["pushed"] is False
    assert push["reason"] == "no origin remote"


def test_push_disabled_writes_no_push_evidence(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["test -f progress.log"])
    assert run_once(job_dir) == EXIT_CONVERGED
    assert not (job_dir / "evidence" / "iter-0001" / "push.json").exists()
