"""A task agreement posted in the plan (failsafe p3, p2 trial D).

Front relayed the requester's agreement to task 1 into `workplan-soak-p2-d`
(#12435); the planning serving answered that the task was closed (#12439),
though nothing had closed it, and the mission's acceptance was then refused.
What is pinned: the plan's reply carries, from the record, that the task is
still open and where it closes; the planner reads where each task stands; a
requester post in the plan before any result, or a task with nothing shown,
brings no such line.
"""

from __future__ import annotations

from agautolab import zulip_listener as zl
from agautolab.worklog import TASK_OPEN, Task

AUTOLAB, FRONT, DEV = 11, 15, 8


def post(id, sender, content):
    return {"id": id, "sender_id": sender, "sender_full_name": {AUTOLAB: "autolab", FRONT: "Front", DEV: "Dev"}[sender],
            "content": content}


def task(serial=1, state=TASK_OPEN):
    return Task(12380, 12379, serial, "work-m12379", f"workrun-task{serial}-m12379", "# Soak", state)


TASK_HISTORY = [
    post(12383, AUTOLAB, "[selfnote][task] 12379#1"),
    post(12384, AUTOLAB, "# Soak\n\nrun it"),
    post(12390, AUTOLAB, "Message received. Please wait for the reply."),
    post(12428, AUTOLAB, "@**Front**\n\nSaved output …\n\n`ag-post intent=report end=12414`"),
]


def test_the_result_a_task_showed_waits_until_somebody_answers_in_its_topic():
    assert zl.shown_result(TASK_HISTORY, AUTOLAB) == 12428
    answered = [*TASK_HISTORY, post(12447, FRONT, "I agree to close task 1.")]
    assert zl.shown_result(answered, AUTOLAB) is None, "the task's own serving owns that answer"
    assert zl.shown_result(TASK_HISTORY[:3], AUTOLAB) is None, "no result shown yet"
    question = [*TASK_HISTORY[:3], post(12400, AUTOLAB, "Which file?\n\n`ag-post intent=response_request to=15 "
                                                        "ask=question`")]
    assert zl.shown_result(question, AUTOLAB) is None, "a question shows no result"


def test_an_agreement_in_the_plan_gets_the_correction_from_the_record():
    plan = [post(12379, AUTOLAB, "[selfnote][mission] robustp1"),
            post(12435, FRONT, "@**autolab-agstudio1** Task 1 of mission m12379: the Omni Agent agrees to close it "
                               "on the result you showed (#12428).\n\n`ag-post intent=report`")]
    lines = zl.misplaced_agreements([(task(), 12428)], plan, AUTOLAB, 12435)
    assert len(lines) == 1
    assert "task 1 is still open" in lines[0] and "#**work-m12379>workrun-task1-m12379**" in lines[0]
    assert "(#12428)" in lines[0] and "closes nothing" in lines[0]


def test_no_correction_without_a_shown_result_or_before_it():
    plan = [post(12379, AUTOLAB, "[selfnote][mission] robustp1"), post(12400, FRONT, "how is it going?")]
    assert zl.misplaced_agreements([(task(), None)], plan, AUTOLAB, 12400) == []
    assert zl.misplaced_agreements([(task(), 12428)], plan, AUTOLAB, 12400) == [], "spoken before the result"
    ours = [*plan, post(12450, AUTOLAB, "noted")]
    assert zl.misplaced_agreements([(task(), 12428)], ours, AUTOLAB, 12450) == [], "nobody else spoke last"


def test_the_planner_reads_where_each_task_stands():
    text = zl.task_status_text([(task(1, "completed"), None), (task(2), 12500)])
    assert "- task 1: completed (#work-m12379 › workrun-task1-m12379)" in text
    assert "- task 2: open; its result (#12500) waits for its requester's agreement in " \
           "#work-m12379 › workrun-task2-m12379" in text
    assert "Nothing said in this conversation closes a task." in text
