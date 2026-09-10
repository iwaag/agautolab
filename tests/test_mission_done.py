"""`agautolab.mission_done`: counting, not judgement.

A mission is done when every one of its live tasks is finished. Nothing here
is a decision an agent should be making, so nothing here is an agent — what
is pinned is the counting, which conversations it will touch and what it says
when the answer is no.

Since `refactor` p1 it counts the conversations rather than Plane issues, so
these tests run against the realm the record actually lives in.
"""

import io

import pytest

from agautolab import mission_done, worklog

from realm import BOT_ID, HUMAN_ID, Realm

CHANNEL = "pj-demo"
TOPIC = "workplan-ship-it"


@pytest.fixture
def realm():
    room = Realm()
    room.add_channel(CHANNEL)
    room.post(CHANNEL, TOPIC, "Please ship it", sender_id=HUMAN_ID)
    return room


def mission_with(realm, *states, topic=TOPIC):
    """A planned mission with one task per state, opened and marked."""
    realm.post(CHANNEL, topic, "go", sender_id=HUMAN_ID)
    _, mission = worklog.record_plan(
        realm, CHANNEL, topic, "demo", "# A mission\n\ndo it\n", BOT_ID
    )
    realm.add_channel(mission.work_channel)
    for serial, state in enumerate(states, start=1):
        run_topic = worklog.run_topic_name(mission.mission_id, serial)
        task = worklog.anchor_task(
            realm, mission.work_channel, run_topic, mission.mission_id, serial, BOT_ID
        )
        worklog.post_document(
            realm, mission.work_channel, run_topic, f"# task {serial}\n\nwork\n"
        )
        if state != worklog.TASK_OPEN:
            worklog.set_task_state(realm, task, state)
    return mission


def run(realm, argv, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = mission_done.main(argv, out=out, err=err, client=realm)
    assert code == expect, (code, out.getvalue(), err.getvalue())
    return out.getvalue(), err.getvalue()


def state_of(realm, mission):
    return worklog.read_mission(realm, CHANNEL, mission.topic, BOT_ID).state


# --- the counting ----------------------------------------------------------


def test_a_mission_whose_every_task_is_finished_is_moved(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED, worklog.TASK_COMPLETED)
    out, _ = run(realm, [])
    assert f'{mission.label} done "A mission" (2 tasks)' in out
    assert state_of(realm, mission) == worklog.MISSION_DONE


def test_one_unfinished_task_leaves_the_mission_alone(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED, worklog.TASK_OPEN)
    out, _ = run(realm, [])
    assert "no mission is ready to be done" in out
    assert state_of(realm, mission) == worklog.MISSION_PLANNED


def test_a_cancelled_task_does_not_hold_the_mission_open(realm):
    """Cancelled is not pending: it leaves the mission, and the count."""
    mission = mission_with(realm, worklog.TASK_COMPLETED, worklog.TASK_CANCELLED)
    out, _ = run(realm, [])
    assert f'{mission.label} done "A mission" (1 tasks)' in out


def test_an_accepted_task_counts_as_finished(realm):
    mission = mission_with(realm, worklog.TASK_ACCEPTED)
    out, _ = run(realm, [])
    assert f"{mission.label} done" in out


def test_a_mission_with_no_task_never_ran(realm):
    mission = mission_with(realm)
    out, _ = run(realm, [])
    assert "no mission is ready to be done" in out
    assert state_of(realm, mission) == worklog.MISSION_PLANNED


def test_a_topic_nobody_planned_is_not_a_mission(realm):
    realm.post(CHANNEL, "workplan-just-asking", "what do you think?", sender_id=HUMAN_ID)
    out, _ = run(realm, [])
    assert "no mission is ready to be done" in out


def test_a_mission_already_done_is_not_moved_again(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED)
    run(realm, [])
    before = len(realm.order)
    run(realm, [])
    assert len(realm.order) == before


# --- naming one mission ----------------------------------------------------


def test_a_named_mission_is_moved_by_its_label(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED)
    out, _ = run(realm, [mission.label])
    assert f'{mission.label} done "A mission"' in out
    assert state_of(realm, mission) == worklog.MISSION_DONE


def test_a_named_mission_is_moved_by_its_bare_id_too(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED)
    run(realm, [str(mission.mission_id)])
    assert state_of(realm, mission) == worklog.MISSION_DONE


def test_a_named_mission_that_is_not_finished_says_how_far_it_is(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED, worklog.TASK_OPEN)
    out, _ = run(realm, [mission.label], expect=1)
    assert f"{mission.label} not moved: task 2 of 2 is not finished" in out
    assert state_of(realm, mission) == worklog.MISSION_PLANNED


def test_a_named_mission_already_done_is_reported_and_succeeds(realm):
    """Asking for a state it is already in is an answer, not a failure —
    the same call `agentchat resolve` makes on a resolved topic."""
    mission = mission_with(realm, worklog.TASK_COMPLETED)
    run(realm, [mission.label])
    out, _ = run(realm, [mission.label])
    assert f"{mission.label} not moved: {mission_done.ALREADY_DONE}" in out


def test_a_name_that_matches_nothing_is_an_error(realm):
    mission_with(realm, worklog.TASK_COMPLETED)
    _, err = run(realm, ["m999999"], expect=1)
    assert "no mission named m999999" in err


def test_a_named_mission_is_found_after_its_topic_was_resolved(realm):
    """The anchor is the identity, so closing out a finished conversation
    does not hide it from the command that closes the mission."""
    mission = mission_with(realm, worklog.TASK_COMPLETED)
    realm.rename_topic(CHANNEL, mission.topic, f"✔ {mission.topic}")
    out, _ = run(realm, [mission.label])
    assert f"{mission.label} done" in out


# --- dry run ---------------------------------------------------------------


def test_dry_run_says_what_would_move_and_moves_nothing(realm):
    mission = mission_with(realm, worklog.TASK_COMPLETED)
    out, _ = run(realm, ["--dry-run"])
    assert f'{mission.label} would be done "A mission" (1 tasks)' in out
    assert state_of(realm, mission) == worklog.MISSION_PLANNED
