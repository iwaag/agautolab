import os
from pathlib import Path

import pytest

from agautolab import mission


def write_log(root: Path, channel: str, version: int, content: str) -> Path:
    path = root / ".local/topics" / channel / "mission-test" / str(version) / "chatlog.txt"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    return path


def test_current_project_uses_latest_dumped_chat(tmp_path):
    older = write_log(tmp_path, "pj-older", 1, "old")
    newer = write_log(tmp_path, "pj-newer", 1, "new")
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))
    assert mission.current_project(tmp_path) == "newer"


def test_current_project_allows_explicit_smoke_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOLAB_PROJECT", "demo-project")
    assert mission.current_project(tmp_path) == "demo-project"


def test_new_mission_selects_ready_state_and_escapes_description(monkeypatch):
    config = mission.PlaneConfig("http://plane", "key", "workspace")
    calls = []
    monkeypatch.setenv("AUTOLAB_PROJECT", "demo-project")
    monkeypatch.setattr(mission, "load_plane_config", lambda: config)
    monkeypatch.setattr(mission, "find_plane_project", lambda config, name: {"id": "project-id"})
    monkeypatch.setattr(mission, "starting_state_id", lambda config, project: "ready-id")

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return 201, {"id": "issue-id"}

    monkeypatch.setattr(mission, "_request_json", request)
    assert mission.new_mission("Ship <thing>", "Use A & B\nVerify it.") == "success"
    body = calls[0][2]["body"]
    assert body == {
        "name": "Ship <thing>",
        "description_html": "<p>Use A &amp; B<br>Verify it.</p>",
        "state": "ready-id",
    }


@pytest.mark.parametrize(
    "states,expected",
    [
        ([{"id": "ready", "name": "Ready", "group": "unstarted"}], "ready"),
        ([{"id": "todo", "name": "Todo", "group": "unstarted"}], "todo"),
        ([{"id": "queued", "name": "Queued", "group": "unstarted"}], "queued"),
        ([{"id": "backlog", "name": "Backlog", "group": "backlog"}], "backlog"),
    ],
)
def test_starting_state_uses_live_vocabulary(monkeypatch, states, expected):
    monkeypatch.setattr(mission, "_request_json", lambda *a, **k: (200, states))
    assert mission.starting_state_id(
        mission.PlaneConfig("http://plane", "key", "workspace"), "project-id"
    ) == expected


@pytest.mark.parametrize("name,description", [("", "description"), ("name", "  ")])
def test_create_mission_rejects_empty_arguments(name, description):
    with pytest.raises(mission.MissionError):
        mission.create_mission("demo-project", name, description)
