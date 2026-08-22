"""autolab's `run_role` is the skeleton's, plus the budget and the bypass.

The skeleton (`agag.agent.run_role`) is tested in pyagag. What is checked
here is what autolab adds: the workspace pin, agcode's budget, the
claude_code bypass, the per-project profile, the read-only tool set, and
that every role's grant now lives in `agents.toml`.
"""

from pathlib import Path

import agag.agent as skeleton
from agag.agent_config import ResolvedAgent, load_config, resolve_role
from agag.harness import HarnessResult

from agautolab import role_run


def resolved(role: str, harness: str = "agcode", allowed: str = "Read") -> ResolvedAgent:
    return ResolvedAgent(
        role=role,
        profile="test",
        harness=harness,
        provider="ollama",
        model="ollama/test",
        model_options={},
        command="agent",
        provider_base_url="http://localhost",
        allowed_tools=allowed,
    )


def harness_calls(monkeypatch, role, harness="agcode"):
    calls = []
    monkeypatch.setattr(
        role_run, "resolve_spec_role", lambda spec, r, **k: resolved(role, harness)
    )
    monkeypatch.setattr(role_run, "load_project_roles", lambda project: {})
    monkeypatch.setattr(
        skeleton,
        "run_harness",
        lambda agent, prompt, **kwargs: (
            calls.append((agent, kwargs)) or HarnessResult("answer", 0, {"role": role, "outcome": "done"})
        ),
    )
    return calls


def test_front_runs_in_the_callers_workspace_with_agcode_s_budget(monkeypatch, tmp_path):
    calls = harness_calls(monkeypatch, "front")

    output, record, code = role_run.run_role(
        "front", "question", cwd=tmp_path, timeout=12, transcript=tmp_path / "raw.jsonl"
    )

    assert (output, code) == ("answer", 0)
    assert record["outcome"] == "done"
    agent, kwargs = calls[0]
    # No workspace pin for `front`: the listener points it at a topic
    # workspace and the gateway at its own, so the caller's cwd must win.
    assert kwargs["cwd"] == tmp_path
    assert kwargs["allowed_tools"] == agent.allowed_tools
    assert kwargs["extra_args"] == [
        "--max-turns", str(role_run.AGCODE_MAX_TURNS),
        "--max-tokens", str(role_run.AGCODE_MAX_TOKENS),
        "--deadline-s", "60.0",
    ]
    assert kwargs["transcript_path"] == tmp_path / "raw.jsonl"
    # agcode has no permission engine to bypass.
    assert kwargs["skip_permissions"] is False


def test_mediator_runs_in_its_fixed_workspace_and_bypasses_the_classifier(monkeypatch, tmp_path):
    calls = harness_calls(monkeypatch, "mediator", "claude_code")

    role_run.run_role("mediator", "work", cwd=tmp_path, timeout=5)

    _, kwargs = calls[0]
    assert kwargs["cwd"] == role_run.PROJECT_ROOT / "agent" / "mediator"
    assert kwargs["extra_args"] is None
    assert kwargs["skip_permissions"] is True


def test_readonly_role_on_agcode_is_handed_fewer_tools(monkeypatch, tmp_path):
    calls = harness_calls(monkeypatch, "summarizer")

    role_run.run_role("summarizer", "summarize", cwd=tmp_path, timeout=5)

    assert calls[0][1]["extra_args"][-2:] == ["--tools", "read-only"]


def test_the_project_profile_wins_and_the_project_is_recorded(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(role_run, "load_project_roles", lambda project: {"coding": "local"})
    monkeypatch.setattr(
        role_run, "resolve_spec_role",
        lambda spec, r, **k: seen.append(k) or resolved(r),
    )
    monkeypatch.setattr(
        skeleton, "run_harness",
        lambda agent, prompt, **kwargs: HarnessResult("ok", 0, {"outcome": "done"}),
    )

    _, record, _ = role_run.run_role("coding", "work", cwd=tmp_path, timeout=5, project="demo")

    assert seen[0]["profile_override"] == "local"
    assert record["project"] == "demo"


def test_every_role_carries_its_grant_in_agents_toml():
    """The table the three agents used to repeat is now the role itself."""
    config, overlay = load_config(role_run.SPEC.agents_config, Path("/nonexistent"))
    for role in ("front", "director", "mediator", "coding", "superdirector", "supercoder"):
        grant = resolve_role(config, overlay, role, check_available=False).allowed_tools
        assert "Bash(agentchat:*)" in grant and "Write" in grant, role
    assert resolve_role(config, overlay, "summarizer", check_available=False).allowed_tools == "Read,Glob,Grep"
