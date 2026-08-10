"""Tests for the conversational window (agent_mindmap p2, steps 1-2).

The model's prose is not testable and is not the contract. What is: the
backend switch resolves the way agforge's does, a failed backend produces a
`failed` record carrying the backend's own words instead of an exception,
and a successful run's record carries the fields devpolicy/agent_records.md
asks for.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "gateway_window", Path(__file__).resolve().parent.parent / "agent" / "gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the module's state paths at a tmp dir so records and the
    `.local/.env` lookup never touch the real node."""
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    monkeypatch.setattr(gateway, "STATE", tmp_path / ".local" / "agent")
    monkeypatch.setattr(gateway, "WINDOW", tmp_path / ".local" / "agent" / "window")
    monkeypatch.setattr(gateway, "JOBS", tmp_path / ".local" / "jobs")
    for name in (
        "AUTOLAB_WINDOW_BACKEND",
        "AUTOLAB_WINDOW_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_backend_defaults_to_ollama(sandbox):
    assert gateway.window_backend() == "ollama"
    assert gateway.window_model("ollama") == "qwen3.6:35b-a3b-coding-nvfp4"


def test_process_env_wins_over_local_env_file(sandbox, monkeypatch):
    (sandbox / ".local").mkdir(parents=True, exist_ok=True)
    (sandbox / ".local" / ".env").write_text("AUTOLAB_WINDOW_BACKEND=ollama\n")
    assert gateway.window_backend() == "ollama"
    monkeypatch.setenv("AUTOLAB_WINDOW_BACKEND", "claude")
    assert gateway.window_backend() == "claude"
    assert gateway.window_model("claude") == "claude-sonnet-5"


def test_local_env_file_is_read_when_the_process_env_is_silent(sandbox):
    (sandbox / ".local").mkdir(parents=True, exist_ok=True)
    (sandbox / ".local" / ".env").write_text("AUTOLAB_WINDOW_BACKEND=claude\n")
    assert gateway.window_backend() == "claude"


def test_unknown_backend_is_refused(sandbox, monkeypatch):
    monkeypatch.setenv("AUTOLAB_WINDOW_BACKEND", "telepathy")
    with pytest.raises(gateway.WindowError):
        gateway.window_backend()


def test_a_failing_backend_is_recorded_not_raised(sandbox, monkeypatch):
    def explode(prompt):
        raise gateway.WindowError("ollama at http://nowhere is unreachable: refused")

    monkeypatch.setitem(gateway.WINDOW_BACKENDS, "ollama", explode)
    record = gateway.answer_window("what can you do?")
    assert record["outcome"] == "failed"
    assert record["backend"] == "ollama"
    # The failing party's words, verbatim — the harness fixes the path only.
    assert record["failure"] == "ollama at http://nowhere is unreachable: refused"
    assert "reply" not in record
    on_disk = json.loads((gateway.WINDOW / "run-0001.json").read_text())
    assert on_disk == record


def test_a_successful_run_records_the_policy_fields(sandbox, monkeypatch):
    monkeypatch.setitem(
        gateway.WINDOW_BACKENDS,
        "ollama",
        lambda prompt: ("six jobs converged.", {"model": "m1", "cost_usd": None}),
    )
    record = gateway.answer_window("which jobs finished?")
    assert record["outcome"] == "done"
    assert record["id"] == "window/run-0001"
    assert record["backend_model"] == "ollama/m1"
    assert record["cost_usd"] is None  # ollama reports no price; none invented
    assert isinstance(record["duration_ms"], int)
    assert record["reply"] == "six jobs converged."


def test_run_ids_do_not_collide(sandbox, monkeypatch):
    monkeypatch.setitem(
        gateway.WINDOW_BACKENDS, "ollama", lambda prompt: ("ok.", {"model": "m"})
    )
    ids = [gateway.answer_window("hi")["id"] for _ in range(3)]
    assert ids == ["window/run-0001", "window/run-0002", "window/run-0003"]


def test_the_prompt_carries_the_guide_and_the_live_job_state(sandbox, monkeypatch):
    (sandbox / ".local" / "jobs" / "demo").mkdir(parents=True)
    (sandbox / ".local" / "jobs" / "demo" / "state.json").write_text(
        json.dumps({"status": "converged", "iteration": 2})
    )
    monkeypatch.setattr(gateway, "GUIDE", sandbox / "GUIDE.md")
    (sandbox / "GUIDE.md").write_text("a job costs about 0.2 USD.")
    seen = {}

    def capture(prompt):
        seen["prompt"] = prompt
        return "ok.", {"model": "m"}

    monkeypatch.setitem(gateway.WINDOW_BACKENDS, "ollama", capture)
    gateway.answer_window("how did demo end?")
    assert "a job costs about 0.2 USD." in seen["prompt"]
    assert '"converged"' in seen["prompt"] and "demo" in seen["prompt"]
    assert "how did demo end?" in seen["prompt"]


def test_a_missing_guide_does_not_break_the_window(sandbox, monkeypatch):
    monkeypatch.setattr(gateway, "GUIDE", sandbox / "absent.md")
    assert "no capability card" in gateway.read_guide()


# --- claude binary resolution ------------------------------------------------
#
# The pointer file usually holds an absolute path into a version-numbered
# editor-extension directory, which goes stale on every update. A glob keeps
# working; these pin that behavior and the precedence around it.

def test_a_glob_pointer_resolves_to_the_newest_match(sandbox, monkeypatch):
    monkeypatch.delenv("AUTOLAB_CLAUDE_BIN", raising=False)
    gateway.STATE.mkdir(parents=True, exist_ok=True)
    for version, mtime in (("1.0", 1_000_000), ("2.0", 2_000_000)):
        d = sandbox / f"ext-{version}" / "bin"
        d.mkdir(parents=True)
        (d / "claude").write_text("#!/bin/sh\n")
        os.utime(d / "claude", (mtime, mtime))
    (gateway.STATE / "claude_bin").write_text(str(sandbox / "ext-*" / "bin" / "claude"))
    assert gateway.claude_bin() == str(sandbox / "ext-2.0" / "bin" / "claude")


def test_a_glob_that_matches_nothing_falls_through_to_path(sandbox, monkeypatch):
    monkeypatch.delenv("AUTOLAB_CLAUDE_BIN", raising=False)
    gateway.STATE.mkdir(parents=True, exist_ok=True)
    (gateway.STATE / "claude_bin").write_text(str(sandbox / "absent-*" / "claude"))
    assert gateway.claude_bin() == "claude"


def test_a_plain_path_is_returned_as_written(sandbox, monkeypatch):
    # Not probed for existence: a wrong plain path must fail loudly at launch
    # with the path in the message, which is what made the stale pointer
    # diagnosable in one read.
    monkeypatch.setenv("AUTOLAB_CLAUDE_BIN", "/nowhere/claude")
    assert gateway.claude_bin() == "/nowhere/claude"
