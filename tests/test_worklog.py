"""The work record, read and written where the work happens."""

import pytest

from agautolab import worklog
from agautolab.worklog import WorklogError

from realm import BOT_ID, HUMAN_ID, Realm

CHANNEL = "pj-demo"
TOPIC = "workplan-ship-it"
PLAN = "# Ship it\n\nTwo tasks.\n"


@pytest.fixture
def realm():
    room = Realm()
    room.add_channel(CHANNEL)
    room.post(CHANNEL, TOPIC, "Please ship it", sender_id=HUMAN_ID)
    return room


def plan(realm, plan_text=PLAN):
    line, mission = worklog.record_plan(realm, CHANNEL, TOPIC, "demo", plan_text, BOT_ID)
    return line, mission


def write_tasks(tmp_path, *documents):
    for number, text in enumerate(documents, start=1):
        (tmp_path / f"task{number}.md").write_text(text, encoding="utf-8")
    return tmp_path


# --- documents -------------------------------------------------------------


def test_task_files_are_numeric_order_and_only_task_files(tmp_path):
    for name in ("task1.md", "task2.md", "task10.md", "plan.md", "notes.md", "3.md"):
        (tmp_path / name).write_text("# t")
    (tmp_path / "task4.md").mkdir()  # a directory is not a task file
    assert [n for n, _ in worklog.task_files(tmp_path)] == [1, 2, 10]
    assert worklog.task_files(tmp_path / "absent") == []


def test_a_plan_with_no_title_is_not_a_plan(realm):
    with pytest.raises(WorklogError):
        worklog.record_plan(realm, CHANNEL, TOPIC, "demo", "\n  \n", BOT_ID)


# --- identity --------------------------------------------------------------


def test_a_mission_is_its_own_note_and_its_names_come_from_that(realm):
    line, mission = plan(realm)
    assert mission.slug == "demo"
    assert mission.label == f"m{mission.mission_id}"
    assert mission.work_channel == f"work-m{mission.mission_id}"
    assert mission.title == "Ship it"
    assert line.startswith("recorded the plan")
    # The anchor is a real message in the conversation, and it is a selfnote,
    # so it never counts as somebody speaking.
    assert realm.messages[mission.mission_id]["content"].startswith("[selfnote][mission]")


def test_re_planning_keeps_the_mission_it_already_is(realm):
    _, first = plan(realm)
    line, second = plan(realm, "# Ship it better\n\nStill two tasks.\n")
    assert second.mission_id == first.mission_id
    assert line.startswith("updated the plan")
    assert second.title == "Ship it better"
    assert worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID).plan.startswith("# Ship it better")


def test_an_unchanged_plan_is_not_re_posted(realm):
    plan(realm)
    before = len(realm.order)
    line, _ = plan(realm)
    assert "unchanged" in line
    assert len(realm.order) == before


def test_a_topic_with_no_plan_holds_no_mission(realm):
    assert worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID) is None


# --- following an anchor ---------------------------------------------------


def test_an_anchor_follows_its_conversation_through_a_rename(realm):
    _, mission = plan(realm)
    realm.move_topic(CHANNEL, TOPIC, "✔ workplan-ship-it")
    found = worklog.mission_at(realm, mission.mission_id, BOT_ID)
    assert found is not None
    assert found.mission_id == mission.mission_id
    # The bare name is what callers keep; the ✔ is Zulip's, not the record's.
    assert found.topic == TOPIC


def test_a_deleted_anchor_is_absent_not_the_work_that_took_its_name(realm):
    """The point of an id: a new mission in a topic of the same name is a
    different mission, and a deleted one does not become it."""
    _, mission = plan(realm)
    realm.delete(mission.mission_id)
    assert worklog.mission_at(realm, mission.mission_id, BOT_ID) is None


# --- the document a conversation currently holds ---------------------------


def test_the_document_is_the_post_the_note_names_not_the_newest_one(realm):
    _, mission = plan(realm)
    realm.post(CHANNEL, TOPIC, "looks good, but rename the second task",
               sender_id=HUMAN_ID)
    assert worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID).plan.strip() == PLAN.strip()


# --- tasks -----------------------------------------------------------------


def open_two_tasks(realm, tmp_path):
    _, mission = plan(realm)
    changes = worklog.plan_changes({}, write_tasks(tmp_path, "# One\n\nfirst", "# Two\n\nsecond"))
    channel = mission.work_channel
    realm.add_channel(channel)
    tasks = {}
    for change in changes:
        topic = worklog.run_topic_name(mission.mission_id, change.serial)
        task = worklog.anchor_task(
            realm, channel, topic, mission.mission_id, change.serial, BOT_ID
        )
        worklog.post_document(realm, channel, topic, change.document)
        tasks[change.serial] = task
    return mission, tasks


def test_planning_creates_one_task_per_file(realm, tmp_path):
    mission, _ = open_two_tasks(realm, tmp_path)
    found = worklog.mission_tasks(realm, mission, BOT_ID)
    assert sorted(found) == [1, 2]
    assert found[1].title == "One"
    assert found[2].document.strip() == "# Two\n\nsecond"
    assert found[1].state == worklog.TASK_OPEN


def test_a_workrun_topic_nobody_anchored_is_not_a_task(realm):
    realm.add_channel("work-m1")
    realm.post("work-m1", "workrun-task1-m1", "run something", sender_id=HUMAN_ID)
    assert worklog.read_task(realm, "work-m1", "workrun-task1-m1", BOT_ID) is None


def test_anchoring_is_idempotent_so_a_re_plan_writes_no_second_identity(realm, tmp_path):
    mission, tasks = open_two_tasks(realm, tmp_path)
    again = worklog.anchor_task(
        realm, mission.work_channel, worklog.run_topic_name(mission.mission_id, 1),
        mission.mission_id, 1, BOT_ID,
    )
    assert again.task_id == tasks[1].task_id


