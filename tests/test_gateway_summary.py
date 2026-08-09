"""Tests for the on-node iteration summarizer (human-in-loop ex1, step 1).

The endpoint itself is a thin wrapper; what is worth pinning down is the state
machine around a paid subprocess: the `.md` file is the only success signal, a
crashed summarizer must read as `error` rather than as a cached summary, and
the one-at-a-time guard must see a live run in any job.
"""

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


def test_done_reads_the_cached_file(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    (job / "summaries" / "iter-0001.md").write_text("it changed one file.\n")
    doc = gateway.summary_status(job, "iter-0001")
    assert doc["status"] == "done" and doc["mtime"] > 0


def test_live_pid_without_exit_file_is_pending(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    (job / "summaries" / "iter-0001.run.json").write_text(
        json.dumps({"pid": os.getpid(), "started": time.time()})
    )
    assert gateway.summary_status(job, "iter-0001")["status"] == "pending"


def test_exit_zero_without_a_summary_is_an_error_not_a_success(tmp_path):
    # The failure mode that would otherwise poison the cache: claude exits 0
    # but writes nothing, so `.md` never appears. Serving that as `done` would
    # hand the caller an empty summary forever.
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    (job / "summaries" / "iter-0001.run.json").write_text(json.dumps({"pid": 1}))
    (job / "summaries" / "iter-0001.exit").write_text("0\n")
    doc = gateway.summary_status(job, "iter-0001")
    assert doc["status"] == "error" and "0" in doc["error"]


def test_dead_pid_without_exit_file_is_an_error(tmp_path):
    job = make_iter(tmp_path, "demo", "iter-0001")
    (job / "summaries").mkdir()
    # A pid that is really gone: the wrapper was killed before its last echo.
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    dead = proc.pid  # reaped, so gone
    (job / "summaries" / "iter-0001.run.json").write_text(json.dumps({"pid": dead}))
    assert gateway.summary_status(job, "iter-0001")["status"] == "error"


def test_running_guard_sees_a_live_run_in_another_job(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "JOBS", tmp_path)
    make_iter(tmp_path, "other", "iter-0002")
    (tmp_path / "other" / "summaries").mkdir()
    (tmp_path / "other" / "summaries" / "iter-0002.run.json").write_text(
        json.dumps({"pid": os.getpid()})
    )
    mine = make_iter(tmp_path, "demo", "iter-0001")
    busy = gateway.summary_running(mine, "iter-0001")
    assert busy["job"] == "other" and busy["self"] is False
    # A finished run must not keep the guard closed forever.
    (tmp_path / "other" / "summaries" / "iter-0002.exit").write_text("0\n")
    assert gateway.summary_running(mine, "iter-0001") is None


def fake_claude(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake-claude"
    fake.write_text(f"#!/bin/sh\ncat >/dev/null\n{body}\n")
    fake.chmod(0o755)
    return fake


def test_summarizer_writes_the_summary_as_written(tmp_path, monkeypatch):
    # Drive the real spawn path with a fake `claude` so the summary and cost
    # recording are exercised without spending money.
    # A quoted heredoc: the JSON must reach the extractor with its `\n`
    # escapes intact, which `echo` in /bin/sh would expand.
    fake = fake_claude(
        tmp_path,
        "cat <<'JSON'\n"
        '{"is_error": false, "result": "Now reading.\\n\\na fake summary",'
        ' "total_cost_usd": 0.03, "num_turns": 4}\n'
        "JSON",
    )
    monkeypatch.setenv("AUTOLAB_CLAUDE_BIN", str(fake))
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(gateway, "JOBS", jobs)
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    job = make_iter(jobs, "demo", "iter-0001")

    gateway.start_summarizer(job, "iter-0001")
    for _ in range(100):
        if gateway.summary_status(job, "iter-0001")["status"] != "pending":
            break
        time.sleep(0.1)
    done = gateway.summary_status(job, "iter-0001")
    assert done["status"] == "done"
    assert done["summarizer"]["cost_usd"] == 0.03
    # Including the model's opening line: nothing here edits its words.
    assert (job / "summaries" / "iter-0001.md").read_text() == (
        "Now reading.\n\na fake summary\n"
    )
    # The prompt names exactly one evidence directory — the containment the
    # summarizer is given instead of a sandbox.
    prompt = (job / "summaries" / "iter-0001.prompt.txt").read_text()
    assert ".local/jobs/demo/evidence/iter-0001" in prompt


def test_failed_summarizer_leaves_no_cached_summary(tmp_path, monkeypatch):
    fake = fake_claude(tmp_path, "echo oops >&2\nexit 3")
    monkeypatch.setenv("AUTOLAB_CLAUDE_BIN", str(fake))
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(gateway, "JOBS", jobs)
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    job = make_iter(jobs, "demo", "iter-0001")

    gateway.start_summarizer(job, "iter-0001")
    for _ in range(100):
        if gateway.summary_status(job, "iter-0001")["status"] != "pending":
            break
        time.sleep(0.1)
    doc = gateway.summary_status(job, "iter-0001")
    assert doc["status"] == "error" and "3" in doc["error"]
    assert not (job / "summaries" / "iter-0001.md").exists()


def test_a_partial_summary_is_still_served(tmp_path, monkeypatch):
    """claude exits 0 on a refusal or a max-turns stop. Its partial words are
    what it has to say about the iteration; `?force=1` regenerates."""
    fake = fake_claude(
        tmp_path,
        "echo '{\"is_error\": true, \"subtype\": \"error_max_turns\", "
        "\"result\": \"partial\"}'",
    )
    monkeypatch.setenv("AUTOLAB_CLAUDE_BIN", str(fake))
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(gateway, "JOBS", jobs)
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    job = make_iter(jobs, "demo", "iter-0001")

    gateway.start_summarizer(job, "iter-0001")
    for _ in range(100):
        if gateway.summary_status(job, "iter-0001")["status"] != "pending":
            break
        time.sleep(0.1)
    done = gateway.summary_status(job, "iter-0001")
    assert done["status"] == "done"
    assert (job / "summaries" / "iter-0001.md").read_text() == "partial\n"
