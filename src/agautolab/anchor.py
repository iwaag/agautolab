"""What a `workrun-` topic knows about itself, written in the topic.

Until `agent_standardize` p9 a `workrun-` topic said what it was for by its
*name* and its channel's *description*: the serial came out of
`workrun-task3-pa-12`, and `project: …; mission: pj-x/workplan-y` was parsed
back out of the `work-pa-12` channel description. That worked, and it meant
the binding lived in two places neither of which is the conversation.

Since p9 the topic carries it, the way agforge's `assetrun-` topics have
since p8. autolab opens the topic when it plans the task and anchors it with
two selfnotes (`agag.selfnote`), hidden from every chatlog and every `read`:

    [selfnote][rootchat] pj-<slug>/workplan-<stem>
    [selfnote][work] <plane issue id>

The first is the ordinary root note every agent writes when it speaks
somewhere on behalf of one of its own conversations — here autolab speaking
to itself, and it is where the mission was planned. Its channel is also
where the project slug comes from. The second is autolab's own tag: the
Sub-Work this topic executes, so a serving looks up its task by id instead of
by counting.

The channel description is still written, unchanged, because a human opening
`work-pa-12` should be able to see what it is for. It is no longer what the
code reads.
"""

from __future__ import annotations

from agag.selfnote import Conversation, note, own_rootchat, parse_note, rootchat_note

#: autolab's own selfnote tag, beside the shared `rootchat` one.
WORK_TAG = "work"

__all__ = [
    "WORK_TAG",
    "Conversation",
    "own_rootchat",
    "own_work",
    "parse_work",
    "rootchat_note",
    "work_note",
]


def work_note(issue_id: str) -> str:
    """`[selfnote][work] <issue id>` — the Sub-Work this topic executes."""
    return note(WORK_TAG, str(issue_id))


def parse_work(content) -> str | None:
    """The Plane issue id a work note names, or None for anything else."""
    value = parse_note(content, WORK_TAG)
    return value or None


def own_work(messages, self_id: int) -> str | None:
    """The Sub-Work this bot anchored this topic to, reading its history.

    The earliest note wins, as with the root note: a topic runs the task it
    was opened for, and a re-plan writes no second note — it updates the
    issue behind the same serial, which is the same issue id.
    """
    for message in messages:
        if message.get("sender_id") != self_id:
            continue
        found = parse_work(message.get("content"))
        if found is not None:
            return found
    return None
