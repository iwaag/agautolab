"""Shared fixture conformance and all-role profile resolution."""

import re
from pathlib import Path

import pytest

from agautolab.agent_config import AgentConfigError, load_config, resolve_role
from agautolab.run_once import run_once

EXAMPLES = Path(__file__).resolve().parents[3] / "devpolicy" / "contracts" / "agent" / "examples"


@pytest.mark.parametrize("project", ["agforge", "agautolab", "agdevworld"])
def test_valid_contract_examples(project):
    main = EXAMPLES / "valid" / project / "agents.toml"
    local = EXAMPLES / "valid" / project / "agents.local.toml"
    config, overlay = load_config(main, local if local.exists() else None)
    for role in config.get("roles", {}):
        resolved = resolve_role(config, overlay, role, check_available=False)
        assert resolved.role == role
        assert resolved.harness in {"opencode", "claude_code", "fake"}


@pytest.mark.parametrize("fixture", sorted((EXAMPLES / "invalid").glob("*.toml")),
                         ids=lambda path: path.stem)
def test_invalid_contract_examples_report_expected_code(fixture):
    expected = re.search(r"# EXPECT: (E_[A-Z_]+)", fixture.read_text()).group(1)
    is_overlay = fixture.name.startswith("overlay-")
    committed = EXAMPLES / "valid" / "agautolab" / "agents.toml" if is_overlay else fixture
    overlay = fixture if is_overlay else None
    with pytest.raises(AgentConfigError) as caught:
        config, local = load_config(committed, overlay)
        for role in config.get("roles", {}):
            resolve_role(config, local, role, check_available=False)
    assert caught.value.code == expected


def test_every_agautolab_role_resolves_and_overlay_can_override(tmp_path):
    committed = Path(__file__).resolve().parents[1] / "agents.toml"
    overlay = tmp_path / "agents.local.toml"
    overlay.write_text('''schema = "ag.agent-config.v1"
[roles.front]
profile = "sonnet-coder"
''')
    config, local = load_config(committed, overlay)
    resolved = {role: resolve_role(config, local, role, check_available=False)
                for role in ("front", "director", "mediator", "coding", "summarizer")}
    assert resolved["front"].profile == "sonnet-coder"
    assert set(resolved) == {"front", "director", "mediator", "coding", "summarizer"}


def test_unknown_job_profile_has_contract_code():
    committed = Path(__file__).resolve().parents[1] / "agents.toml"
    config, local = load_config(committed)
    with pytest.raises(AgentConfigError) as caught:
        resolve_role(config, local, "coding", profile_override="absent", check_available=False)
    assert caught.value.code == "E_UNKNOWN_PROFILE"


def test_anthropic_secret_file_reference_becomes_process_environment(tmp_path):
    committed = Path(__file__).resolve().parents[1] / "agents.toml"
    secret = tmp_path / "anthropic-key"
    secret.write_text("deployment-secret\n")
    overlay = tmp_path / "agents.local.toml"
    overlay.write_text(f'''schema = "ag.agent-config.v1"
[local.harness.claude_code]
command = "/usr/bin/true"
[local.secrets]
anthropic_api_key_file = "{secret}"
''')
    config, local = load_config(committed, overlay)
    resolved = resolve_role(config, local, "coding")
    assert resolved.environment == {"ANTHROPIC_API_KEY": "deployment-secret"}


def test_job_profile_override_fails_with_contract_code(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.yaml").write_text('''goal: test
profile: absent
gates: ["true"]
''')
    assert run_once(job) == 30
    assert "E_UNKNOWN_PROFILE" in (job / "state.json").read_text()
