"""`agautolab.mission_done`: counting, not judgement.

A mission Work is Done when every one of its live Sub-Works is completed.
Nothing here is a decision an agent should be making, so nothing here is an
agent — what is pinned is the counting, whose issues it will touch (its own,
with children) and what it says when the answer is no.
"""

import io

import pytest

from agag import plane as shared_plane

from agautolab import mission, mission_done
from test_mission import FakePlane

CHANNEL = "pj-demo-project"
TOPIC = "workplan-test"

PROJECT = {
    "id": "p1",
    "identifier": "PD",
    "name": "demo",
    "description": "[AUTO] autolab project: demo",
}


@pytest.fixture
def plane(monkeypatch):
    fake = FakePlane()
    mission._LABEL_CACHE.clear()
    monkeypatch.setattr(mission_done, "load_plane_config", lambda path=None: mission.PlaneConfig(
        "http://plane", "key", "workspace"))
    monkeypatch.setattr(mission_done, "list_plane_projects", lambda config: [PROJECT])
    monkeypatch.setattr(shared_plane, "_request_json", fake)
    return fake


def work(plane, *, state="ready-id", key=f"{CHANNEL}/{TOPIC}", source=mission.EXTERNAL_SOURCE):
    return plane.add(
        name="A mission", state=state, external_id=key, external_source=source
    )


def task(plane, parent, serial, state):
    return plane.add(
        name=f"task {serial}",
        state=state,
        parent=str(parent["id"]),
        external_id=f"{CHANNEL}/{TOPIC}#{serial}",
        external_source=mission.EXTERNAL_SOURCE,
    )


def run(argv, expect=0):
    out, err = io.StringIO(), io.StringIO()
    code = mission_done.main(argv, out=out, err=err)
    assert code == expect, (code, out.getvalue(), err.getvalue())
    return out.getvalue(), err.getvalue()


# --- the counting ----------------------------------------------------------


def test_a_work_whose_every_sub_work_is_completed_is_moved(plane):
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    task(plane, parent, 2, "done-id")
    out, _ = run([])
    assert 'PD-4 Done "A mission" (2 sub-works)' in out
    assert plane.patches == [("issue-1", {"state": "done-id"})]


def test_one_unfinished_sub_work_leaves_the_mission_alone(plane):
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    task(plane, parent, 2, "started-id")
    out, _ = run([])
    assert "no mission Work is ready to be Done" in out
    assert plane.patches == []


def test_a_cancelled_sub_work_does_not_hold_the_mission_open(plane):
    """Cancelled is not pending: `sub_works` drops it, and so does the count."""
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    task(plane, parent, 2, "cancelled-id")
    out, _ = run([])
    assert 'PD-4 Done "A mission" (1 sub-works)' in out


def test_a_work_with_no_sub_work_is_not_a_mission(plane):
    work(plane, state="ready-id")
    out, _ = run([])
    assert "no mission Work is ready to be Done" in out
    assert plane.patches == []


def test_a_sweep_leaves_an_issue_this_agent_did_not_register_alone(plane):
    """Somebody's hand-made issue is not this command's business."""
    parent = work(plane, source="handmade")
    task(plane, parent, 1, "done-id")
    out, _ = run([])
    assert "no mission Work is ready to be Done" in out
    assert plane.patches == []


def test_a_mission_already_done_is_not_moved_again(plane):
    parent = work(plane, state="done-id")
    task(plane, parent, 1, "done-id")
    run([])
    assert plane.patches == []


# --- naming one Work -------------------------------------------------------


def test_a_named_work_is_moved_by_its_label(plane):
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    out, _ = run(["PD-4"])
    assert 'PD-4 Done "A mission"' in out
    assert plane.patches == [("issue-1", {"state": "done-id"})]


def test_a_named_work_is_moved_by_its_id_too(plane):
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    run([str(parent["id"])])
    assert plane.patches == [("issue-1", {"state": "done-id"})]


def test_a_named_work_that_is_not_finished_says_how_far_it_is(plane):
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    task(plane, parent, 2, "ready-id")
    out, _ = run(["PD-4"], expect=1)
    assert "PD-4 not moved: 1 of 2 sub-works are not completed" in out
    assert plane.patches == []


def test_a_named_work_already_done_is_reported_and_succeeds(plane):
    """Asking for a state it is already in is an answer, not a failure —
    the same call `agentchat resolve` makes on a resolved topic."""
    parent = work(plane, state="done-id")
    task(plane, parent, 1, "done-id")
    out, _ = run(["PD-4"])
    assert f"PD-4 not moved: {mission_done.ALREADY_DONE}" in out
    assert plane.patches == []


def test_a_name_that_matches_nothing_is_an_error(plane):
    work(plane)
    _, err = run(["PD-999"], expect=1)
    assert "no Work named PD-999" in err


# --- dry run ---------------------------------------------------------------


def test_dry_run_says_what_would_move_and_moves_nothing(plane):
    parent = work(plane)
    task(plane, parent, 1, "done-id")
    out, _ = run(["--dry-run"])
    assert 'PD-4 would be Done "A mission" (1 sub-works)' in out
    assert plane.patches == []
