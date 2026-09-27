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
from agautolab.instance import COMFYNOTIFY_BIN, PROVISIONER_ENV


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


def test_gemini_roles_run_on_the_bypass_and_readonly_ones_on_plan(monkeypatch, tmp_path):
    """gemini_cli's permission story is its approval mode: the working roles
    get the same bypass as claude_code (spelled `yolo` by the harness), and a
    read-only role keeps `plan` — which the bypass would otherwise override."""
    calls = harness_calls(monkeypatch, "coding", "gemini_cli")
    role_run.run_role("coding", "work", cwd=tmp_path, timeout=5)
    _, kwargs = calls[0]
    assert kwargs["skip_permissions"] is True
    assert kwargs["extra_args"] == []

    calls = harness_calls(monkeypatch, "summarizer", "gemini_cli")
    role_run.run_role("summarizer", "summarize", cwd=tmp_path, timeout=5)
    _, kwargs = calls[0]
    assert kwargs["skip_permissions"] is False
    assert kwargs["extra_args"] == ["--approval-mode", "plan"]


def test_the_gemini_profile_is_declared_and_resolves(monkeypatch):
    config, overlay = load_config(role_run.SPEC.agents_config, Path("/nonexistent"))
    agent = resolve_role(config, overlay, "coding", profile_override="gemini", check_available=False)
    assert (agent.harness, agent.provider, agent.native_model) == ("gemini_cli", "google", "gemini-2.5-flash")


def test_agy_roles_all_run_on_the_bypass_including_the_readonly_one(monkeypatch, tmp_path):
    """agy has no read-only door: headless mode auto-denies reads as well as
    commands, and `--mode plan` writes a plan instead of answering. So the
    summarizer gets the bypass, not a mode, and no harness args."""
    for role in ("coding", "summarizer"):
        calls = harness_calls(monkeypatch, role, "agy")
        role_run.run_role(role, "work", cwd=tmp_path, timeout=5)
        _, kwargs = calls[0]
        assert kwargs["skip_permissions"] is True, role
        assert kwargs["extra_args"] is None, role


def test_the_agy_profiles_are_declared_and_resolve(monkeypatch):
    config, overlay = load_config(role_run.SPEC.agents_config, Path("/nonexistent"))
    agent = resolve_role(config, overlay, "coding", profile_override="agy", check_available=False)
    assert (agent.harness, agent.provider, agent.native_model) == ("agy", "antigravity", "gemini-3.8-flash-medium")
    agent = resolve_role(config, overlay, "coding", profile_override="agy-claude", check_available=False)
    assert (agent.harness, agent.provider, agent.native_model) == ("agy", "antigravity", "claude-sonnet-4-6")


def test_codex_readonly_role_gets_the_read_only_sandbox_and_the_rest_the_bypass(monkeypatch, tmp_path):
    """codex's sandbox is the whole permission: `summarizer` gets `-s
    read-only` and no bypass (the bypass would turn it into full access);
    every other role gets `danger-full-access` through `skip_permissions`,
    because `workspace-write` can neither commit nor reach Zulip."""
    calls = harness_calls(monkeypatch, "coding", "codex")
    role_run.run_role("coding", "work", cwd=tmp_path, timeout=5)
    _, kwargs = calls[0]
    assert kwargs["skip_permissions"] is True
    assert kwargs["extra_args"] == []

    calls = harness_calls(monkeypatch, "summarizer", "codex")
    role_run.run_role("summarizer", "summarize", cwd=tmp_path, timeout=5)
    _, kwargs = calls[0]
    assert kwargs["skip_permissions"] is False
    assert kwargs["extra_args"] == ["-s", "read-only"]


def test_the_codex_profiles_are_declared_and_resolve(monkeypatch):
    config, overlay = load_config(role_run.SPEC.agents_config, Path("/nonexistent"))
    agent = resolve_role(config, overlay, "coding", profile_override="codex", check_available=False)
    assert (agent.harness, agent.provider, agent.native_model) == ("codex", "openai", "gpt-5.6-terra")
    assert agent.model_options == {"effort": "medium"}
    agent = resolve_role(config, overlay, "summarizer", profile_override="codex-mini", check_available=False)
    assert (agent.harness, agent.provider, agent.native_model) == ("codex", "openai", "gpt-5.4-mini")
    assert agent.model_options == {"effort": "low"}


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


def test_every_role_is_given_the_provisioner_credential_path_not_its_value():
    environment = role_run.SPEC.extra_environment({})
    assert environment["AGAG_ZULIP_ADMIN_ENV"] == str(PROVISIONER_ENV)
    if COMFYNOTIFY_BIN.is_dir():
        assert environment["PATH"].split(":")[0] == str(COMFYNOTIFY_BIN)


def test_extra_meta_reaches_the_record_beside_the_project(monkeypatch, tmp_path):
    """61510bd made the listener stamp the conversation into every run record,
    but this wrapper did not accept `extra_meta`, so every listener-started
    run failed with a TypeError before the harness started — seen live from
    the Front Desk on 2026-09-08 (`front_desk` p1 step 4)."""
    harness_calls(monkeypatch, "superdirector")
    _, record, code = role_run.run_role(
        "superdirector", "plan", cwd=tmp_path, timeout=5, project="ghtrends",
        extra_meta={"conversation": "pj-ghtrends/workplan-trend6"},
    )
    assert code == 0
    assert record["project"] == "ghtrends"
    assert record["conversation"] == "pj-ghtrends/workplan-trend6"


def test_a_run_inside_a_serving_keeps_a_live_execution_record(monkeypatch, tmp_path):
    """failsafe p2 step 2: the health interface's input. Inside a listener's
    serving the run is given a record path named after the serving and the
    role, and the record names the serving's ack and conversation; outside
    one (a CLI, a test) no record is kept."""
    from agag import serving

    calls = harness_calls(monkeypatch, "supercoder", harness="claude_code")
    monkeypatch.setattr(role_run, "EXECUTIONS_DIR", tmp_path / "executions")
    role_run.run_role("supercoder", "q", cwd=tmp_path, timeout=60)
    assert calls[-1][1]["live"] is None

    journal = serving.NullJournal(trigger_id=5)
    journal._serving = serving.Serving(7, "work-m1", "workrun-task1-m1", "owner", 5, ack_id=41)
    with serving.bound(journal):
        role_run.run_role("supercoder", "q", cwd=tmp_path, timeout=60)
    live = calls[-1][1]["live"]
    assert live.path.parent == tmp_path / "executions" and live.path.name.startswith("s7-supercoder-")
    assert live.doc["serving"]["ack"] == 41 and live.doc["serving"]["topic"] == "workrun-task1-m1"


def test_old_execution_records_are_pruned(monkeypatch, tmp_path):
    monkeypatch.setattr(role_run, "EXECUTIONS_DIR", tmp_path)
    for n in range(5):
        (tmp_path / f"r{n}.json").write_text("{}")
    role_run.prune_executions(keep=2)
    assert len(list(tmp_path.glob("*.json"))) == 2
