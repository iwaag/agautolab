"""Retiring a plan conversation and opening its replacement (`refactor` p2).

Four things had to be true before this workflow could be trusted, and each
one is a way the old Plane-backed model would have got the answer wrong:

- a **reused topic name** is not the work that used to wear it;
- a **late callback** to a retired task reaches that task, and runs nothing;
- a **restart** finds the replacement and nothing of what was retired, so no
  serving happens twice;
- a **deleted origin** is absent, not whatever is standing where it stood.

They are tested against `realm.Realm`, which renames topics and archives
channels the way Zulip does — including the part that makes the ordering
matter: a topic renamed onto a name that already exists merges into it.
"""

import pytest

from agag.zulip import sweep_topics
from agautolab import worklog
from agautolab.worklog import (
    MISSION_REPLACED,
    TASK_CANCELLED,
    TASK_COMPLETED,
)

from realm import BOT_ID, HUMAN_ID, Realm

CHANNEL = "pj-demo"
TOPIC = "workplan-ship-it"
PLAN = "# Ship it\n\nTwo tasks.\n"
NEW_PLAN = "# Ship something else\n\nOne task.\n"


@pytest.fixture
def realm():
    room = Realm()
    room.add_channel(CHANNEL)
    room.post(CHANNEL, TOPIC, "Please ship it", sender_id=HUMAN_ID)
    return room


def planned(realm, *, finish_first=True):
    """A mission with two task topics, the first of them completed."""
    _, mission = worklog.record_plan(realm, CHANNEL, TOPIC, "demo", PLAN, BOT_ID)
    realm.add_channel(mission.work_channel)
    tasks = []
    for serial in (1, 2):
        topic = worklog.run_topic_name(mission.mission_id, serial)
        task = worklog.anchor_task(
            realm, mission.work_channel, topic, mission.mission_id, serial, BOT_ID
        )
        worklog.post_document(
            realm, mission.work_channel, topic, f"# Task {serial}\n\nbody\n"
        )
        tasks.append(worklog.read_task(realm, mission.work_channel, topic, BOT_ID))
    if finish_first:
        worklog.record_result(realm, tasks[0], "done")
    return mission, tasks


# --- the workflow ----------------------------------------------------------


def test_replacing_retires_the_old_work_and_opens_a_new_mission(realm):
    mission, tasks = planned(realm)

    result = worklog.replace_mission(realm, mission, BOT_ID, reason="Wrong request.")

    # The old conversation kept every message and its identity, and gave up
    # its name: it is renamed out of the `workplan-` vocabulary *and* resolved.
    retired = worklog.mission_at(realm, mission.mission_id, BOT_ID)
    assert retired is not None
    assert retired.state == MISSION_REPLACED
    assert result.retired_topic == f"✔ retired-{TOPIC}-{mission.label}"
    assert realm.topic_of(mission.mission_id)[1] == result.retired_topic

    # The replacement wears the freed name and is different work.
    assert result.mission.topic == TOPIC
    assert result.mission.mission_id != mission.mission_id
    assert result.mission.replaces == mission.mission_id
    assert worklog.predecessor(realm, result.mission, BOT_ID).mission_id == mission.mission_id

    # Finished work is carried by reference; unfinished work is dropped.
    assert [task.serial for task in result.carried] == [1]
    assert [task.serial for task in result.dropped] == [2]
    summary = realm.contents(CHANNEL, TOPIC)[-1]
    assert "Carried forward" in summary and "task 1" in summary
    assert "Dropped" in summary and "task 2" in summary
    # Carried forward means referenced, not re-created as a finished row: the
    # replacement has no tasks at all until it is planned.
    assert worklog.mission_tasks(realm, result.mission, BOT_ID) == {}


def test_the_completed_task_keeps_its_record_and_the_unfinished_one_is_cancelled(realm):
    mission, tasks = planned(realm)

    worklog.replace_mission(realm, mission, BOT_ID)

    finished = worklog.task_at(realm, tasks[0].task_id, BOT_ID)
    dropped = worklog.task_at(realm, tasks[1].task_id, BOT_ID)
    assert finished.state == TASK_COMPLETED
    assert dropped.state == TASK_CANCELLED


