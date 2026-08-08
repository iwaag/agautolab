"""Tests for the gateway's read-only job-layer helpers (monitor scope 1).

The routes themselves are thin wrappers; what is worth pinning down is that a
job mid-write degrades to a row with an error instead of raising, and that the
cost rollups add up the fields a human is going to make decisions on.
"""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gateway_jobs", Path(__file__).resolve().parent.parent / "agent" / "gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def make_job(root: Path, name: str, state: str | dict, iters: dict) -> Path:
    job = root / name
    (job / "evidence").mkdir(parents=True)
    (job / "state.json").write_text(
        state if isinstance(state, str) else json.dumps(state)
    )
    (job / "job.yaml").write_text("goal: x\nadapter: fake\nmax_iterations: 7\n")
    for it, cost in iters.items():
        d = job / "evidence" / it
        d.mkdir()
        (d / "adapter_result.json").write_text(
            json.dumps({"exit_code": 0, "total_cost_usd": cost, "num_turns": 3})
            if cost is not None
            else "{half-written"
        )
    return job


def test_summary_rolls_up_state_and_cost(tmp_path):
    job = make_job(
        tmp_path,
        "demo",
        {
            "status": "converged",
            "iteration": 3,
            "consecutive_no_progress": 0,
            "last_gate_summary": {"total": 2, "passed": True, "failing": []},
            "error": None,
        },
        {"iter-0001": 0.25, "iter-0003": 0.75},
    )
    row = gateway.job_summary(job)

    assert row["name"] == "demo"
    assert row["status"] == "converged" and row["terminal"] is True
    assert row["iteration"] == 3 and row["max_iterations"] == 7
    assert row["cost_usd"] == 1.0
    # Evidence dirs are the timeline; gaps (iter-0002 missing) are reported
    # honestly rather than inferred from the iteration counter.
    assert row["iterations_on_disk"] == 2
    assert row["last_evidence"] == "evidence/iter-0003"
    assert "error" not in row


def test_unparsable_state_degrades_to_a_row_not_an_exception(tmp_path):
    job = make_job(tmp_path, "broken", '{"status": "runn', {"iter-0001": None})
    row = gateway.job_summary(job)

    assert row["error"]
    assert row["status"] is None and row["terminal"] is False
    assert row["cost_usd"] is None  # unparsable adapter_result contributes nothing
    assert row["max_iterations"] == 7  # job.yaml is still readable


def test_detail_timeline_carries_gates_and_files(tmp_path):
    job = make_job(tmp_path, "demo", {"status": "running", "iteration": 1}, {})
    d = job / "evidence" / "iter-0001"
    d.mkdir()
    (d / "adapter_result.json").write_text(
        json.dumps({"exit_code": 0, "total_cost_usd": 0.5, "num_turns": 9})
    )
    (d / "gates.json").write_text(
        json.dumps([{"command": "node --test", "exit_code": 1, "output_tail": "x"}])
    )
    (d / "diff.patch").write_text("diff")

    entry = gateway.job_detail(job)["evidence"][0]
    assert entry["iter"] == "iter-0001"
    assert entry["files"] == ["adapter_result.json", "diff.patch", "gates.json"]
    assert entry["cost_usd"] == 0.5 and entry["num_turns"] == 9
    assert entry["gates"] == [{"command": "node --test", "exit_code": 1,
                               "timed_out": None}]


def test_sessions_cost_sums_every_session(monkeypatch, tmp_path):
    state = tmp_path / "agent"
    (state / "sessions").mkdir(parents=True)
    (state / "gateway").mkdir()
    monkeypatch.setattr(gateway, "STATE", state)
    monkeypatch.setattr(gateway, "GATEWAY", state / "gateway")
    for n, cost in ((1, 0.5), (2, 1.25)):
        (state / "sessions" / f"session-{n:04d}.json").write_text(
            json.dumps({"total_cost_usd": cost})
        )
    (state / "sessions" / "session-0003.json").write_text("{truncated")

    cost = gateway.sessions_cost()
    assert cost["sessions_usd"] == 1.75
    assert cost["current_run_sessions_usd"] is None  # no run recorded


def test_mission_first_line_skips_the_markdown_heading(tmp_path):
    p = tmp_path / "MISSION.md"
    p.write_text("# Mission\n\nI want a Snake game in my browser.\n")
    assert gateway.mission_first_line(p) == "I want a Snake game in my browser."
    assert gateway.mission_first_line(tmp_path / "absent.md") is None


def test_devstyle_report_extracted_from_notes(monkeypatch, tmp_path):
    state = tmp_path / "agent"
    state.mkdir()
    monkeypatch.setattr(gateway, "STATE", state)

    assert gateway.devstyle_report() is None  # no NOTES.md

    (state / "NOTES.md").write_text("STATUS: complete\n\nnothing structured here\n")
    assert gateway.devstyle_report() is None  # NOTES.md without the report

    (state / "NOTES.md").write_text(
        "STATUS: complete\n\n## Report\n\n"
        "- Style chosen: instant-ramen\n"
        "- Why: the job was small and the gates were cheap\n"
        "- Was it right in hindsight: yes\n"
    )
    assert gateway.devstyle_report() == {
        "style_chosen": "instant-ramen",
        "why": "the job was small and the gates were cheap",
        "hindsight": "yes",
    }
