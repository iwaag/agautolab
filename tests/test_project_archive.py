import json

import pytest

from agautolab import project_archive
from agautolab.project_archive import ABSENT, ALREADY, ARCHIVED

PLANE = project_archive.PlaneConfig("http://plane", "key", "workspace")
GITEA = project_archive.GiteaConfig("http://gitea", "token", "autodev")


class FakeClient:
    """A ZulipClient stand-in with one archivable channel."""

    def __init__(self, channels, error=None, folders=()):
        self._channels = channels
        self._error = error
        self._folders = list(folders)
        self.archived = []
        self.archived_folders = []

    def channels(self):
        return self._channels

    def archive_channel(self, stream_id):
        if self._error is not None:
            raise self._error
        self.archived.append(stream_id)
        return {"result": "success"}

    def channel_folder_by_name(self, name):
        return next((f for f in self._folders if f["name"] == name), None)

    def archive_channel_folder(self, folder_id):
        if self._error is not None:
            raise self._error
        self.archived_folders.append(folder_id)
        return {"result": "success"}


def test_archive_plane_project_archives_once_and_recognizes_the_second_run(monkeypatch):
    rows = [{"id": "p1", "name": "Whack A Mole"}]
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return 200, {"results": rows}
        rows[0]["archived_at"] = "2026-08-17T00:00:00Z"
        return 204, {}

    monkeypatch.setattr(project_archive, "_request_json", request)
    assert project_archive.archive_plane_project(PLANE, "whack-a-mole") == ARCHIVED
    assert calls[-1] == ("POST", "http://plane/api/v1/workspaces/workspace/projects/p1/archive/")
    assert project_archive.archive_plane_project(PLANE, "whack-a-mole") == ALREADY


def test_archive_plane_project_reports_an_unknown_project_as_absent(monkeypatch):
    monkeypatch.setattr(project_archive, "_request_json", lambda *a, **k: (200, {"results": []}))
    assert project_archive.archive_plane_project(PLANE, "whack-a-mole") == ABSENT


def test_archive_gitea_repo_patches_only_a_live_repository(monkeypatch):
    state = {"archived": False}
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, kwargs.get("body")))
        if method == "GET":
            return 200, dict(state)
        state["archived"] = True
        return 200, dict(state)

    monkeypatch.setattr(project_archive, "_request_json", request)
    assert project_archive.archive_gitea_repo(GITEA, "spike") == ARCHIVED
    assert calls[-1] == ("PATCH", {"archived": True})
    assert project_archive.archive_gitea_repo(GITEA, "spike") == ALREADY
    assert calls[-1] == ("GET", None)  # nothing written on the second run


def test_archive_gitea_repo_reports_a_missing_repository_as_absent(monkeypatch):
    monkeypatch.setattr(project_archive, "_request_json", lambda *a, **k: (404, {}))
    assert project_archive.archive_gitea_repo(GITEA, "spike") == ABSENT


def test_archive_zulip_channel_uses_the_pj_prefixed_name():
    client = FakeClient([{"name": "pj-spike", "stream_id": 9}, {"name": "general", "stream_id": 3}])
    assert project_archive.archive_zulip_channel(client, "spike") == ARCHIVED
    assert client.archived == [9]
    # An archived channel leaves the listing, so the second run finds nothing.
    assert project_archive.archive_zulip_channel(FakeClient([]), "spike") == ABSENT


def test_archive_zulip_channel_raises_when_the_bot_may_not_administer_it():
    client = FakeClient(
        [{"name": "pj-spike", "stream_id": 9}],
        error=project_archive.ZulipError("DELETE streams/9 -> HTTP 400: Must be an organization administrator"),
    )
    with pytest.raises(project_archive.ProjectArchiveError, match="pj-spike"):
        project_archive.archive_zulip_channel(client, "spike")


def test_archive_zulip_folder_retires_an_emptied_folder():
    folders = [{"id": 4, "name": "pj-spike"}]
    # The project channel is already archived and gone from the listing;
    # only unrelated channels remain.
    client = FakeClient([{"name": "general", "stream_id": 3, "folder_id": None}], folders=folders)
    assert project_archive.archive_zulip_folder(client, "spike") == ARCHIVED
    assert client.archived_folders == [4]
    assert project_archive.archive_zulip_folder(FakeClient([]), "spike") == ABSENT


