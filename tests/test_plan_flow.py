"""Plan-phase flow tests: plan → approve → implement, and reject → replan.

All through the fake adapter (zero tokens). Jobs are created without gates in
job.yaml, which is the signal to start in the plan phase.
"""

import json
from pathlib import Path

from agautolab.cli import main
from agautolab.run_once import run_once
from agautolab.state import (
    EXIT_AWAITING_APPROVAL,
    EXIT_CONTINUE,
    EXIT_CONVERGED,
    EXIT_ERROR,
    EXIT_STUCK,
    State,
)

from test_run_once import make_job, read_state


def latest_prompt(job_dir: Path) -> str:
    iters = sorted((job_dir / "evidence").iterdir())
    return (iters[-1] / "prompt.txt").read_text(encoding="utf-8")


def test_plan_approve_implement_converged(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir)  # no gates -> plan phase

    # Plan iteration: fake adapter writes PLAN.md + proposed_gates.yaml.
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL
    state = read_state(job_dir)
    assert state["status"] == "awaiting_approval"
    assert state["phase"] == "plan"
    assert (job_dir / "target" / "PLAN.md").is_file()
    assert (job_dir / "target" / "proposed_gates.yaml").is_file()
    prompt = latest_prompt(job_dir)
    assert "plan, do not implement yet" in prompt
    assert "Toy goal for tests." in prompt

    # Another run-once while awaiting: refuses to run anything.
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL
    assert read_state(job_dir)["iteration"] == 1

    # Approve: proposed gates become official, phase flips to implement.
    assert main(["approve", str(job_dir)]) == 0
    state = read_state(job_dir)
    assert state["phase"] == "implement"
    assert state["approved_gates"] == ["test -s progress.log"]

    # Implement iteration: append happens, approved gate passes.
    assert run_once(job_dir) == EXIT_CONVERGED
    assert read_state(job_dir)["status"] == "converged"
    prompt = latest_prompt(job_dir)
    assert "Approved plan" in prompt
    assert "test -s progress.log" in prompt


def test_reject_feeds_back_and_replans(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir)
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL

    feedback = "Gate is too weak: name the exact verification endpoint."
    assert main(["reject", str(job_dir), "--feedback", feedback]) == 0
    state = read_state(job_dir)
    assert state["status"] == "running"
    assert state["phase"] == "plan"
    assert feedback in (job_dir / "NOTES.md").read_text(encoding="utf-8")

    # Next plan iteration sees the feedback and revises the plan.
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL
    prompt = latest_prompt(job_dir)
    assert "plan REJECTED" in prompt
    assert feedback in prompt
    assert "## Revision 1" in (job_dir / "target" / "PLAN.md").read_text(
        encoding="utf-8"
    )


def test_reject_feedback_from_file(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir)
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL

    feedback_file = tmp_path / "feedback.md"
    feedback_file.write_text("Trace every goal sentence to a gate.\n")
    assert main(["reject", str(job_dir), "--feedback", str(feedback_file)]) == 0
    notes = (job_dir / "NOTES.md").read_text(encoding="utf-8")
    assert "Trace every goal sentence to a gate." in notes
    assert str(feedback_file) not in notes  # content, not the path


def test_incomplete_plan_deliverables_continue(tmp_path):
    job_dir = tmp_path / "job"
    # Whitespace-only gate -> proposed_gates.yaml parses as invalid.
    make_job(job_dir, adapter_config={"plan_gates": [" "]}, max_iterations=2)
    assert run_once(job_dir) == EXIT_CONTINUE
    state = read_state(job_dir)
    assert state["status"] == "running"
    assert state["phase"] == "plan"
    # Iteration ceiling still bounds the plan phase.
    assert run_once(job_dir) == EXIT_STUCK
    assert "plan phase" in read_state(job_dir)["error"]


def test_approve_reject_require_awaiting_state(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir, gates=["true"])
    assert main(["approve", str(job_dir)]) == 2  # no state yet -> pending
    assert run_once(job_dir) == EXIT_CONVERGED
    assert main(["approve", str(job_dir)]) == 2  # terminal
    assert main(["reject", str(job_dir), "--feedback", "x"]) == 2


def test_approve_refuses_invalid_proposed_gates(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir)
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL
    (job_dir / "target" / "proposed_gates.yaml").write_text("gates: []\n")
    assert main(["approve", str(job_dir)]) == 2
    assert read_state(job_dir)["status"] == "awaiting_approval"


def test_implement_phase_without_gates_errors(tmp_path):
    job_dir = tmp_path / "job"
    make_job(job_dir)
    State(status="running", phase="implement").save(job_dir)
    assert run_once(job_dir) == EXIT_ERROR
    assert "no gates" in read_state(job_dir)["error"]


def test_status_json_exposes_review_surface(tmp_path, capsys):
    job_dir = tmp_path / "job"
    make_job(job_dir)
    assert run_once(job_dir) == EXIT_AWAITING_APPROVAL
    capsys.readouterr()

    assert main(["status", str(job_dir), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "awaiting_approval"
    assert doc["awaiting_approval"] is True
    assert doc["phase"] == "plan"
    assert doc["proposed_gates"] == ["test -s progress.log"]
    assert doc["plan_file"] == "target/PLAN.md"

    assert main(["approve", str(job_dir)]) == 0
    capsys.readouterr()
    assert main(["status", str(job_dir), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["awaiting_approval"] is False
    assert doc["phase"] == "implement"
    assert doc["approved_gates"] == ["test -s progress.log"]


def test_loop_stops_at_awaiting_approval(tmp_path, monkeypatch):
    from agautolab import loop as loop_mod
    from agautolab.loop import loop

    job_dir = tmp_path / "job"
    make_job(job_dir)
    monkeypatch.setattr(loop_mod.time, "sleep", lambda _s: None)
    assert loop(job_dir) == EXIT_AWAITING_APPROVAL
    assert read_state(job_dir)["status"] == "awaiting_approval"

    # approve, then the loop drives the implement phase to convergence.
    assert main(["approve", str(job_dir)]) == 0
    assert loop(job_dir) == EXIT_CONVERGED