def test_a_topic_of_a_reused_name_belonging_to_another_mission_is_filtered_out(
    realm, tmp_path
):
    """Names narrow the walk; the note decides. A `workrun-` topic in this
    mission's channel that carries another mission's note is not this
    mission's task."""
    mission, _ = open_two_tasks(realm, tmp_path)
    stray = "workrun-task9-m999"
    realm.post(mission.work_channel, stray, "[selfnote][task] 999#9")
    assert 9 not in worklog.mission_tasks(realm, mission, BOT_ID)


# --- reconciliation --------------------------------------------------------


def test_reconciliation_matches_by_serial_and_leaves_state_alone(realm, tmp_path):
    mission, tasks = open_two_tasks(realm, tmp_path)
    worklog.record_result(realm, tasks[1], "done and dusted")
    live = worklog.mission_tasks(realm, mission, BOT_ID)
    assert live[1].state == worklog.TASK_COMPLETED

    changes = worklog.plan_changes(
        live, write_tasks(tmp_path, "# One\n\nfirst", "# Two\n\nsecond, revised")
    )
    assert [(c.serial, c.action) for c in changes] == [(1, "unchanged"), (2, "updated")]


def test_a_disappeared_serial_is_cancelled_and_a_new_one_created(realm, tmp_path):
    mission, _ = open_two_tasks(realm, tmp_path)
    live = worklog.mission_tasks(realm, mission, BOT_ID)
    (tmp_path / "task2.md").unlink()
    (tmp_path / "task3.md").write_text("# Three\n\nthird", encoding="utf-8")
    changes = worklog.plan_changes(live, tmp_path)
    assert [(c.serial, c.action) for c in changes] == [
        (1, "unchanged"), (3, "created"), (2, "cancelled"),
    ]


def test_changing_a_completed_task_is_its_own_action(realm, tmp_path):
    mission, tasks = open_two_tasks(realm, tmp_path)
    worklog.record_result(realm, tasks[1], "done")
    live = worklog.mission_tasks(realm, mission, BOT_ID)
    changes = worklog.plan_changes(
        live, write_tasks(tmp_path, "# One\n\nfirst, again", "# Two\n\nsecond")
    )
    assert changes[0].action == "changed-after-done"


def test_a_cancelled_task_leaves_the_mission(realm, tmp_path):
    mission, tasks = open_two_tasks(realm, tmp_path)
    assert worklog.cancel_tasks(realm, worklog.mission_tasks(realm, mission, BOT_ID)) == 2
    assert worklog.mission_tasks(realm, mission, BOT_ID) == {}


# --- results and the gate --------------------------------------------------


def test_a_result_is_posted_where_the_task_ran(realm, tmp_path):
    _, tasks = open_two_tasks(realm, tmp_path)
    worklog.record_result(realm, tasks[1], "it works")
    posted = realm.contents(tasks[1].channel, tasks[1].topic)
    assert any(text.startswith("## Result") and "it works" in text for text in posted)


def test_the_gate_holds_a_task_until_its_predecessor_is_finished(realm, tmp_path):
    _, tasks = open_two_tasks(realm, tmp_path)
    blocked = worklog.run_target(realm, tasks[2], BOT_ID)
    assert blocked.blocked_by is not None

    worklog.record_result(realm, tasks[1], "done")
    task2 = worklog.read_task(realm, tasks[2].channel, tasks[2].topic, BOT_ID)
    assert worklog.run_target(realm, task2, BOT_ID).blocked_by is None


def test_the_first_task_is_never_blocked(realm, tmp_path):
    _, tasks = open_two_tasks(realm, tmp_path)
    assert worklog.run_target(realm, tasks[1], BOT_ID).blocked_by is None


def test_accepting_a_task_satisfies_the_gate_too(realm, tmp_path):
    _, tasks = open_two_tasks(realm, tmp_path)
    worklog.set_task_state(realm, tasks[1], worklog.TASK_ACCEPTED)
    task2 = worklog.read_task(realm, tasks[2].channel, tasks[2].topic, BOT_ID)
    assert worklog.run_target(realm, task2, BOT_ID).blocked_by is None


def test_a_task_whose_mission_is_gone_cannot_run(realm, tmp_path):
    mission, tasks = open_two_tasks(realm, tmp_path)
    realm.delete(mission.mission_id)
    with pytest.raises(WorklogError):
        worklog.run_target(realm, tasks[1], BOT_ID)


# --- the read-back the planner is given ------------------------------------


def test_the_read_back_is_the_plan_and_the_live_tasks(realm, tmp_path, monkeypatch):
    mission, tasks = open_two_tasks(realm, tmp_path)
    out = tmp_path / "current"
    out.mkdir()
    assert worklog.write_mission_workspace(realm, out, CHANNEL, TOPIC, BOT_ID) is True
    assert (out / "mission.md").read_text(encoding="utf-8").startswith("# Ship it")
    assert (out / "task2.md").read_text(encoding="utf-8").strip() == "# Two\n\nsecond"

    worklog.set_task_state(realm, tasks[2], worklog.TASK_CANCELLED)
    for path in out.iterdir():
        path.unlink()
    worklog.write_mission_workspace(realm, out, CHANNEL, TOPIC, BOT_ID)
    assert sorted(p.name for p in out.iterdir()) == ["mission.md", "task1.md"]


def test_nothing_is_read_back_before_a_plan_exists(realm, tmp_path):
    assert worklog.write_mission_workspace(realm, tmp_path, CHANNEL, TOPIC, BOT_ID) is False