def test_archive_zulip_folder_keeps_a_folder_that_still_holds_a_work_channel():
    folders = [{"id": 4, "name": "pj-spike"}]
    client = FakeClient([{"name": "work-sp-1", "stream_id": 12, "folder_id": 4}], folders=folders)
    assert project_archive.archive_zulip_folder(client, "spike") == project_archive.KEPT
    assert client.archived_folders == []


def test_archive_zulip_folder_raises_when_the_bot_may_not_archive_it():
    client = FakeClient(
        [], folders=[{"id": 4, "name": "pj-spike"}],
        error=project_archive.ZulipError("PATCH channel_folders/4 -> HTTP 400: Must be an organization administrator"),
    )
    with pytest.raises(project_archive.ProjectArchiveError, match="pj-spike"):
        project_archive.archive_zulip_folder(client, "spike")


def test_archive_workspace_moves_the_clone_set_aside(tmp_path):
    root, archive = tmp_path / "projects", tmp_path / "projects-archived"
    (root / "spike" / "main").mkdir(parents=True)
    (root / "spike" / "main" / "kept.txt").write_text("work in progress", encoding="utf-8")

    assert project_archive.archive_workspace("spike", root=root, archive=archive) == ARCHIVED
    assert not (root / "spike").exists()
    assert (archive / "spike" / "main" / "kept.txt").read_text(encoding="utf-8") == (
        "work in progress"
    )
    assert project_archive.archive_workspace("spike", root=root, archive=archive) == ALREADY
    assert project_archive.archive_workspace("other", root=root, archive=archive) == ABSENT


def test_archive_workspace_refuses_to_merge_two_copies(tmp_path):
    root, archive = tmp_path / "projects", tmp_path / "projects-archived"
    (root / "spike").mkdir(parents=True)
    (archive / "spike").mkdir(parents=True)
    with pytest.raises(project_archive.ProjectArchiveError, match="resolve by hand"):
        project_archive.archive_workspace("spike", root=root, archive=archive)


def test_archive_project_covers_all_four_surfaces(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(project_archive, "load_plane_config", lambda: PLANE)
    monkeypatch.setattr(project_archive, "load_gitea_config", lambda: GITEA)
    monkeypatch.setattr(
        project_archive.ZulipClient, "from_env", classmethod(lambda cls, path: FakeClient([]))
    )
    monkeypatch.setattr(
        project_archive,
        "archive_plane_project",
        lambda config, name: calls.append(("plane", name)) or ARCHIVED,
    )
    monkeypatch.setattr(
        project_archive,
        "archive_gitea_repo",
        lambda config, name: calls.append(("repo", name)) or ARCHIVED,
    )
    monkeypatch.setattr(
        project_archive,
        "archive_zulip_channel",
        lambda client, name: calls.append(("zulip", name)) or ARCHIVED,
    )
    monkeypatch.setattr(
        project_archive,
        "archive_zulip_folder",
        lambda client, name: calls.append(("zulip-folder", name)) or ARCHIVED,
    )
    monkeypatch.setattr(
        project_archive,
        "archive_workspace",
        lambda name: calls.append(("workspace", name)) or ARCHIVED,
    )

    report = project_archive.archive_project("demo-project")
    assert report == {
        "project": "demo-project",
        "plane": ARCHIVED,
        "gitea": {
            "demo-project": ARCHIVED,
            "demo-project-direction": ARCHIVED,
            "demo-project-devlog": ARCHIVED,
        },
        "zulip": ARCHIVED,
        "zulip_folder": ARCHIVED,
        "workspace": ARCHIVED,
    }
    assert calls == [
        ("plane", "demo-project"),
        ("repo", "demo-project"),
        ("repo", "demo-project-direction"),
        ("repo", "demo-project-devlog"),
        ("zulip", "demo-project"),
        ("zulip-folder", "demo-project"),
        ("workspace", "demo-project"),
    ]


@pytest.mark.parametrize("name", ["x", "Bad Name", "../escape", "-leading"])
def test_archive_project_rejects_unsafe_names(name):
    with pytest.raises(project_archive.ProjectArchiveError):
        project_archive.archive_project(name)


def test_main_reports_one_line_per_project_and_survives_one_failure(monkeypatch, capsys):
    def archive(project, *, zulip_env=None):
        if project == "broken":
            raise project_archive.ProjectArchiveError("no such channel")
        return {"project": project, "plane": ARCHIVED}

    monkeypatch.setattr(project_archive, "archive_project", archive)
    assert project_archive.main(["spike", "broken", "phase2local"]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["project"] for line in lines] == ["spike", "broken", "phase2local"]
    assert lines[1]["error"] == "no such channel"
    assert project_archive.main([]) == 2
