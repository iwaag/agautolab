"""Window records use canonical role/profile/harness/model identity."""

import importlib.util
import json
from pathlib import Path

import pytest

from agag.agent_config import AgentConfigError

spec = importlib.util.spec_from_file_location(
    "gateway_window", Path(__file__).resolve().parent.parent / "agent" / "gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    monkeypatch.setattr(gateway, "STATE", tmp_path / ".local" / "agent")
    monkeypatch.setattr(gateway, "WINDOW", tmp_path / ".local" / "agent" / "window")
    monkeypatch.setattr(gateway, "JOBS", tmp_path / ".local" / "jobs")
    return tmp_path


def meta(outcome="done"):
    return {
        "schema": "ag.agent-run.v1", "role": "front", "profile": "local",
        "harness": "opencode", "provider": "ollama",
        "model": "ollama/qwen3.6:35b-a3b-coding-nvfp4", "outcome": outcome,
        "duration_ms": 9, "cost_usd": 0.0,
    }


def test_a_successful_run_records_normalized_fields(sandbox, monkeypatch):
    monkeypatch.setattr(gateway, "run_role", lambda *a, **k: ("six jobs converged.", meta(), 0))
    record = gateway.answer_window("which jobs finished?")
    assert record["id"] == "window/run-0001"
    assert record["role"] == "front"
    assert record["profile"] == "local"
    assert record["harness"] == "opencode"
    assert record["model"].startswith("ollama/")
    assert "backend" not in record and "backend_model" not in record
    assert record["reply"] == "six jobs converged."


def test_config_failure_is_recorded_without_fallback(sandbox, monkeypatch):
    def fail(*args, **kwargs):
        raise AgentConfigError("E_UNAVAILABLE", "selected command is absent")
    monkeypatch.setattr(gateway, "run_role", fail)
    record = gateway.answer_window("hello")
    assert record["outcome"] == "failed"
    assert record["failure"].startswith("E_UNAVAILABLE")
    assert "reply" not in record
    assert json.loads((gateway.WINDOW / "run-0001.json").read_text()) == record


def test_harness_error_metadata_is_preserved(sandbox, monkeypatch):
    failed = meta("failed") | {"failure": "opencode exited 2"}
    monkeypatch.setattr(gateway, "run_role", lambda *a, **k: ("diagnostic", failed, -1))
    record = gateway.answer_window("hello")
    assert record["outcome"] == "failed"
    assert record["failure"] == "opencode exited 2"
    assert "reply" not in record


def test_run_ids_do_not_collide(sandbox, monkeypatch):
    monkeypatch.setattr(gateway, "run_role", lambda *a, **k: ("ok", meta(), 0))
    ids = [gateway.answer_window("hi")["id"] for _ in range(3)]
    assert ids == ["window/run-0001", "window/run-0002", "window/run-0003"]


def test_prompt_carries_guide_state_and_user_message(sandbox, monkeypatch):
    (sandbox / ".local" / "jobs" / "demo").mkdir(parents=True)
    (sandbox / ".local" / "jobs" / "demo" / "state.json").write_text(
        json.dumps({"status": "converged", "iteration": 2})
    )
    monkeypatch.setattr(gateway, "GUIDE", sandbox / "GUIDE.md")
    (sandbox / "GUIDE.md").write_text("capability card")
    seen = {}
    def capture(role, prompt, **kwargs):
        seen["prompt"] = prompt
        return "ok", meta(), 0
    monkeypatch.setattr(gateway, "run_role", capture)
    gateway.answer_window("how did demo end?")
    assert "capability card" in seen["prompt"]
    assert '"converged"' in seen["prompt"] and "demo" in seen["prompt"]
    assert "how did demo end?" in seen["prompt"]


def test_mission_block_is_applied_after_success(sandbox, monkeypatch):
    monkeypatch.setattr(gateway, "run_role", lambda *a, **k: (
        "Starting.\n<<mission max_sessions=2>>build it<</mission>>", meta(), 0))
    monkeypatch.setattr(gateway, "start_mission", lambda mission, count: (
        202, {"accepted": True, "mission_text": mission, "max": count}))
    record = gateway.answer_window("build it")
    assert record["reply"] == "Starting."
    assert record["mission"]["status"] == 202


def test_a_missing_guide_does_not_break_the_window(sandbox, monkeypatch):
    monkeypatch.setattr(gateway, "GUIDE", sandbox / "absent.md")
    assert "no capability card" in gateway.read_guide()
