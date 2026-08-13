from pathlib import Path

from agag.agent_config import ResolvedAgent
from agag.harness import HarnessResult

from agautolab import role_run


def resolved(role: str, harness: str = "opencode") -> ResolvedAgent:
    return ResolvedAgent(
        role=role,
        profile="test",
        harness=harness,
        provider="ollama",
        model="ollama/test",
        model_options={},
        command="agent",
        provider_base_url="http://localhost",
    )


def test_front_runs_harness_in_its_fixed_workspace(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(role_run, "resolve_project_role", lambda *a, **k: resolved("front"))
    monkeypatch.setattr(role_run, "load_project_roles", lambda project: {})

    def fake_run(agent, prompt, **kwargs):
        calls.append((agent, prompt, kwargs))
        return HarnessResult("answer", 0, {"role": "front", "outcome": "done"})

    monkeypatch.setattr(role_run, "run_harness", fake_run)
    output, record, code = role_run.run_role(
        "front", "question", cwd=tmp_path, timeout=12, transcript=tmp_path / "raw.jsonl"
    )

    assert (output, code) == ("answer", 0)
    assert record["outcome"] == "done"
    assert calls[0][2]["cwd"] == role_run.PROJECT_ROOT / "agent" / "front"
    assert calls[0][2]["allowed_tools"] == role_run.ROLE_ALLOWED_TOOLS["front"]
    assert calls[0][2]["opencode_config"] == role_run.PROJECT_ROOT / "agent/opencode-front.json"
    assert calls[0][2]["transcript_path"] == tmp_path / "raw.jsonl"


def test_mediator_runs_in_its_fixed_workspace(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(role_run, "resolve_project_role", lambda *a, **k: resolved("mediator", "claude_code"))
    monkeypatch.setattr(role_run, "load_project_roles", lambda project: {})
    monkeypatch.setattr(
        role_run,
        "run_harness",
        lambda agent, prompt, **kwargs: (
            calls.append(kwargs) or HarnessResult("done", 0, {"outcome": "done"})
        ),
    )

    role_run.run_role("mediator", "work", cwd=tmp_path, timeout=5)

    assert calls[0]["cwd"] == role_run.PROJECT_ROOT / "agent" / "mediator"
    assert calls[0]["opencode_config"] is None


def test_resolution_checks_harness_availability(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(role_run, "load_project_roles", lambda project: {})

    def fake_resolve(role, **kwargs):
        seen.append(kwargs)
        return resolved(role)

    monkeypatch.setattr(role_run, "resolve_project_role", fake_resolve)
    monkeypatch.setattr(
        role_run,
        "run_harness",
        lambda *a, **k: HarnessResult("done", 0, {"outcome": "done"}),
    )

    role_run.run_role("front", "work", cwd=tmp_path, timeout=5)

    assert "check_available" not in seen[0]
