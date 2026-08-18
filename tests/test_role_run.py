from pathlib import Path

from agag.agent_config import ResolvedAgent
from agag.harness import HarnessResult

from agautolab import role_run


def resolved(role: str, harness: str = "agcode") -> ResolvedAgent:
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


def test_front_runs_harness_in_the_callers_workspace(monkeypatch, tmp_path):
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
    # No workspace pin for `front`: the listener points it at a topic
    # workspace and the gateway at its own, so the caller's cwd must win.
    assert calls[0][2]["cwd"] == tmp_path
    assert calls[0][2]["allowed_tools"] == role_run.ROLE_ALLOWED_TOOLS["front"]
    # `front` works, so it gets agcode's full tool set: no preset flag, just
    # the turn/deadline budget every agcode role carries.
    assert calls[0][2]["extra_args"] == [
        "--max-turns", str(role_run.AGCODE_MAX_TURNS),
        "--max-tokens", str(role_run.AGCODE_MAX_TOKENS),
        "--deadline-s", "60.0",
    ]
    assert calls[0][2]["transcript_path"] == tmp_path / "raw.jsonl"
    # agcode has no permission engine to bypass.
    assert calls[0][2]["skip_permissions"] is False


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
    # claude_code gets no agcode preset flag; its grant is allowed_tools.
    assert calls[0]["extra_args"] is None
    # claude_code's classifier denies allowed commands in non-interactive
    # runs, so it is bypassed for the workspace-bound roles.
    assert calls[0]["skip_permissions"] is True


def test_readonly_role_on_agcode_is_handed_fewer_tools(monkeypatch, tmp_path):
    """Under agcode the tool set *is* the permission: `summarizer` is offered
    read/list and nothing else, rather than being denied a tool it was shown."""
    calls = []
    monkeypatch.setattr(role_run, "resolve_project_role", lambda *a, **k: resolved("summarizer"))
    monkeypatch.setattr(role_run, "load_project_roles", lambda project: {})
    monkeypatch.setattr(
        role_run,
        "run_harness",
        lambda agent, prompt, **kwargs: (
            calls.append(kwargs) or HarnessResult("done", 0, {"outcome": "done"})
        ),
    )

    role_run.run_role("summarizer", "summarize", cwd=tmp_path, timeout=5)

    assert calls[0]["extra_args"] == [
        "--max-turns", str(role_run.AGCODE_MAX_TURNS),
        "--max-tokens", str(role_run.AGCODE_MAX_TOKENS),
        "--deadline-s", "60.0",
        "--tools", "read-only",
    ]


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


def test_supercoder_is_a_configured_role_with_the_working_grant():
    """A run- serving resolves `supercoder`; without an allowlist entry
    claude_code would omit `--allowedTools` and wait for an interactive
    permission answer until the timeout."""
    from agautolab.agent_settings import resolve_project_role

    assert role_run.ROLE_ALLOWED_TOOLS["supercoder"] == role_run.WORKING_ALLOWED_TOOLS
    assert resolve_project_role("supercoder").profile == "sonnet"
