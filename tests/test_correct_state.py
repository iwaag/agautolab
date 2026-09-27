"""failsafe p4 step 5: a defect's `cancelled` over a completed task is
corrected by one more note of the kind readers read, never by an edit."""

from __future__ import annotations

import pytest

from agautolab.anchor import own_state
from agautolab.correct_state import CorrectionRefused, plan_correction

BOT, DEV = 11, 8


def m(i, content, sender=BOT):
    return {"id": i, "sender_id": sender, "content": content}


HISTORY = [
    m(1, "[selfnote][task] 7#1"),
    m(2, "# task"),
    m(3, "done", DEV),
    m(4, "## Result\n\nok"),
    m(5, "[selfnote][state] completed"),
    m(9, "[selfnote][state] cancelled"),
]


def test_the_defect_is_recognised_and_the_correction_reads_completed():
    assert plan_correction(HISTORY, BOT, 9) == 5
    corrected = [*HISTORY, m(10, "[selfnote][state] completed"),
                 m(11, "[selfnote][correction] #9 state cancelled -> completed (as #5): defect")]
    assert own_state(corrected, BOT) == "completed"


@pytest.mark.parametrize("history, wrong", [
    ([*HISTORY[:4], m(9, "[selfnote][state] cancelled")], 9),          # never completed
    (HISTORY, 5),                                                      # not a cancelled note
    ([*HISTORY, m(12, "[selfnote][state] held")], 9),                  # moved since
    ([*HISTORY[:5], m(9, "[selfnote][state] cancelled", DEV)], 9),     # somebody else's note
    ([*HISTORY, m(12, "started again")], 9),                           # the agent worked after it
])
def test_anything_but_that_case_is_refused(history, wrong):
    with pytest.raises(CorrectionRefused):
        plan_correction(history, BOT, wrong)
