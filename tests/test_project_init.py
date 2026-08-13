from pathlib import Path

import pytest

from agautolab import project_init


def test_plane_identifier_is_readable_and_bounded():
    assert project_init.plane_identifier("whack-a-mole") == "WAM"
    assert project_init.plane_identifier("whack-a-mole-2") == "WAM2"
    assert len(project_init.plane_identifier("one-two-three-four-five-six-seven-eight-nine")) <= 12
    assert project_init.plane_project_name("whack-a-mole-2") == "Whack A Mole 2"


def test_ensure_plane_project_reuses_normalized_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        project_init,
        "_request_json",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)) or (200, {"results": [{"id": "p1", "name": "Whack A Mole", "identifier": "WAM"}]})
        ),
    )
    result = project_init.ensure_plane_project(
        project_init.PlaneConfig("http://plane", "key", "workspace"), "whack-a-mole"
    )
    assert result["id"] == "p1"
    assert len(calls) == 1


def test_ensure_plane_project_adds_suffix_on_identifier_collision(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, kwargs.get("body")))
        if method == "GET":
            return 200, {"results": [{"name": "Wild Active Meadow", "identifier": "WAM"}]}
        return 201, {"id": "p2", **kwargs["body"]}

    monkeypatch.setattr(project_init, "_request_json", request)
    result = project_init.ensure_plane_project(
        project_init.PlaneConfig("http://plane", "key", "workspace"), "whack-a-mole"
    )
    assert result["identifier"] == "WAM2"


def test_init_project_runs_every_idempotent_step_in_order(monkeypatch, tmp_path):
    calls = []
    plane = project_init.PlaneConfig("http://plane", "key", "workspace")
    gitea = project_init.GiteaConfig("http://gitea", "token", "autodev")
    monkeypatch.setattr(project_init, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(project_init, "load_plane_config", lambda: plane)
    monkeypatch.setattr(project_init, "load_gitea_config", lambda: gitea)
    monkeypatch.setattr(
        project_init, "ensure_plane_project", lambda config, name: calls.append(("plane", name))
    )
    monkeypatch.setattr(
        project_init, "ensure_gitea_repo", lambda config, name: calls.append(("repo", name))
    )
    monkeypatch.setattr(
        project_init,
        "ensure_clone",
        lambda config, repo, path: calls.append(("clone", repo, path.relative_to(tmp_path))),
    )

    assert project_init.init_project("demo-project") == "success"
    assert calls == [
        ("plane", "demo-project"),
        ("repo", "demo-project"),
        ("repo", "demo-project-direction"),
        ("clone", "demo-project", Path("demo-project/main")),
        ("clone", "demo-project-direction", Path("demo-project/direction")),
    ]


@pytest.mark.parametrize("name", ["x", "Bad Name", "../escape", "-leading"])
def test_init_project_rejects_unsafe_names(name):
    with pytest.raises(project_init.ProjectInitError):
        project_init.init_project(name)
