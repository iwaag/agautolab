from pathlib import Path

import pytest

from agautolab import mission


CHANNEL = "pj-demo-project"
TOPIC = "mission-test"
WORK_KEY = f"{CHANNEL}/{TOPIC}"


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


def test_task_files_are_numeric_order_and_only_task_files(tmp_path):
    for name in ("task1.md", "task2.md", "task10.md", "new_mission.md", "notes.md", "3.md"):
        (tmp_path / name).write_text("# t")
    (tmp_path / "task4.md").mkdir()  # a directory is not a task file
    numbered = mission.task_files(tmp_path)
    assert [number for number, _ in numbered] == [1, 2, 10]
    assert mission.task_files(tmp_path / "absent") == []


def test_html_to_text_inverts_description_html_and_strips_other_tags():
    assert mission.html_to_text(mission.description_html("With A & B.\nThen verify.")) == (
        "With A & B.\nThen verify."
    )
    assert mission.html_to_text("<p>one</p><p>two <strong>bold</strong></p>") == "one\n\ntwo bold"
    assert mission.html_to_text(None) == ""


def test_compose_document_inverts_split_document():
    document = mission.compose_document("Build it", "<p>With A &amp; B.<br>Then verify.</p>")
    assert document == "# Build it\n\nWith A & B.\nThen verify.\n"
    assert mission.split_document(document) == ("Build it", "With A & B.\nThen verify.")
    assert mission.compose_document("Bare", None) == "# Bare\n"


def test_sub_works_filters_cancelled_and_sorts_by_sequence():
    issues = [
        {"id": "c2", "parent": "p", "state": "live", "sequence_id": 9},
        {"id": "c1", "parent": "p", "state": "live", "sequence_id": 2},
        {"id": "dead", "parent": "p", "state": "gone", "sequence_id": 1},
        {"id": "other", "parent": "q", "state": "live", "sequence_id": 3},
        {"id": "p", "parent": None, "state": "live", "sequence_id": 1},
    ]
    groups = {"live": "unstarted", "gone": "cancelled"}
    assert [row["id"] for row in mission.sub_works(issues, "p", groups)] == ["c1", "c2"]


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


class FakePlane:
    """Minimal Plane issue API: external keys, list, states, POST and PATCH."""

    STATES = [
        {"id": "ready-id", "name": "Ready", "group": "unstarted"},
        {"id": "started-id", "name": "In Progress", "group": "started"},
        {"id": "done-id", "name": "Done", "group": "completed"},
        {"id": "cancelled-id", "name": "Cancelled", "group": "cancelled"},
    ]

    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.creates: list[dict] = []
        self.patches: list[tuple[str, dict]] = []
        self.next_sequence = 4

    def add(self, **fields) -> dict:
        issue = {
            "id": f"issue-{len(self.issues) + 1}",
            "sequence_id": self.next_sequence,
            "parent": None,
            "state": "ready-id",
            **fields,
        }
        self.next_sequence += 1
        self.issues[issue["id"]] = issue
        return issue

    def __call__(self, method, url, *, headers, body=None, timeout=30):
        if url.endswith("/states/"):
            return 200, self.STATES
        if method == "GET" and "external_id=" in url:
            external_id = url.split("external_id=")[1].split("&")[0]
            external_id = external_id.replace("%2F", "/").replace("%23", "#").replace("%40", "@")
            issue = next(
                (row for row in self.issues.values() if row.get("external_id") == external_id),
                None,
            )
            return (200, issue) if issue else (404, {"error": "not found"})
        if method == "GET":
            return 200, {"results": list(self.issues.values()), "next_page_results": False}
        if method == "PATCH":
            issue_id = url.rstrip("/").rsplit("/", 1)[1]
            self.patches.append((issue_id, body))
            self.issues[issue_id].update(body)
            return 200, self.issues[issue_id]
        self.creates.append(body)
        return 201, self.add(**body)


@pytest.fixture
def plane(monkeypatch):
    fake = FakePlane()
    monkeypatch.setattr(mission, "load_plane_config", lambda: mission.PlaneConfig(
        "http://plane", "key", "workspace"))
    monkeypatch.setattr(
        mission, "find_plane_project", lambda config, name: {"id": "p1", "identifier": "PD"}
    )
    monkeypatch.setattr(mission, "_request_json", fake)
    return fake


def test_upsert_work_updates_an_existing_work_in_place(plane):
    work = plane.add(name="Old title", external_id=WORK_KEY)
    line = mission.upsert_work("demo", CHANNEL, TOPIC, "New title", "New body & more")
    assert line == 'updated PD-4 "New title"'
    assert plane.patches == [
        (work["id"], {"name": "New title", "description_html": "<p>New body &amp; more</p>"})
    ]
    assert plane.creates == []


def test_upsert_work_creates_the_first_work(plane):
    line = mission.upsert_work("demo", CHANNEL, TOPIC, "Build it", "Body")
    assert line == 'created PD-4 "Build it"'
    assert plane.creates[0]["external_id"] == WORK_KEY
    assert plane.creates[0]["state"] == "ready-id"


