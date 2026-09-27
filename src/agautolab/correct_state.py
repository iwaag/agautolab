"""Correct a task state a defect wrote, without rewriting its history.

failsafe p4 step 5. Cancelling two finished trial missions (failsafe p3)
wrote `[selfnote][state] cancelled` over their completed task 1 — the
`cancel_tasks` defect fixed in agautolab `32ad9b9`. Every reader takes the
newest state note, so the tasks read `cancelled` although their work was
completed, recorded and integrated.

The correction is one more note of the kind every reader already reads —
`[selfnote][state] completed`, newest wins — and beside it a
`[selfnote][correction]` note naming the note it corrects and why, so the
history keeps both the defect and its repair. Nothing is edited or deleted.
It is refused unless the record itself shows the case: a `completed` state
before the note being corrected, and no work after it. It writes `completed`
(this agent's own word), never `accepted` or `done`, which are the
requester's. The mission's own state is not touched: its cancellation was
the requester's decision.

Both notes are selfnotes, so nobody is served: no worker wakes, nothing is
integrated again. A work channel archived with its mission is unarchived for
the two writes and archived again.

    python -m agautolab.correct_state <task anchor id> --note <wrong state note id> --because "<why>"
"""

from __future__ import annotations

import argparse
import sys

from agag.selfnote import note
from agag.zulip import ZulipClient, ZulipError

from .anchor import parse_state
from .instance import SPEC
from .worklog import TASK_CANCELLED, TASK_COMPLETED, bare_topic, read_task

CORRECTION_TAG = "correction"


class CorrectionRefused(RuntimeError):
    """The record does not show the case this correction is for."""


def plan_correction(history: list[dict], self_id: int, wrong: int) -> int:
    """The id of the `completed` note the wrong `cancelled` one overwrote;
    refused (`CorrectionRefused`) unless the history shows exactly that."""
    by_id = {int(m.get("id") or 0): m for m in history}
    target = by_id.get(int(wrong))
    if target is None or target.get("sender_id") != self_id or parse_state(target.get("content")) != TASK_CANCELLED:
        raise CorrectionRefused(f"#{wrong} is not a `cancelled` state note of this agent in this task")
    states = [(int(m.get("id") or 0), parse_state(m.get("content"))) for m in history
              if m.get("sender_id") == self_id and parse_state(m.get("content")) is not None]
    before = [(i, s) for i, s in states if i < int(wrong)]
    if not before or before[-1][1] != TASK_COMPLETED:
        raise CorrectionRefused(f"the state before #{wrong} is not `completed`, so it did not overwrite a finished task")
    after = [(i, s) for i, s in states if i > int(wrong)]
    if after:
        raise CorrectionRefused(f"the task's state moved after #{wrong} (#{after[-1][0]} `{after[-1][1]}`); "
                                "correct it by hand if at all")
    if any(parse_state(m.get("content")) is None and int(m.get("id") or 0) > int(wrong)
           and m.get("sender_id") == self_id and not str(m.get("content") or "").startswith("[selfnote]")
           for m in history):
        raise CorrectionRefused(f"this agent spoke in the task after #{wrong}; its record is not only the defect")
    return before[-1][0]


def _stream(client: ZulipClient, name: str) -> dict | None:
    return next((s for s in client.channels(include_archived=True) if s.get("name") == name), None)


def correct(client: ZulipClient, task_id: int, wrong: int, because: str) -> str:
    self_id = int(client.whoami()["user_id"])
    found = client.message(int(task_id), strict=True)
    if not found:
        raise CorrectionRefused(f"task anchor #{task_id} is gone")
    channel = str(found.get("display_recipient"))
    topic = str(found.get("subject"))
    task = read_task(client, channel, topic, self_id)
    if task is None or task.task_id != int(task_id):
        raise CorrectionRefused(f"#{task_id} is not a task anchor of this agent")
    history = client.topic_history(channel, topic, num_before=1000)
    completed = plan_correction(history, self_id, wrong)
    stream = _stream(client, channel)
    archived = bool(stream and stream.get("is_archived"))
    if archived:
        client.call("PATCH", f"streams/{int(stream['stream_id'])}", {"is_archived": False})
    try:
        client.send_to_channel(channel, topic, note("state", TASK_COMPLETED))
        client.send_to_channel(channel, topic, note(
            CORRECTION_TAG, f"#{int(wrong)} state {TASK_CANCELLED} -> {TASK_COMPLETED} (as #{completed}): {because}"))
    finally:
        if archived:
            client.archive_channel(int(stream["stream_id"]))
    return (f"task {task.serial} of m{task.mission_id} ({channel}/{bare_topic(topic)}) reads `completed` again; "
            f"#{wrong} stays in its history, named by a correction note"
            + ("; the channel was unarchived for the two notes and archived again" if archived else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agautolab.correct_state", description=__doc__.split("\n\n")[0])
    parser.add_argument("task_id", type=int, help="the task's anchor (its [selfnote][task] note)")
    parser.add_argument("--note", type=int, required=True, help="the `cancelled` state note written in error")
    parser.add_argument("--because", required=True, help="why it was wrong, for the record")
    args = parser.parse_args(argv)
    try:
        print(correct(ZulipClient.from_env(SPEC.zulip_env), args.task_id, args.note, args.because))
    except (CorrectionRefused, ZulipError) as error:
        print(f"not corrected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
