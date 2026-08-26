"""The `autolab` command as a serving reaches it."""

from pathlib import Path

import pytest

from agautolab import cli, project_init

TOKEN = "s3cret-gitea-token-value"


def _config():
    return project_init.GiteaConfig("http://gitea.example", TOKEN, "autodev")


def _run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_bare_command_is_self_describing(capsys):
    code, out, _ = _run(capsys)
    assert code == 0
    assert "autolab doc patterns" in out
    assert "project" in out


def test_help_lists_both_subcommands(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "doc" in out and "project" in out
    assert "init-repo" in out


def test_doc_patterns_prints_the_pattern_document_verbatim(capsys):
    code, out, _ = _run(capsys, "doc", "patterns")
    assert code == 0
    assert out == cli.DOCUMENTS["patterns"].read_text(encoding="utf-8")
    assert "# project patterns" in out


def test_unknown_doc_lists_the_known_ones_and_fails(capsys):
    code, out, err = _run(capsys, "doc", "zine")
    assert code == 1
    assert out == ""
    assert "patterns" in err


def test_repository_name_follows_project_init_naming():
    assert cli.repository_name("studyarxiv", "main") == "studyarxiv"
    assert cli.repository_name("studyarxiv", "publish") == "studyarxiv-publish"


def _fixture_workspace(monkeypatch, tmp_path, slug="studyarxiv"):
    projects_root = tmp_path / "projects"
    workspace = projects_root / slug
    workspace.mkdir(parents=True)
    monkeypatch.setattr(project_init, "PROJECTS_ROOT", projects_root)
    monkeypatch.setattr(project_init, "load_gitea_config", _config)
    monkeypatch.chdir(workspace)
    return workspace


def test_init_repo_creates_and_clones_the_standard_repository(monkeypatch, tmp_path, capsys):
    workspace = _fixture_workspace(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        project_init, "ensure_gitea_repo", lambda config, name: calls.append(("repo", name))
    )

    def clone(config, repo, destination):
        calls.append(("clone", repo, destination))
        (destination / ".git").mkdir(parents=True)

    monkeypatch.setattr(project_init, "ensure_clone", clone)

    code, out, _ = _run(capsys, "project", "init-repo", "publish")
    assert code == 0
    assert calls == [
        ("repo", "studyarxiv-publish"),
        ("clone", "studyarxiv-publish", workspace / "publish"),
    ]
    assert f"path: {workspace / 'publish'}" in out
    assert "remote: http://gitea.example/autodev/studyarxiv-publish.git" in out


def test_init_repo_main_uses_the_bare_project_name(monkeypatch, tmp_path, capsys):
    workspace = _fixture_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(project_init, "ensure_gitea_repo", lambda config, name: None)
    monkeypatch.setattr(
        project_init, "ensure_clone", lambda config, repo, dest: (dest / ".git").mkdir(parents=True)
    )
    code, out, _ = _run(capsys, "project", "init-repo", "main")
    assert code == 0
    assert "remote: http://gitea.example/autodev/studyarxiv.git" in out
    assert (workspace / "main" / ".git").is_dir()


def test_init_repo_refuses_a_folder_cloned_from_somewhere_else(monkeypatch, tmp_path, capsys):
    workspace = _fixture_workspace(monkeypatch, tmp_path)
    (workspace / "publish").mkdir()
    (workspace / "publish" / ".git").mkdir()
    monkeypatch.setattr(cli, "existing_remote", lambda path: "https://github.com/iwaag/other.git")
    monkeypatch.setattr(
        project_init, "ensure_gitea_repo", lambda *a: pytest.fail("must not touch Gitea")
    )
    code, _, err = _run(capsys, "project", "init-repo", "publish")
    assert code == 1
    assert "https://github.com/iwaag/other.git" in err
    assert "refusing" in err


def test_init_repo_leaves_the_matching_clone_alone(monkeypatch, tmp_path, capsys):
    workspace = _fixture_workspace(monkeypatch, tmp_path)
    (workspace / "publish" / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        cli, "existing_remote", lambda path: "http://gitea.example/autodev/studyarxiv-publish.git"
    )
    monkeypatch.setattr(
        project_init, "ensure_gitea_repo", lambda *a: pytest.fail("must not touch Gitea")
    )
    code, out, _ = _run(capsys, "project", "init-repo", "publish")
    assert code == 0
    assert f"path: {workspace / 'publish'}" in out


def test_init_repo_refuses_a_plain_folder_in_the_way(monkeypatch, tmp_path, capsys):
    workspace = _fixture_workspace(monkeypatch, tmp_path)
    (workspace / "publish").mkdir()
    monkeypatch.setattr(
        project_init, "ensure_gitea_repo", lambda *a: pytest.fail("must not touch Gitea")
    )
    code, _, err = _run(capsys, "project", "init-repo", "publish")
    assert code == 1
    assert "not a git clone" in err


def test_init_repo_outside_a_workspace_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(project_init, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(project_init, "load_gitea_config", _config)
    monkeypatch.chdir(tmp_path)
    code, _, err = _run(capsys, "project", "init-repo", "publish")
    assert code == 1
    assert "--project" in err


@pytest.mark.parametrize("folder", ["../escape", "a/b", "..", "."])
def test_init_repo_rejects_unsafe_folder_names(monkeypatch, tmp_path, capsys, folder):
    _fixture_workspace(monkeypatch, tmp_path)
    code, _, err = _run(capsys, "project", "init-repo", folder)
    assert code == 1
    assert "invalid folder name" in err


def test_the_gitea_token_never_reaches_the_output(monkeypatch, tmp_path, capsys):
    """Whatever the command prints — success or failure — carries no token."""
    workspace = _fixture_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(project_init, "ensure_gitea_repo", lambda config, name: None)
    monkeypatch.setattr(
        project_init, "ensure_clone", lambda config, repo, dest: (dest / ".git").mkdir(parents=True)
    )
    outputs = []
    for argv in (["project", "init-repo", "publish"], ["doc", "patterns"], ["doc", "zine"], []):
        cli.main(argv)
        captured = capsys.readouterr()
        outputs += [captured.out, captured.err]
    (workspace / "direction").mkdir()
    cli.main(["project", "init-repo", "direction"])
    captured = capsys.readouterr()
    outputs += [captured.out, captured.err]
    assert all(TOKEN not in text for text in outputs)
    assert any(text for text in outputs)
