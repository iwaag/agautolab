"""The next task of an authorized mission starts when the one before it closes
(`robust_workflow` p1 step 3).

Until now every task of a mission waited for somebody's post in its own topic,
so a requester who had already said the mission may run, and had just accepted
the task before, still had to relay a start — the step adventure_game p3 lost
24 minutes to when Front reported a start it never made. The close-out now
posts the start itself. These tests pin when it does, when it says why not,
and that a repeat never starts a task twice.
"""

import pytest
from dataclasses import replace

from agag.selfnote import owed_start, parse_start

from agautolab import worklog
from agautolab.worklog import MISSION_STARTED, RunTarget, TASK_HELD
from agautolab.zulip_listener import start_next_task

from realm import BOT_ID, HUMAN_ID, Realm
from test_worklog import CHANNEL, TOPIC, open_two_tasks

FRONT = 15


@pytest.fixture
def realm():
    room = Realm()
    room.add_channel(CHANNEL)
    room.post(CHANNEL, TOPIC, "Please ship it", sender_id=HUMAN_ID)
    return room


def closed_first(realm, tmp_path, state=MISSION_STARTED):
    mission, tasks = open_two_tasks(realm, tmp_path)
    mission = replace(mission, state=state)
    first = worklog.set_task_state(realm, tasks[1], worklog.TASK_COMPLETED)
    accepted = realm.post(first.channel, first.topic, "Task 1 is accepted.", sender_id=FRONT)
    requester = {"id": accepted, "sender_id": FRONT, "sender_full_name": "Front"}
    return RunTarget(first, mission), tasks[2], requester


def contents(realm, task):
    return realm.contents(task.channel, task.topic)


def test_the_next_task_starts_with_a_visible_line_and_a_start_note(realm, tmp_path):
    target, second, requester = closed_first(realm, tmp_path)
    line = start_next_task(realm, target, BOT_ID, requester)
    assert line.startswith("task 2 of") and "starts now" in line
    posted = contents(realm, second)
    visible = [text for text in posted if text.startswith("Task 2 of")]
    assert visible and "@**" not in visible[0], "the start line names nobody: a mention would buy a run"
    history = realm.topic_history(second.channel, second.topic)
    pending = owed_start(history, BOT_ID)
    assert pending is not None and pending["sender_id"] == FRONT
    assert parse_start(history[-1]["content"])[0] == requester["id"], "the start names the acceptance"


def test_a_repeated_close_out_never_starts_a_task_twice(realm, tmp_path):
    target, second, requester = closed_first(realm, tmp_path)
    start_next_task(realm, target, BOT_ID, requester)
    before = len(contents(realm, second))
    line = start_next_task(realm, target, BOT_ID, requester)
    assert "already under way" in line
    assert len(contents(realm, second)) == before


def test_a_task_somebody_already_posted_into_is_left_alone(realm, tmp_path):
    """The old relay still works, and is recognised: a start that arrived by
    post first is not doubled by the close-out."""
    target, second, requester = closed_first(realm, tmp_path)
    realm.post(second.channel, second.topic, "Start task 2.", sender_id=FRONT)
    assert "already under way" in start_next_task(realm, target, BOT_ID, requester)


def test_a_mission_that_was_never_started_does_not_advance(realm, tmp_path):
    target, second, requester = closed_first(realm, tmp_path, state=worklog.MISSION_PLANNED)
    line = start_next_task(realm, target, BOT_ID, requester)
    assert "was not started" in line
    assert owed_start(realm.topic_history(second.channel, second.topic), BOT_ID) is None


def test_the_requester_can_hold_the_next_task(realm, tmp_path):
    target, second, requester = closed_first(realm, tmp_path)
    line = start_next_task(realm, target, BOT_ID, requester, hold="wait for my review of the art")
    assert "is held" in line and "wait for my review" in line
    history = realm.topic_history(second.channel, second.topic)
    assert owed_start(history, BOT_ID) is None
    assert worklog.read_task(realm, second.channel, second.topic, BOT_ID).state == TASK_HELD
    # A later close-out does not override the hold.
    assert "is held" in start_next_task(realm, target, BOT_ID, requester)


def test_the_last_task_closing_starts_nothing(realm, tmp_path):
    target, second, requester = closed_first(realm, tmp_path)
    worklog.set_task_state(realm, second, worklog.TASK_COMPLETED)
    last = RunTarget(replace(second, state=worklog.TASK_COMPLETED), target.mission)
    assert "every task" in start_next_task(realm, last, BOT_ID, requester)


def test_starting_the_mission_starts_its_first_task(realm, tmp_path):
    from agautolab.zulip_listener import start_first_task

    mission, tasks = open_two_tasks(realm, tmp_path)
    mission = replace(mission, state=MISSION_STARTED)
    said = realm.post(CHANNEL, TOPIC, "The plan is accepted; start it.", sender_id=FRONT)
    requester = {"id": said, "sender_id": FRONT, "sender_full_name": "Front"}
    line = start_first_task(realm, mission, BOT_ID, requester)
    assert line.startswith("task 1 of") and "starts now" in line
    first = tasks[1]
    assert owed_start(realm.topic_history(first.channel, first.topic), BOT_ID)["sender_id"] == FRONT
    assert owed_start(realm.topic_history(tasks[2].channel, tasks[2].topic), BOT_ID) is None
    assert "already under way" in start_first_task(realm, mission, BOT_ID, requester)


def test_the_injected_skip_fault_skips_one_start_and_is_spent(realm, tmp_path, monkeypatch):
    from agautolab import zulip_listener

    fault = tmp_path / "faults" / "skip-next-start"
    fault.parent.mkdir()
    fault.touch()
    monkeypatch.setattr(zulip_listener, "SKIP_START_FAULT", fault)
    target, second, requester = closed_first(realm, tmp_path)
    assert "injected fault" in start_next_task(realm, target, BOT_ID, requester)
    assert not fault.exists()
    assert owed_start(realm.topic_history(second.channel, second.topic), BOT_ID) is None
    assert "starts now" in start_next_task(realm, target, BOT_ID, requester)
