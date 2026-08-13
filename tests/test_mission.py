import os
from pathlib import Path

import pytest

from agautolab import mission


def write_dump(root: Path, channel: str, version: int, content: str = "chat") -> Path:
    path = root / ".local/topics" / channel / "mission-test" / str(version)
    path.mkdir(parents=True)
    (path / "chatlog.txt").write_text(content)
    return path


def test_latest_dump_directory_uses_newest_chat(tmp_path):
    older = write_dump(tmp_path, "pj-older", 1)
    newer = write_dump(tmp_path, "pj-newer", 1)
    os.utime(older / "chatlog.txt", ns=(1, 1))
    os.utime(newer / "chatlog.txt", ns=(2, 2))
    assert mission.latest_dump_directory(tmp_path) == newer
    assert mission.current_project(newer) == "newer"
    assert mission.topic_key(newer) == ("pj-newer", "mission-test")


def test_current_project_allows_explicit_smoke_override(monkeypatch, tmp_path):
    dump = write_dump(tmp_path, "pj-newer", 1)
    monkeypatch.setenv("AUTOLAB_PROJECT", "demo-project")
    assert mission.current_project(dump) == "demo-project"


def test_topic_key_rejects_a_directory_outside_a_project_channel(tmp_path):
    dump = tmp_path / "elsewhere" / "mission-test" / "1"
    dump.mkdir(parents=True)
    with pytest.raises(mission.MissionError):
        mission.topic_key(dump)


@pytest.mark.parametrize(
    "text,title,description",
    [
        ("# Ship it\n\nUse A & B.\nVerify.", "Ship it", "Use A & B.\nVerify."),
        ("\n\n## Second level\nbody", "Second level", "body"),
        ("no heading here\nrest of it", "no heading here", "rest of it"),
        ("# Only a title\n", "Only a title", ""),
    ],
)
def test_split_document(text, title, description):
    assert mission.split_document(text) == (title, description)


def test_split_document_truncates_and_rejects_empty():
    title, _ = mission.split_document("# " + "x" * 400)
    assert len(title) == mission.TITLE_LIMIT
    with pytest.raises(mission.MissionError):
        mission.split_document("\n  \n")