def test_the_replacement_is_planned_like_any_fresh_conversation(realm):
    mission, _ = planned(realm)
    result = worklog.replace_mission(realm, mission, BOT_ID)

    line, planned_again = worklog.record_plan(
        realm, CHANNEL, TOPIC, "demo", NEW_PLAN, BOT_ID
    )

    # `record_plan` finds the anchor the replacement already wrote, so the
    # mission is not minted twice and the `replaces` link survives.
    assert planned_again.mission_id == result.mission.mission_id
    assert line.startswith("recorded the plan")
    assert worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID).replaces == mission.mission_id


def test_a_conversation_with_nothing_in_it_is_already_nothing_to_retire(realm):
    realm.add_channel("pj-empty")
    assert worklog.retire_conversation(realm, "pj-empty", "workplan-nothing", "m1") == (
        "workplan-nothing"
    )


# --- the four verification cases ------------------------------------------


def test_a_reused_topic_name_is_not_the_work_that_used_to_wear_it(realm):
    """The replacement takes the retired conversation's display name."""
    mission, _ = planned(realm)
    result = worklog.replace_mission(realm, mission, BOT_ID)

    # Reading the name gives the new work...
    here = worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID)
    assert here.mission_id == result.mission.mission_id
    # ...and the old work is still itself, found through its anchor, with its
    # plan and its state intact under the name it was moved to.
    old = worklog.mission_at(realm, mission.mission_id, BOT_ID)
    assert old.plan == PLAN.strip() and old.state == MISSION_REPLACED
    assert old.topic != TOPIC


def test_a_late_callback_reaches_the_retired_task_and_runs_nothing(realm):
    """A delegation names its home by channel/topic text, so the question is
    whether that text still finds the task it was written for.

    It does, and by construction rather than by luck: a task topic's name is
    minted from its mission's **anchor id**, so the replacement's tasks live
    in another channel entirely and no later work can want this name. The
    retirement resolves the topic rather than renaming it, and reading a
    conversation follows the ✔ — so the late answer lands on the cancelled
    task, where the gate refuses to run anything.
    """
    mission, tasks = planned(realm, finish_first=False)
    late = tasks[1]
    worklog.replace_mission(realm, mission, BOT_ID)

    found = worklog.read_task(realm, late.channel, late.topic, BOT_ID)
    assert found is not None and found.task_id == late.task_id
    assert found.state == TASK_CANCELLED
    with pytest.raises(worklog.WorklogError, match="cancelled"):
        worklog.run_target(realm, found, BOT_ID)


def test_a_restart_finds_the_replacement_and_nothing_that_was_retired(realm):
    """The sweep is the execution queue, so this is what "leaves the queue"
    has to mean: after a replacement, a listener starting from nothing sees
    the replacement's conversation and none of the retired one's."""
    mission, _ = planned(realm)
    worklog.replace_mission(realm, mission, BOT_ID)
    # A human speaks in every topic that still exists, which is the only way
    # a topic can await a reply at all.
    for channel, topic in [
        (CHANNEL, TOPIC),
        (CHANNEL, f"✔ retired-{TOPIC}-{mission.label}"),
    ]:
        realm.post(channel, topic, "anything", sender_id=HUMAN_ID)

    awaiting = sweep_topics(realm, BOT_ID, ("workplan-", "workrun-"))

    assert awaiting == [(CHANNEL, TOPIC)]
    # The work channel is gone from the subscriptions, so its task topics are
    # not walked at all — the coarse half of leaving the queue.
    assert mission.work_channel in realm.archived
    assert all(row["name"] != mission.work_channel for row in realm.subscriptions())


def test_a_deleted_origin_is_absent_not_the_work_standing_where_it_stood(realm):
    mission, _ = planned(realm)
    result = worklog.replace_mission(realm, mission, BOT_ID)

    realm.delete(mission.mission_id)

    # The replacement still says what it replaced, and the answer to "what
    # was that" is honestly nothing — emphatically not the replacement, which
    # is the thing now wearing the retired conversation's name.
    assert result.mission.replaces == mission.mission_id
    assert worklog.predecessor(realm, result.mission, BOT_ID) is None
    assert worklog.mission_at(realm, mission.mission_id, BOT_ID) is None


