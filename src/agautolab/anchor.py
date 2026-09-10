"""What a work conversation knows about itself, written in the conversation.

Until `agent_standardize` p9 a `workrun-` topic said what it was for by its
*name* and its channel's *description*. p9 moved that into the topic, as two
selfnotes (`agag.selfnote`) — a `[rootchat]` note naming the mission
conversation and a `[work]` note naming the Plane Sub-Work it executed.

`refactor` p1 removes the second system. There is no Plane issue to name any
more, so the notes here carry the whole record instead of a pointer into one:

    [selfnote][mission] <project slug>       in a workplan- topic
    [selfnote][task] <mission id>#<serial>   in a workrun- topic
    [selfnote][doc] <message id>             the visible document that is current
    [selfnote][state] <state word>           the newest one wins
    [selfnote][replaces] <message id>        the work this one was opened to replace

**The identity of a mission is the message id of its own `[mission]` note**,
and a task's is the id of its `[task]` note. Nothing is keyed on a topic's
name, so a topic may be renamed, resolved, moved, or replaced by other work
of the same name and the record still says which conversation is which. A
deleted note is a deleted work record: absent, rather than silently the next
thing that took its name.

`refactor` p2 adds the fifth note, which is a *relation* rather than a fact
about one conversation. Replacement is how work is revised when re-planning
in place is not enough: the old conversation is retired and a new one is
opened, and `[selfnote][replaces]` in the new one names the old one's anchor
by id. By id, because the replacement usually takes over the retired
conversation's display name — that is the point of retiring it — so a name
would point at the replacement itself. A `replaces` pointer whose target has
been deleted reads as **absent**, which is the honest answer for a
predecessor nobody kept.

The `[rootchat]` note stays exactly as it was — it is the shared convention
that routes a callback home, and it says which conversation autolab is
speaking on behalf of. The two answer different questions: `rootchat` is
"where do I reply", `task` is "what am I running".

`agag.selfnote` has the format and the reason selfnotes never buy a run.
"""

from __future__ import annotations

import re

from agag.selfnote import Conversation, note, own_rootchat, parse_note, rootchat_note

#: autolab's own selfnote tags, beside the shared `rootchat` and `served`.
MISSION_TAG = "mission"
TASK_TAG = "task"
DOC_TAG = "doc"
STATE_TAG = "state"
REPLACES_TAG = "replaces"

#: `<mission id>#<serial>` — what a task note carries.
TASK_VALUE = re.compile(r"^(?P<mission>\d+)\s*#\s*(?P<serial>\d+)$")

__all__ = [
    "DOC_TAG",
    "EXTERNAL_STATES",
    "MISSION_TAG",
    "REPLACES_TAG",
    "STATE_TAG",
    "TASK_TAG",
    "Conversation",
    "doc_note",
    "mission_note",
    "own_doc",
    "own_mission",
    "own_replaces",
    "own_rootchat",
    "own_state",
    "own_task",
    "parse_doc",
    "parse_mission",
    "parse_replaces",
    "parse_state",
    "parse_task",
    "replaces_note",
    "rootchat_note",
    "state_note",
    "task_note",
]


def mission_note(slug: str) -> str:
    """`[selfnote][mission] <project slug>` — this conversation is a mission.

    The value is the project, which is also readable from the channel name;
    it is written anyway so the note says what it is to anyone who finds it
    alone. What matters is the note's **own message id**: that is the
    mission.
    """
    return note(MISSION_TAG, str(slug))


def parse_mission(content) -> str | None:
    """The project slug a mission note names, or None for anything else."""
    return parse_note(content, MISSION_TAG)


def task_note(mission_id: int, serial: int) -> str:
    """`[selfnote][task] <mission id>#<serial>` — this conversation is a task.

    The note's own message id is the task; the value says which mission it
    belongs to and which position it holds in that mission's order.
    """
    return note(TASK_TAG, f"{int(mission_id)}#{int(serial)}")


def parse_task(content) -> tuple[int, int] | None:
    """`(mission id, serial)` of a task note, or None for anything else."""
    value = parse_note(content, TASK_TAG)
    if value is None:
        return None
    match = TASK_VALUE.match(value.strip())
    if not match:
        return None
    return int(match.group("mission")), int(match.group("serial"))


