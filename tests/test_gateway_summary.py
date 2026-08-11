"""State and spawn tests for the profile-selected iteration summarizer."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gateway_summary", Path(__file__).resolve().parent.parent / "agent" / "gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def make_iter(root: Path, job: str, iteration: str) -> Path:
    d = root / job / "evidence" / iteration
    d.mkdir(parents=True)
    (d / "adapter_result.json").write_text(json.dumps({"exit_code": 0}))
    return root / job


def test_absent_when_nothing_ran(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    assert gateway.summary_status(job, "iter-0001") == {"status": "absent"}


def test_done_reads_cached_file_and_normalized_record(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    (job / "summaries" / "iter-0001.md").write_text("it changed one file.\n")
    (job / "summaries" / "iter-0001.cost.json").write_text(json.dumps({
        "role": "summarizer", "profile": "sonnet", "harness": "claude_code",
        "provider": "anthropic", "model": "anthropic/claude-sonnet-5",
        "outcome": "done", "cost_usd": 0.03,
    }))
    doc = gateway.summary_status(job, "iter-0001")
    assert doc["status"] == "done"
    assert doc["summarizer"]["role"] == "summarizer"
    assert doc["summarizer"]["cost_usd"] == 0.03


def test_live_pid_without_exit_file_is_pending(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    (job / "summaries" / "iter-0001.run.json").write_text(
        json.dumps({"pid": os.getpid(), "started": time.time()}))
    assert gateway.summary_status(job, "iter-0001")["status"] == "pending"


def test_exit_without_a_summary_is_error(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    (job / "summaries" / "iter-0001.run.json").write_text(json.dumps({"pid": 1}))
    (job / "summaries" / "iter-0001.exit").write_text("0\n")
    assert gateway.summary_status(job, "iter-0001")["status"] == "error"


def test_dead_pid_without_exit_file_is_error(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    (job / "summaries" / "iter-0001.run.json").write_text(json.dumps({"pid": proc.pid}))
    assert gateway.summary_status(job, "iter-0001")["status"] == "error"


def test_running_guard_sees_live_run_in_another_job(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "JOBS", tmp_path)
    make_iter(tmp_path, "other", "iter-0002")
    (tmp_path / "other" / "summaries").mkdir()
    (tmp_path / "other" / "summaries" / "iter-0002.run.json").write_text(
        json.dumps({"pid": os.getpid()}))
    mine = make_iter(tmp_path, "demo", "iter-0001")
    assert gateway.summary_running(mine, "iter-0001")["job"] == "other"
    (tmp_path / "other" / "summaries" / "iter-0002.exit").write_text("0\n")
    assert gateway.summary_running(mine, "iter-0001") is None


def test_start_uses_common_role_runner(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(gateway, "JOBS", jobs)
    job = make_iter(jobs, "demo", "iter-0001")
    seen = {}
    class Process:
        pid = 4321
    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return Process()
    monkeypatch.setattr(gateway.subprocess, "Popen", fake_popen)
    assert gateway.start_summarizer(job, "iter-0001") == 4321
    command = seen["argv"][2]
    assert "agautolab.role_run summarizer" in command
    assert "--record" in command and "--transcript" in command
    run = json.loads((job / "summaries" / "iter-0001.run.json").read_text())
    assert run["role"] == "summarizer"