# --- the route the agent actually uses ------------------------------------
#
# `replace.flag` beside `plan.md` in the planning workspace. These run the
# real handler against the realm, so what is checked is the whole round: the
# flag is read before the plan, and the plan lands in the conversation the
# replacement opened rather than the one it retired.


def test_replace_flag_retires_the_mission_and_plans_into_its_replacement(realm, tmp_path):
    from agautolab import zulip_listener

    mission, _ = planned(realm)
    (tmp_path / "plan.md").write_text(NEW_PLAN, encoding="utf-8")
    (tmp_path / "task1.md").write_text("# Only task\n\nbody\n", encoding="utf-8")
    (tmp_path / "replace.flag").write_text("The request was wrong.\n", encoding="utf-8")

    sections, resolve_after = zulip_listener.handle_superdirector_response(
        realm, CHANNEL, TOPIC, "demo", tmp_path, BOT_ID,
        requester_mention="@**Developer**",
    )

    assert resolve_after is False
    assert f"mission {mission.label} is retired" in sections[0]
    assert "carries forward 1 finished task(s)" in sections[0]

    # The plan went into the replacement, not into the retired conversation.
    replacement = worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID)
    assert replacement.mission_id != mission.mission_id
    assert replacement.plan == NEW_PLAN.strip()
    assert worklog.mission_at(realm, mission.mission_id, BOT_ID).plan == PLAN.strip()

    # And its own task surfaces are in its own channel.
    tasks = worklog.mission_tasks(realm, replacement, BOT_ID)
    assert [task.serial for task in tasks.values()] == [1]
    assert replacement.work_channel in realm.streams

    # The requester is named where the replacement opens, because the final
    # reply is posted into a conversation they have never spoken in.
    assert any(
        "@**Developer**" in content for content in realm.contents(CHANNEL, TOPIC)
    )


def test_replace_flag_without_a_plan_changes_nothing(realm, tmp_path):
    from agautolab import zulip_listener

    mission, _ = planned(realm)
    (tmp_path / "replace.flag").write_text("scrap it\n", encoding="utf-8")

    sections, _ = zulip_listener.handle_superdirector_response(
        realm, CHANNEL, TOPIC, "demo", tmp_path, BOT_ID
    )

    assert sections == [zulip_listener.NO_REPLACEMENT_PLAN]
    still = worklog.read_mission(realm, CHANNEL, TOPIC, BOT_ID)
    assert still.mission_id == mission.mission_id
    assert still.state != MISSION_REPLACED
    assert mission.work_channel not in realm.archived


def test_a_replaced_mission_is_never_swept_as_finished(realm):
    """Two things would otherwise close a replaced mission by accident, and
    the state note is the answer to both.

    Its work channel is archived, so its tasks cannot be read at all; and if
    the archive had failed, retiring cancelled every unfinished task and the
    sweep does not count cancelled ones — leaving exactly the tasks that
    *did* finish, which is what "all finished" looks like.
    """
    from agautolab import mission_done

    mission, _ = planned(realm)
    worklog.replace_mission(realm, mission, BOT_ID)
    retired = worklog.mission_at(realm, mission.mission_id, BOT_ID)

    assert worklog.mission_tasks(realm, retired, BOT_ID) == {}
    assert "replaced" in mission_done.reason_not_finished(retired, [])
    # The second reading: the archive did not take, so the completed task is
    # still visible and the mission counts as complete on the numbers alone.
    realm.archived.remove(mission.work_channel)
    tasks = sorted(
        worklog.mission_tasks(realm, retired, BOT_ID).values(),
        key=lambda task: task.serial,
    )
    assert [task.state for task in tasks] == [TASK_COMPLETED]
    assert "replaced" in mission_done.reason_not_finished(retired, tasks)
    assert [c.label for c in mission_done.finished_missions(realm, BOT_ID)] == []