def doc_note(message_id: int) -> str:
    """`[selfnote][doc] <message id>` — which post is the current document.

    A plan is re-posted when it changes and a task description is re-posted
    when the planner rewrites it, so "the document" cannot be "the newest
    post" — the newest post is usually the developer talking. This names it.
    """
    return note(DOC_TAG, str(int(message_id)))


def parse_doc(content) -> int | None:
    """The message id a doc note names, or None for anything else."""
    value = parse_note(content, DOC_TAG)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def replaces_note(anchor_id: int) -> str:
    """`[selfnote][replaces] <message id>` — what this work was opened for.

    Written once, when the replacement conversation is opened, beside the
    note that gives it its own identity. It is the only link back: the
    retired conversation has usually given up its display name to this one.
    """
    return note(REPLACES_TAG, str(int(anchor_id)))


def parse_replaces(content) -> int | None:
    """The anchor id a replaces note names, or None for anything else."""
    value = parse_note(content, REPLACES_TAG)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


#: The two state words somebody **other than this bot** may write.
#:
#: Every other state is this agent's own report of its own work, and a note
#: from another sender claiming it would be somebody moving work they did not
#: do. These two are the opposite: `accepted` and `done` are a *person*
#: accepting the work, and the person accepts it from the operation room, with
#: their own credential (`agentroom.close`). Read them from anyone or the
#: acceptance never arrives — which is what `refactor` p1 step 4 met live, the
#: room's own write invisible to the record it was written into.
EXTERNAL_STATES = ("accepted", "done")


def state_note(state: str) -> str:
    """`[selfnote][state] <word>` — where this work has got to.

    Appended, never edited: the sequence of notes is the history of the work,
    and the newest is where it stands.
    """
    return note(STATE_TAG, str(state))


def parse_state(content) -> str | None:
    """The state a state note names, or None for anything else."""
    value = parse_note(content, STATE_TAG)
    return value.strip().lower() if value else None


# --- reading a conversation's own notes back ------------------------------
#
# Two rules, and the difference between them is the whole model:
#
# - **identity is written once**, by the run that opened the conversation, so
#   the *earliest* note wins — the same rule `own_rootchat` follows. A later
#   note of the same kind would be a repeat, and the first one is what the
#   conversation was opened as.
# - **state and the current document change**, so the *newest* note wins.


def _earliest(messages, self_id: int, parse):
    for message in messages:
        if message.get("sender_id") != self_id:
            continue
        found = parse(message.get("content"))
        if found is not None:
            return message, found
    return None


def _newest(messages, self_id: int, parse):
    for message in reversed(list(messages)):
        if message.get("sender_id") != self_id:
            continue
        found = parse(message.get("content"))
        if found is not None:
            return found
    return None


def own_mission(messages, self_id: int) -> tuple[int, str] | None:
    """`(mission id, project slug)` this bot anchored this topic to."""
    found = _earliest(messages, self_id, parse_mission)
    if found is None:
        return None
    message, slug = found
    return int(message["id"]), slug


def own_task(messages, self_id: int) -> tuple[int, int, int] | None:
    """`(task id, mission id, serial)` this bot anchored this topic to."""
    found = _earliest(messages, self_id, parse_task)
    if found is None:
        return None
    message, (mission_id, serial) = found
    return int(message["id"]), mission_id, serial


def own_replaces(messages, self_id: int) -> int | None:
    """The anchor id of the work this conversation was opened to replace.

    Identity-shaped, so the earliest note wins: a conversation replaces one
    thing, decided when it was opened.
    """
    found = _earliest(messages, self_id, parse_replaces)
    return None if found is None else found[1]


def own_doc(messages, self_id: int) -> int | None:
    """The message id of the document this conversation currently holds."""
    return _newest(messages, self_id, parse_doc)


def own_state(messages, self_id: int) -> str | None:
    """Where this work has got to, per its newest state note.

    This bot's own notes, plus anyone's `accepted`/`done` — see
    `EXTERNAL_STATES` for why the second half is not a hole.
    """
    for message in reversed(list(messages)):
        state = parse_state(message.get("content"))
        if state is None:
            continue
        if message.get("sender_id") == self_id or state in EXTERNAL_STATES:
            return state
    return None