def test_cancel_sub_works_moves_only_live_children(plane):
    work = plane.add(name="Work", external_id=WORK_KEY)
    live_one = plane.add(name="c1", parent=work["id"])
    plane.add(name="dead", parent=work["id"], state="cancelled-id")
    live_two = plane.add(name="c2", parent=work["id"])
    plane.add(name="other", parent=None)

    assert mission.cancel_sub_works("demo", CHANNEL, TOPIC) == 2
    assert plane.patches == [
        (live_one["id"], {"state": "cancelled-id"}),
        (live_two["id"], {"state": "cancelled-id"}),
    ]


def test_cancel_sub_works_without_a_work_is_a_quiet_zero(plane):
    assert mission.cancel_sub_works("demo", CHANNEL, TOPIC) == 0
    assert plane.patches == []


def test_transition_work_moves_the_work_by_state_group(plane):
    work = plane.add(name="Work", external_id=WORK_KEY)
    assert mission.transition_work("demo", CHANNEL, TOPIC, "started") == "PD-4"
    assert plane.patches == [(work["id"], {"state": "started-id"})]
    with pytest.raises(mission.MissionError):
        mission.transition_work("demo", CHANNEL, "mission-unknown", "started")


def test_state_id_for_group_names_the_missing_group(plane):
    config = mission.PlaneConfig("http://plane", "key", "workspace")
    with pytest.raises(mission.MissionError) as error:
        mission.state_id_for_group(config, "p1", "no-such-group")
    assert "no-such-group" in str(error.value)


def test_register_task_files_keys_sub_works_with_the_generation(plane, tmp_path):
    work = plane.add(name="Work", external_id=WORK_KEY)
    (tmp_path / "task1.md").write_text("# First\ndo this")
    (tmp_path / "task2.md").write_text("# Second\ndo that")
    (tmp_path / "new_mission.md").write_text("# Work\nbody")

    lines = mission.register_task_files("demo", CHANNEL, TOPIC, tmp_path, rev=3)

    assert lines == ['created sub-work PD-5 "First"', 'created sub-work PD-6 "Second"']
    first, second = plane.creates
    assert first["external_id"] == f"{WORK_KEY}@3#1"
    assert second["external_id"] == f"{WORK_KEY}@3#2"
    assert first["parent"] == work["id"]
    assert first["state"] == "ready-id"


def test_register_task_files_reports_an_empty_split(plane, tmp_path):
    plane.add(name="Work", external_id=WORK_KEY)
    lines = mission.register_task_files("demo", CHANNEL, TOPIC, tmp_path, rev=1)
    assert lines == ["the coding run wrote no task files; the mission has no sub-work"]


def test_register_task_files_requires_the_work(plane, tmp_path):
    with pytest.raises(mission.MissionError):
        mission.register_task_files("demo", CHANNEL, TOPIC, tmp_path, rev=1)


def workspace_plane(monkeypatch, *, issue, issues, groups):
    monkeypatch.setattr(mission, "load_plane_config", lambda: mission.PlaneConfig(
        "http://plane", "key", "workspace"))
    monkeypatch.setattr(
        mission, "find_plane_project", lambda config, name: {"id": "p1", "identifier": "PD"}
    )
    monkeypatch.setattr(mission, "find_issue_by_external", lambda config, pid, key: issue)
    monkeypatch.setattr(mission, "list_issues", lambda config, pid: issues)
    monkeypatch.setattr(mission, "state_groups", lambda config, pid: groups)


def test_write_mission_workspace_writes_mission_and_live_tasks(tmp_path, monkeypatch):
    work = {
        "id": "w1",
        "name": "Build it",
        "description_html": "<p>With A &amp; B.</p>",
        "sequence_id": 1,
        "parent": None,
        "state": "live",
    }
    issues = [
        work,
        {"id": "c1", "parent": "w1", "state": "live", "sequence_id": 2,
         "name": "First", "description_html": "<p>do this</p>"},
        {"id": "c2", "parent": "w1", "state": "gone", "sequence_id": 3,
         "name": "Old", "description_html": "<p>cancelled</p>"},
        {"id": "c3", "parent": "w1", "state": "live", "sequence_id": 4,
         "name": "Second", "description_html": "<p>do that</p>"},
    ]
    # The external lookup may return a thin object; the list row must win.
    workspace_plane(monkeypatch, issue={"id": "w1"}, issues=issues,
                    groups={"live": "unstarted", "gone": "cancelled"})

    assert mission.write_mission_workspace(tmp_path, "demo", "pj-demo", "mission-x") is True
    assert (tmp_path / "mission.md").read_text() == "# Build it\n\nWith A & B.\n"
    assert (tmp_path / "task1.md").read_text() == "# First\n\ndo this\n"
    assert (tmp_path / "task2.md").read_text() == "# Second\n\ndo that\n"
    assert not (tmp_path / "task3.md").exists()


def test_write_mission_workspace_without_a_work_writes_nothing(tmp_path, monkeypatch):
    workspace_plane(monkeypatch, issue=None, issues=[], groups={})
    assert mission.write_mission_workspace(tmp_path, "demo", "pj-demo", "mission-x") is False
    assert list(tmp_path.iterdir()) == []