def test_task_files_are_numeric_order_not_string_order(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for name in ("1.md", "2.md", "10.md", "notes.md", "3.txt"):
        (tasks / name).write_text("# t")
    (tasks / "sub").mkdir()
    numbered, ignored = mission.task_files(tasks)
    assert [number for number, _ in numbered] == [1, 2, 10]
    assert ignored == ["3.txt", "notes.md", "sub"]
    assert mission.task_files(tmp_path / "absent") == ([], [])


class FakePlane:
    """Minimal Plane issue API: external keys, 409 on a duplicate create."""

    def __init__(self, *, conflict_first=False):
        self.issues = {}
        self.creates = []
        self.conflict_first = conflict_first
        self.next_sequence = 4

    def __call__(self, method, url, *, headers, body=None, timeout=30):
        if url.endswith("/states/"):
            return 200, [{"id": "ready-id", "name": "Ready", "group": "unstarted"}]
        if method == "GET":
            external_id = url.split("external_id=")[1].split("&")[0].replace("%2F", "/")
            external_id = external_id.replace("%23", "#")
            issue = self.issues.get(external_id)
            return (200, issue) if issue else (404, {"error": "not found"})
        self.creates.append(body)
        if self.conflict_first:
            self.conflict_first = False
            return 409, {"error": "already exists", "id": "other-id"}
        issue = {
            "id": f"issue-{len(self.issues) + 1}",
            "sequence_id": self.next_sequence,
            **body,
        }
        self.next_sequence += 1
        self.issues[body["external_id"]] = issue
        return 201, issue


def prepare(tmp_path, monkeypatch, *, tasks=("# First\ndo this", "# Second\ndo that")):
    dump = write_dump(tmp_path, "pj-demo-project", 3)
    (dump / "mission.md").write_text("# Build it\n\nWith A & B.\nThen verify.")
    if tasks:
        (dump / "tasks").mkdir()
        for index, text in enumerate(tasks, start=1):
            (dump / "tasks" / f"{index}.md").write_text(text)
    monkeypatch.setattr(mission, "load_plane_config", lambda: mission.PlaneConfig(
        "http://plane", "key", "workspace"))
    monkeypatch.setattr(
        mission, "find_plane_project", lambda config, name: {"id": "p1", "identifier": "PD"}
    )
    return dump


def test_register_dump_creates_the_mission_and_its_sub_work(tmp_path, monkeypatch):
    dump = prepare(tmp_path, monkeypatch)
    plane = FakePlane()
    monkeypatch.setattr(mission, "_request_json", plane)

    output = mission.register_dump(dump)

    assert output.splitlines() == [
        'created PD-4 "Build it"',
        'created sub-work PD-5 "First"',
        'created sub-work PD-6 "Second"',
    ]
    mission_body, first, second = plane.creates
    assert mission_body["external_source"] == "agautolab"
    assert mission_body["external_id"] == "pj-demo-project/mission-test"
    assert mission_body["description_html"] == "<p>With A &amp; B.<br>Then verify.</p>"
    assert mission_body["state"] == "ready-id"
    assert "parent" not in mission_body
    assert first["external_id"] == "pj-demo-project/mission-test#1"
    assert first["parent"] == "issue-1"
    assert second["external_id"] == "pj-demo-project/mission-test#2"


def test_register_dump_is_idempotent_across_dump_versions(tmp_path, monkeypatch):
    dump = prepare(tmp_path, monkeypatch)
    plane = FakePlane()
    monkeypatch.setattr(mission, "_request_json", plane)
    mission.register_dump(dump)

    # The same topic fires again: a new version directory, same files.
    later = write_dump(tmp_path, "pj-demo-project", 4)
    (later / "mission.md").write_text(dump.joinpath("mission.md").read_text())
    (later / "tasks").mkdir()
    for path in sorted((dump / "tasks").iterdir()):
        (later / "tasks" / path.name).write_text(path.read_text())

    output = mission.register_dump(later)

    assert len(plane.creates) == 3
    assert output.splitlines() == [
        'already registered PD-4 "Build it"',
        'already registered sub-work PD-5 "First"',
        'already registered sub-work PD-6 "Second"',
    ]


def test_register_dump_absorbs_a_create_conflict(tmp_path, monkeypatch):
    dump = prepare(tmp_path, monkeypatch, tasks=())
    plane = FakePlane(conflict_first=True)
    plane.issues["pj-demo-project/mission-test"] = {"id": "raced", "sequence_id": 9}
    monkeypatch.setattr(mission, "_request_json", plane)
    # The lookup finds the racer before the POST here; drive the 409 path by
    # hiding it from the first GET only.
    lookups = []

    original = mission.find_issue_by_external

    def once(config, project_id, external_id):
        lookups.append(external_id)
        return None if len(lookups) == 1 else original(config, project_id, external_id)

    monkeypatch.setattr(mission, "find_issue_by_external", once)
    assert mission.register_dump(dump).splitlines()[0] == 'already registered PD-9 "Build it"'


def test_register_dump_reports_a_missing_mission_as_normal(tmp_path, monkeypatch):
    dump = write_dump(tmp_path, "pj-demo-project", 1)
    monkeypatch.setattr(mission, "load_plane_config", lambda: pytest.fail("must not call Plane"))
    assert mission.register_dump(dump) == "no mission"


def test_register_dump_accepts_a_mission_without_tasks(tmp_path, monkeypatch):
    dump = prepare(tmp_path, monkeypatch, tasks=())
    monkeypatch.setattr(mission, "_request_json", FakePlane())
    output = mission.register_dump(dump)
    assert output.splitlines() == [
        'created PD-4 "Build it"',
        "no tasks/ directory content; the mission has no sub-work",
    ]


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
