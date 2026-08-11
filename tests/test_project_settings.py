"""Per-project agent selection and its run-record evidence."""

import json
from pathlib import Path

import pytest
import yaml
from agag.agent_config import AgentConfigError
from agag.harness import HarnessResult, identity

from agautolab import project_settings, role_run
from agautolab.project_settings import ProjectSettingsError, load_project_roles
from agautolab.role_run import run_role
from agautolab.run_once import run_once
from agautolab.state import EXIT_CONVERGED, EXIT_ERROR


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / ".local" / "projects"
    root.mkdir(parents=True)
    monkeypatch.setattr(project_settings, "PROJECTS_ROOT", root)
    return root


def write_project(projects_root: Path, name: str, content: str) -> Path:
    root = projects_root / name
    root.mkdir(parents=True)
    (root / "agents.toml").write_text(content, encoding="utf-8")
    return root


def write_job(job_dir: Path, **overrides) -> None:
    document = {
        "goal": "project profile test",
        "adapter": "fake",
        "gates": ["true"],
        **overrides,
    }
    job_dir.mkdir()
    (job_dir / "job.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def test_loader_reads_roles_and_missing_file_means_defaults(projects_root):
    write_project(projects_root, "yokai", '[roles]\ncoding = "stub"\ndirector = "stub"\n')
    assert load_project_roles("yokai") == {"coding": "stub", "director": "stub"}
    assert load_project_roles("missing") == {}
    assert load_project_roles(None) == {}


@pytest.mark.parametrize(
    "content, message",
    [
        ("[roles\ncoding = 'stub'", "cannot read project agent settings"),
        ('[roles]\nfront = "stub"\n', r"unknown role\(s\): front"),
        ('[roles]\ncoding = ""\n', "roles.coding must be a non-empty string"),
    ],
)
def test_loader_rejects_invalid_present_files(projects_root, content, message):
    write_project(projects_root, "bad", content)
    with pytest.raises(ProjectSettingsError, match=message):
        load_project_roles("bad")


def test_job_profile_precedes_project_profile(projects_root, tmp_path):
    write_project(projects_root, "demo", '[roles]\ncoding = "absent"\n')
    job = tmp_path / "job"
    write_job(job, project="demo", profile="stub")
    assert run_once(job) == EXIT_CONVERGED
    evidence = json.loads((job / "evidence" / "iter-0001" / "adapter_result.json").read_text())
    assert evidence["project"] == "demo"
    assert evidence["profile"] == "stub"


def test_project_profile_is_used_when_job_has_no_override(projects_root, tmp_path):
    write_project(projects_root, "demo", '[roles]\ncoding = "stub"\n')
    job = tmp_path / "job"
    write_job(job, project="demo")
    assert run_once(job) == EXIT_CONVERGED
    evidence = json.loads((job / "evidence" / "iter-0001" / "adapter_result.json").read_text())
    assert (evidence["project"], evidence["profile"]) == ("demo", "stub")


def test_unknown_project_profile_fails_visibly(projects_root, tmp_path):
    write_project(projects_root, "demo", '[roles]\ncoding = "absent"\n')
    job = tmp_path / "job"
    write_job(job, project="demo")
    assert run_once(job) == EXIT_ERROR
    assert "E_UNKNOWN_PROFILE" in (job / "state.json").read_text()


def test_director_discovers_project_and_records_resolved_profile(
    projects_root, tmp_path, monkeypatch
):
    root = write_project(projects_root, "yokai", '[roles]\ndirector = "stub"\n')
    direction = root / "direction"
    direction.mkdir()
    record = tmp_path / "director.json"
    monkeypatch.setattr(
        role_run,
        "run_harness",
        lambda agent, *args, **kwargs: HarnessResult(
            "fake director", 0, {**identity(agent), "outcome": "done"}
        ),
    )

    output, normalized, code = run_role(
        "director", "smoke", cwd=direction, timeout=5, record=record
    )

    assert code == 0
    assert output
    assert normalized["project"] == "yokai"
    assert normalized["profile"] == "stub"
    assert json.loads(record.read_text())["project"] == "yokai"


def test_director_unknown_project_profile_fails(projects_root):
    root = write_project(projects_root, "yokai", '[roles]\ndirector = "absent"\n')
    direction = root / "direction"
    direction.mkdir()
    with pytest.raises(AgentConfigError, match="E_UNKNOWN_PROFILE"):
        run_role("director", "smoke", cwd=direction, timeout=5)
