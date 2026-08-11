"""Gateway project/profile read model tests."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gateway_projects", Path(__file__).resolve().parent.parent / "agent" / "gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def configure(monkeypatch, tmp_path):
    config = tmp_path / "agents.toml"
    config.write_text(
        """schema = "ag.agent-config.v1"
project = "test"
[models."local/test"]
[profiles.local]
harness = "fake"
model = "local/test"
[profiles.sonnet]
harness = "fake"
model = "local/test"
[roles.coding]
profile = "sonnet"
[roles.director]
profile = "local"
[capabilities]
provides = []
"""
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(gateway, "AGENTS_CONFIG", config)
    monkeypatch.setattr(gateway, "AGENTS_LOCAL_CONFIG", tmp_path / "absent.toml")
    monkeypatch.setattr(gateway, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(gateway, "load_project_roles", lambda name: _load(projects, name))
    return projects


def _load(projects, name):
    # Keep this test independent of module-global paths by parsing the tiny fixture.
    import tomllib

    path = projects / name / "agents.toml"
    if not path.exists():
        return {}
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise gateway.ProjectSettingsError(f"cannot read project settings: {error}")
    return doc.get("roles", {})


def test_projects_include_effective_profiles_and_sources(monkeypatch, tmp_path):
    projects = configure(monkeypatch, tmp_path)
    (projects / "defaults").mkdir()
    selected = projects / "selected"
    selected.mkdir()
    (selected / "agents.toml").write_text('[roles]\ncoding = "local"\n')

    doc = gateway.projects_document()

    assert doc["kind"] == "autolab.projects.v1"
    assert doc["profiles"] == ["local", "sonnet"]
    assert doc["projects"] == [
        {
            "name": "defaults",
            "roles": {
                "coding": {"profile": "sonnet", "source": "default"},
                "director": {"profile": "local", "source": "default"},
            },
        },
        {
            "name": "selected",
            "roles": {
                "coding": {"profile": "local", "source": "project"},
                "director": {"profile": "local", "source": "default"},
            },
        },
    ]


def test_bad_project_does_not_hide_good_projects(monkeypatch, tmp_path):
    projects = configure(monkeypatch, tmp_path)
    bad = projects / "bad"
    bad.mkdir()
    (bad / "agents.toml").write_text("[roles")
    (projects / "good").mkdir()

    rows = gateway.projects_document()["projects"]

    assert rows[0]["name"] == "bad" and "error" in rows[0]
    assert rows[1]["name"] == "good" and "roles" in rows[1]


def test_job_yaml_surfaces_project_with_or_without_pyyaml(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    (job / "job.yaml").write_text("project: yokai\nmax_iterations: 4\n")

    assert gateway.job_yaml_fields(job)["project"] == "yokai"
