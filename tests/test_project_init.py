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
    monkeypatch.setattr(
        project_init,
        "ensure_gitignore",
        lambda config, path: calls.append(("gitignore", path.relative_to(tmp_path))),
    )

    assert project_init.init_project("demo-project") == "success"
    # Every clone gets the same treatment: a `.gitignore` and nothing else.
    assert calls == [
        ("plane", "demo-project"),
        ("repo", "demo-project"),
        ("clone", "demo-project", Path("demo-project/main")),
        ("gitignore", Path("demo-project/main")),
        ("repo", "demo-project-direction"),
        ("clone", "demo-project-direction", Path("demo-project/direction")),
        ("gitignore", Path("demo-project/direction")),
        ("repo", "demo-project-devlog"),
        ("clone", "demo-project-devlog", Path("demo-project/devlog")),
        ("gitignore", Path("demo-project/devlog")),
    ]


def wire_init(monkeypatch, tmp_path, calls):
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

    def clone(config, repo, path):
        calls.append(("clone", repo, path.relative_to(tmp_path)))
        (path / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(project_init, "ensure_clone", clone)
    monkeypatch.setattr(
        project_init,
        "ensure_gitignore",
        lambda config, path: calls.append(("gitignore", path.relative_to(tmp_path))),
    )


def test_init_project_main_only_clones_main_and_makes_devlog_a_plain_folder(monkeypatch, tmp_path):
    calls = []
    wire_init(monkeypatch, tmp_path, calls)

    assert project_init.init_project("rtnotes", main_only=True) == "success"
    assert calls == [
        ("plane", "rtnotes"),
        ("repo", "rtnotes"),
        ("clone", "rtnotes", Path("rtnotes/main")),
        ("gitignore", Path("rtnotes/main")),
    ]
    assert (tmp_path / "rtnotes" / "devlog").is_dir()
    assert not (tmp_path / "rtnotes" / "devlog" / ".git").exists()
    assert not (tmp_path / "rtnotes" / "direction").exists()
    assert project_init.is_main_only(tmp_path / "rtnotes")

    # A runtime re-run passes no layout and must read main-only off the disk.
    calls.clear()
    assert project_init.init_project("rtnotes") == "success"
    assert [call[0] for call in calls] == ["plane", "repo", "clone", "gitignore"]
    assert not (tmp_path / "rtnotes" / "direction").exists()


def test_init_project_main_only_leaves_an_existing_full_project_alone(monkeypatch, tmp_path):
    calls = []
    wire_init(monkeypatch, tmp_path, calls)
    project_init.init_project("demo-project")
    assert (tmp_path / "demo-project" / "devlog" / ".git").is_dir()

    calls.clear()
    project_init.init_project("demo-project", main_only=True)
    assert [call[1] for call in calls if call[0] == "repo"] == ["demo-project"]
    assert (tmp_path / "demo-project" / "direction" / ".git").is_dir()
    assert (tmp_path / "demo-project" / "devlog" / ".git").is_dir()
    assert not project_init.is_main_only(tmp_path / "demo-project")


def test_init_project_refuses_to_turn_a_local_devlog_into_a_clone(monkeypatch, tmp_path):
    calls = []
    wire_init(monkeypatch, tmp_path, calls)
    project_init.init_project("rtnotes", main_only=True)

    with pytest.raises(project_init.ProjectInitError, match="main-only"):
        project_init.init_project("rtnotes", main_only=False)
    assert not (tmp_path / "rtnotes" / "devlog" / ".git").exists()


def test_ensure_gitignore_seeds_commits_and_is_idempotent(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        project_init, "_git", lambda config, *args, cwd=None: commands.append(args) or ""
    )
    gitea = project_init.GiteaConfig("http://gitea", "token", "autodev")

    assert project_init.ensure_gitignore(gitea, tmp_path) is True
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".local/\n"
    assert [args[0] for args in commands] == ["add", "-c", "push"]
    assert commands[-1] == ("push", "origin", "HEAD:main")

    commands.clear()
    assert project_init.ensure_gitignore(gitea, tmp_path) is False
    assert commands == []


def test_ensure_gitignore_appends_to_an_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(project_init, "_git", lambda config, *args, cwd=None: "")
    (tmp_path / ".gitignore").write_text("dist/\n", encoding="utf-8")

    assert project_init.ensure_gitignore(
        project_init.GiteaConfig("http://gitea", "token", "autodev"), tmp_path
    ) is True
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "dist/\n.local/\n"


def test_auto_description_carries_marker_and_slug():
    assert project_init.auto_description("whack-a-mole") == "[AUTO] autolab project: whack-a-mole"
    assert project_init.project_slug({"description": "[AUTO] autolab project: whack-a-mole"}) == (
        "whack-a-mole"
    )
    # Case-insensitive marker, and the prettified name as the fallback source.
    assert project_init.project_slug(
        {"description": "[auto]", "name": "Whack A Mole"}
    ) == "whack-a-mole"
    assert project_init.project_slug({"description": "hand made", "name": "ProjectA"}) is None


@pytest.mark.parametrize("name", ["x", "Bad Name", "../escape", "-leading"])
def test_init_project_rejects_unsafe_names(name):
    with pytest.raises(project_init.ProjectInitError):
        project_init.init_project(name)


def test_commit_all_and_push_commits_a_dirty_workspace(monkeypatch, tmp_path):
    commands = []

    def fake_git(config, *args, cwd=None):
        commands.append(args)
        return " M notes.md\n?? new.md\n" if args[0] == "status" else ""

    monkeypatch.setattr(project_init, "_git", fake_git)
    gitea = project_init.GiteaConfig("http://gitea", "token", "autodev")

    assert project_init.commit_all_and_push(gitea, tmp_path, "record notes") is True
    assert commands[0] == ("status", "--porcelain")
    assert ("add", "-A") in commands
    assert commands[-1] == ("push", "origin", "HEAD:main")


def test_push_main_repository_publishes_what_the_run_committed(monkeypatch, tmp_path):
    """It commits nothing: what enters the history was the agent's decision,
    and only the publishing was missing."""
    commands = []

    def fake_git(config, *args, cwd=None):
        commands.append(args)
        return "aaa\nbbb\n" if args[0] == "rev-list" else ""

    monkeypatch.setattr(project_init, "_git", fake_git)
    gitea = project_init.GiteaConfig("http://gitea", "token", "autodev")

    assert project_init.push_main_repository(gitea, tmp_path) == 2
    assert commands[0] == ("fetch", "origin", "main")
    assert commands[1] == ("rev-list", "origin/main..HEAD")
    assert commands[-1] == ("push", "origin", "HEAD:main")
    assert not any(args[0] in {"add", "commit"} for args in commands)


def test_push_main_repository_pushes_nothing_when_the_clone_is_level(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        project_init, "_git", lambda config, *args, cwd=None: commands.append(args) or ""
    )
    gitea = project_init.GiteaConfig("http://gitea", "token", "autodev")

    assert project_init.push_main_repository(gitea, tmp_path) == 0
    assert [args[0] for args in commands] == ["fetch", "rev-list"]


def test_commit_all_and_push_leaves_a_clean_workspace_alone(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        project_init, "_git", lambda config, *args, cwd=None: commands.append(args) or ""
    )
    gitea = project_init.GiteaConfig("http://gitea", "token", "autodev")

    assert project_init.commit_all_and_push(gitea, tmp_path, "record notes") is False
    assert [args[0] for args in commands] == ["status"]
