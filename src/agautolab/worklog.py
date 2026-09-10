"""The mission and its tasks, kept in the Zulip conversations they happen in.

Until `refactor` p1 this was `mission.py`: a Plane mirror of one chat topic,
with the mission as a Work, each task as a Sub-Work, results as issue
comments, and the whole lifecycle expressed in Plane's state groups. The chat
carried the conversation and Plane carried the record, so understanding one
mission meant reading two systems and trusting that they still agreed.

They are one system now. **A conversation is its own record.** The plan lives
in the `workplan-` topic that asked for it; each task's executable
description lives in the `workrun-` topic that runs it; a result is posted
where the task ran. Nothing about a mission is anywhere but Zulip and Git.

## What a conversation says about itself

Prose is what a human reads, so the plan and the task descriptions are
ordinary posts. Everything a *program* has to answer without guessing is a
selfnote (`agag.selfnote`) — machine-to-machine, hidden from every chatlog,
and never counted as somebody speaking, so writing one never buys a run:

    [selfnote][mission] <project slug>      in the workplan- topic
    [selfnote][task] <mission id>#<serial>  in a workrun- topic
    [selfnote][doc] <message id>            the visible document that is current
    [selfnote][state] <state word>          the newest one wins
    [selfnote][replaces] <anchor id>        the work this one was opened to replace

## Identity is a message id, not a name

`[selfnote][mission]`'s **own message id** is the mission's identity, and
`[selfnote][task]`'s own id is the task's. A message id is the one thing in
Zulip that no rename touches: resolving a topic renames it, a human may
rename it again, a message may be moved to another channel entirely — the id
survives all of it, and `ZulipClient.message` answers with the conversation
the anchor is in *now*.

That is what separates a conversation's identity from its reusable display
name. A `workrun-task1-m5512` topic that has been retired and replaced by a
new topic of the same name is a different task, because the anchor is a
different message; and a deleted anchor is **absent** — not the new work that
happens to be called what the old work was called.

## Replacing work that re-planning cannot fix

Re-planning rewrites a mission in place: the serials keep their topics and a
completed task stays completed. `refactor` p2 adds the other move, for when
the request itself was wrong — **retire this conversation and open its
replacement**. Retiring is two Zulip operations on the old topic, and both
are deliberate: it is renamed out of the `workplan-`/`workrun-` vocabulary,
so no listener matches it any more, and it is resolved, so no sweep looks at
it at all. Its unfinished tasks are cancelled and resolved and its work
channel is archived, which is how retired work leaves the execution queue.

The replacement then takes the freed display name and says what it is:
`[selfnote][replaces] <old anchor id>`, and a visible post listing what it
carries forward (the tasks that were finished — referenced, not recreated as
finished rows) and what it drops. The link is an id because the name now
belongs to the replacement.

Retiring does not interrupt anything. The listener serves one topic at a
time, so a replacement decided while a task is running is applied after that
run returns, and a run already under way finishes against the work it
started on. Forced interruption is out of scope.

## The four states, and why they are four

`open` → `completed` → `accepted` for a task, with `cancelled` off to one
side. They are deliberately not collapsed:

- `completed` is the *run* saying it did the work and the developer agreeing
  in the conversation. It is what the next task's gate waits for.
- `accepted` is a **human** accepting the request as a whole, which happens
  in the operation room and not in the task's own topic.
- resolving the topic (Zulip's `✔ `) is neither: it closes the conversation.

A mission's own states are `planned`, `started`, `cancelled`, `replaced` and
`done`. `replaced` is not a kind of `cancelled`: the work was not called off,
it was re-asked somewhere else, and the somewhere else is discoverable from
the replacement's own `replaces` note.

## What is not here any more

Plane's `AUTO` label, the `[AUTO]` project marker as an execution filter, the
whole-board `next_work` queue, and issue states. A task runs because somebody
posts in its topic; that was already true before this phase, and the queue
had no remaining caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from agag.document import TITLE_LIMIT, DocumentError, compose, split
from agag.zulip import (
    RESOLVED_TOPIC_PREFIX,
    ZulipClient,
    ZulipError,
    live_topic_name,
    log,
    topic_history_across_resolve,
)

from .anchor import (
    doc_note,
    mission_note,
    own_doc,
    own_mission,
    own_replaces,
    own_state,
    own_task,
    replaces_note,
    state_note,
    task_note,
)

#: How much of a conversation one lookup reads. A `workrun-` topic is a
#: conversation plus its own bookkeeping; a `workplan-` topic accumulates a
#: post per planning round. Both stay far below this.
HISTORY_MESSAGES = 1000

TASK_FILE = re.compile(r"^task(?P<number>\d+)\.md$")

#: Task states. `open` is the absence of any other one, so it is never
#: written; a topic with no state note holds work nobody has finished.
TASK_OPEN = "open"
TASK_COMPLETED = "completed"
TASK_CANCELLED = "cancelled"
TASK_ACCEPTED = "accepted"
#: What satisfies the gate in front of the next task. Accepting a task is a
#: stronger statement than completing it, so it satisfies the gate too.
TASK_FINISHED = (TASK_COMPLETED, TASK_ACCEPTED)

#: Mission states.
MISSION_PLANNED = "planned"
MISSION_STARTED = "started"
MISSION_CANCELLED = "cancelled"
#: The mission was re-asked as another conversation, which names this one.
MISSION_REPLACED = "replaced"
MISSION_DONE = "done"

__all__ = [
    "HISTORY_MESSAGES",
    "MISSION_CANCELLED",
    "MISSION_DONE",
    "MISSION_PLANNED",
    "MISSION_REPLACED",
    "MISSION_STARTED",
    "Mission",
    "Replacement",
    "Task",
    "TaskChange",
    "TASK_ACCEPTED",
    "TASK_CANCELLED",
    "TASK_COMPLETED",
    "TASK_FINISHED",
    "TASK_OPEN",
    "TITLE_LIMIT",
    "WorklogError",
    "anchor_task",
    "cancel_tasks",
    "compose_document",
    "ensure_mission",
    "mission_at",
    "mission_from_anchor",
    "mission_label",
    "mission_tasks",
    "plan_changes",
    "post_document",
    "predecessor",
    "read_mission",
    "read_task",
    "record_plan",
    "record_result",
    "replace_mission",
    "replacement_summary",
    "retire_conversation",
    "retire_task",
    "retired_topic_name",
    "run_topic_name",
    "set_mission_state",
    "set_task_state",
    "split_document",
    "task_at",
    "task_files",
    "work_channel_name",
    "write_mission_workspace",
]


class WorklogError(RuntimeError):
    """A work record could not be read or written."""


# --- documents -------------------------------------------------------------
#
# `agag.document`'s rules under this module's own error, so a caller that
# already catches `WorklogError` catches an empty plan file too.


def split_document(text: str) -> tuple[str, str]:
    try:
        return split(text)
    except DocumentError as error:
        raise WorklogError(str(error)) from error


def compose_document(title: str, body: str | None) -> str:
    return compose(title, body)


def task_files(directory: Path) -> list[tuple[int, Path]]:
    """`task[N].md` files in numeric order. Anything else in the directory —
    the chatlog and the `current/` mirror included — is simply not a task."""
    tasks: list[tuple[int, Path]] = []
    if not directory.is_dir():
        return tasks
    for path in directory.iterdir():
        match = TASK_FILE.fullmatch(path.name)
        if match and path.is_file():
            tasks.append((int(match.group("number")), path))
    tasks.sort(key=lambda item: item[0])
    return tasks


# --- names -----------------------------------------------------------------
#
# A mission's label is its anchor id, which is unique by construction. The old
# labels were Plane's (`pa-12`), and a mission that was re-planned kept one
# while a mission that was replaced needed a new one nobody could mint. An id
# answers both: `work-m5512` can only ever be one mission's channel, and a
# second mission never wants that name.

WORK_CHANNEL_PREFIX = "work-"
RUN_TOPIC_PREFIX = "workrun-"


def mission_label(mission_id: int) -> str:
    """`m5512` — the mission's anchor id, worn as a name."""
    return f"m{int(mission_id)}"


def work_channel_name(mission_id: int) -> str:
    """`work-m5512` — one channel per mission."""
    return f"{WORK_CHANNEL_PREFIX}{mission_label(mission_id)}"


def run_topic_name(mission_id: int, serial: int) -> str:
    """`workrun-task3-m5512` — one topic per task serial."""
    return f"{RUN_TOPIC_PREFIX}task{int(serial)}-{mission_label(mission_id)}"


def rerun_topic_name(mission_id: int, serial: int) -> str:
    """A fresh execution surface when a completed task is changed.

    The completed topic keeps its record and its `completed` state; the
    rework gets its own topic, its own anchor and its own task identity.
    """
    return f"{RUN_TOPIC_PREFIX}rerun-task{int(serial)}-{mission_label(mission_id)}"


def bare_topic(name: str) -> str:
    """A topic name without Zulip's resolved marker."""
    return name[len(RESOLVED_TOPIC_PREFIX):] if name.startswith(RESOLVED_TOPIC_PREFIX) else name


# --- reading a conversation ------------------------------------------------


def _history(client: ZulipClient, channel: str, topic: str) -> list[dict]:
    return topic_history_across_resolve(client, channel, topic, HISTORY_MESSAGES)


def _document_from(history: list[dict], self_id: int) -> str:
    """The visible document this conversation currently holds.

    The `[selfnote][doc]` note names it by message id, so a re-plan that
    posted a new description does not leave the old one ambiguous with it,
    and a developer's own post is never mistaken for the task text.
    """
    pointer = own_doc(history, self_id)
    if pointer is None:
        return ""
    for message in history:
        if int(message.get("id", 0)) == pointer:
            return str(message.get("content") or "").strip()
    return ""


@dataclass(frozen=True)
class Mission:
    """One mission: the plan, where it lives, and what state it is in."""

    mission_id: int
    slug: str
    channel: str
    topic: str
    #: The plan as the superdirector wrote it, `# title` heading and all.
    plan: str = ""
    state: str = MISSION_PLANNED
    #: The anchor id of the mission this one was opened to replace, if any.
    #: An id and not a name, because this mission usually took that one's name.
    replaces: int | None = None

    @property
    def label(self) -> str:
        return mission_label(self.mission_id)

    @property
    def work_channel(self) -> str:
        return work_channel_name(self.mission_id)

    @property
    def title(self) -> str:
        try:
            return split_document(self.plan)[0] if self.plan else ""
        except WorklogError:
            return ""


@dataclass(frozen=True)
class Task:
    """One task: which mission and serial it is, and what state it is in."""

    task_id: int
    mission_id: int
    serial: int
    channel: str
    topic: str
    #: The executable description, `# title` heading and all.
    document: str = ""
    state: str = TASK_OPEN
    #: The anchor id of the task this one reworks, if any (a rerun topic).
    replaces: int | None = None

    @property
    def title(self) -> str:
        try:
            return split_document(self.document)[0] if self.document else ""
        except WorklogError:
            return ""

    @property
    def finished(self) -> bool:
        return self.state in TASK_FINISHED


def read_mission(client: ZulipClient, channel: str, topic: str, self_id: int) -> Mission | None:
    """The mission a `workplan-` conversation holds, or None when it holds none.

    None means *not planned yet* — a topic where somebody asked for work and
    the superdirector has not written a plan.
    """
    history = _history(client, channel, topic)
    anchor = own_mission(history, self_id)
    if anchor is None:
        return None
    mission_id, slug = anchor
    return Mission(
        mission_id,
        slug,
        channel,
        bare_topic(topic),
        _document_from(history, self_id),
        own_state(history, self_id) or MISSION_PLANNED,
        own_replaces(history, self_id),
    )


def read_task(client: ZulipClient, channel: str, topic: str, self_id: int) -> Task | None:
    """The task a `workrun-` conversation is bound to, or None for a topic
    that is not one of ours — a `workrun-` name somebody typed by hand."""
    history = _history(client, channel, topic)
    anchor = own_task(history, self_id)
    if anchor is None:
        return None
    task_id, mission_id, serial = anchor
    return Task(
        task_id,
        mission_id,
        serial,
        channel,
        bare_topic(topic),
        _document_from(history, self_id),
        own_state(history, self_id) or TASK_OPEN,
        own_replaces(history, self_id),
    )


def _conversation_of(client: ZulipClient, message_id: int) -> tuple[str, str] | None:
    """Where an anchor message is **now**, or None when it is gone.

    Deleted is absent. A caller must not fall back to a topic of the
    remembered name: that name may have been reused by work this anchor knows
    nothing about, which is the whole reason identity is an id.
    """
    message = client.message(int(message_id))
    if message is None:
        return None
    channel = message.get("display_recipient")
    topic = message.get("subject")
    if not isinstance(channel, str) or not isinstance(topic, str) or not channel or not topic:
        return None
    return channel, bare_topic(topic)


def mission_at(client: ZulipClient, mission_id: int, self_id: int) -> Mission | None:
    """Follow a mission anchor to the conversation it is in now, and read it."""
    where = _conversation_of(client, mission_id)
    if where is None:
        return None
    mission = read_mission(client, where[0], where[1], self_id)
    return mission if mission and mission.mission_id == int(mission_id) else None


#: Kept under its older name for the callers that speak of anchors.
mission_from_anchor = mission_at


def task_at(client: ZulipClient, task_id: int, self_id: int) -> Task | None:
    """Follow a task anchor to the conversation it is in now, and read it."""
    where = _conversation_of(client, task_id)
    if where is None:
        return None
    task = read_task(client, where[0], where[1], self_id)
    return task if task and task.task_id == int(task_id) else None


def mission_tasks(
    client: ZulipClient, mission: Mission, self_id: int
) -> dict[int, Task]:
    """Every task of `mission`, by serial, read from its work channel.

    The channel is walked and each `workrun-` topic in it is read; a topic's
    **note** is what says which mission and serial it belongs to, so a name
    that was reused by other work is filtered out here rather than trusted.
    The names still narrow the walk — reading a channel's unrelated topics
    would be calls spent on nothing — but they never decide.

    When two live topics claim one serial the newest anchor wins: that is a
    replacement whose predecessor has not been retired, and the newer work is
    the work.
    """
    channel = mission.work_channel
    try:
        topics = client.channel_topics(client.stream_id(channel))
    except ZulipError as error:
        log(f"could not list {channel!r}: {error!r}")
        return {}
    found: dict[int, Task] = {}
    for name in topics:
        if not bare_topic(name).startswith(RUN_TOPIC_PREFIX):
            continue
        task = read_task(client, channel, name, self_id)
        if task is None or task.mission_id != mission.mission_id:
            continue
        if task.state == TASK_CANCELLED:
            continue
        current = found.get(task.serial)
        if current is None or task.task_id > current.task_id:
            found[task.serial] = task
    return found


# --- writing ---------------------------------------------------------------


def ensure_mission(
    client: ZulipClient, channel: str, topic: str, slug: str, self_id: int
) -> Mission:
    """The mission of this `workplan-` conversation, minting its anchor once.

    Idempotent: a re-plan finds the note the first plan wrote and keeps the
    same identity, which is what lets a mission be re-planned without every
    task topic losing the mission it belongs to.
    """
    existing = read_mission(client, channel, topic, self_id)
    if existing is not None:
        return existing
    live = live_topic_name(client, channel, topic)
    anchor = client.send_to_channel(channel, live, mission_note(slug))
    return Mission(int(anchor), slug, channel, bare_topic(live))


def post_document(client: ZulipClient, channel: str, topic: str, document: str,
                  preface: str = "") -> int:
    """Post a document where it belongs and mark it as the current one.

    The visible post is what a human reads; the `[selfnote][doc]` note that
    follows it is how the next run finds it again without parsing the
    conversation. `preface` is the one sentence a planner sometimes owes the
    reader — "Updated by planner." — and is deliberately outside the document
    so the document stays exactly what was written.
    """
    live = live_topic_name(client, channel, topic)
    body = f"{preface}\n\n{document}" if preface else document
    message_id = client.send_to_channel(channel, live, body)
    client.send_to_channel(channel, live, doc_note(message_id))
    return int(message_id)


def _set_state(client: ZulipClient, channel: str, topic: str, state: str) -> str:
    live = live_topic_name(client, channel, topic)
    client.send_to_channel(channel, live, state_note(state))
    return state


def set_mission_state(client: ZulipClient, mission: Mission, state: str) -> Mission:
    _set_state(client, mission.channel, mission.topic, state)
    return replace(mission, state=state)


def set_task_state(client: ZulipClient, task: Task, state: str) -> Task:
    _set_state(client, task.channel, task.topic, state)
    return replace(task, state=state)


def anchor_task(
    client: ZulipClient, channel: str, topic: str, mission_id: int, serial: int,
    self_id: int, extra_notes: list[str] | None = None,
) -> Task:
    """Open a task's conversation by saying what it is for.

    The notes go in before the visible description, so the description stays
    the topic's last real post and opening a topic fires nothing — a selfnote
    is never somebody speaking. Idempotent by the task note: a topic already
    anchored keeps the identity it was opened with.
    """
    existing = read_task(client, channel, topic, self_id)
    if existing is not None:
        return existing
    live = live_topic_name(client, channel, topic)
    anchor = client.send_to_channel(channel, live, task_note(mission_id, serial))
    for extra in extra_notes or []:
        client.send_to_channel(channel, live, extra)
    return Task(int(anchor), int(mission_id), int(serial), channel, bare_topic(live))


def record_plan(
    client: ZulipClient, channel: str, topic: str, slug: str, plan_text: str, self_id: int
) -> tuple[str, Mission]:
    """Store this planning round's `plan.md` in its own conversation.

    Returns a report line and the mission, whose identity is minted here on
    the first round and reused on every later one.
    """
    split_document(plan_text)  # a plan with no title is not a plan
    mission = ensure_mission(client, channel, topic, slug, self_id)
    known = mission.plan.strip()
    if known == plan_text.strip():
        return f"plan for {mission.label} is unchanged", mission
    post_document(client, mission.channel, mission.topic, plan_text)
    verb = "updated" if known else "recorded"
    return f'{verb} the plan for {mission.label} "{split_document(plan_text)[0]}"', replace(
        mission, plan=plan_text
    )


def record_result(client: ZulipClient, task: Task, report: str) -> Task:
    """Post one task's result where the task ran, and mark it completed.

    The report is a visible post: it is what the requester was waiting for,
    and a result nobody can read is not a result. The state note beside it is
    what the next task's gate reads.
    """
    live = live_topic_name(client, task.channel, task.topic)
    text = report.strip()
    if text:
        client.send_to_channel(task.channel, live, f"## Result\n\n{text}")
    return set_task_state(client, task, TASK_COMPLETED)


def cancel_tasks(client: ZulipClient, tasks: dict[int, Task]) -> int:
    """Cancel every live task of a mission. Returns how many moved."""
    moved = 0
    for serial in sorted(tasks):
        task = tasks[serial]
        if task.state == TASK_CANCELLED:
            continue
        set_task_state(client, task, TASK_CANCELLED)
        moved += 1
    return moved


# --- retiring a conversation and opening its replacement -------------------
#
# Re-planning is the cheap revision and it happens in place. This is the
# expensive one: the request itself was wrong, so the conversation that
# carried it is retired and a new one is opened in its place.
#
# Retirement is not deletion and not cancellation. The retired conversation
# keeps every message it ever held and stays reachable through its anchor;
# what it gives up is its **display name** and its place in the execution
# queue.

#: What a retired conversation is renamed to. The `workplan-`/`workrun-`
#: prefix is gone, so no listener's topic filter matches it any more, and the
#: label keeps two retirements of the same name apart — a label is an anchor
#: id, so it is unique by construction.
RETIRED_TOPIC_PREFIX = "retired-"


def retired_topic_name(topic: str, label: str) -> str:
    """`retired-workplan-trend-m5512` — where a retired conversation goes."""
    return f"{RETIRED_TOPIC_PREFIX}{bare_topic(topic)}-{label}"


def retire_conversation(client: ZulipClient, channel: str, topic: str, label: str) -> str:
    """Rename a conversation out of the queue and resolve it. Returns its name.

    One rename does both, because Zulip's resolve *is* a rename: the new name
    is `\u2714 retired-<old name>-<label>`. Two things are true of it and both
    are needed —

    - it no longer starts with a prefix any listener sweeps, so even an
      unresolved read of it matches nothing;
    - it is resolved, so `sweep_topics` never even reads it.

    Renaming also **releases the old display name**, which the replacement
    then takes. That ordering is not a preference: Zulip has one topic per
    name in a channel, so a replacement created first would merge into the
    conversation it was meant to replace.

    A conversation with no messages cannot be renamed — there is nothing to
    PATCH — and is already nothing to serve, so its name is returned unchanged.
    """
    live = live_topic_name(client, channel, topic)
    try:
        tail = client.topic_history(channel, live, num_before=1)
    except ZulipError as error:
        raise WorklogError(f"could not read {channel}/{live} to retire it: {error}") from error
    if not tail:
        return live
    retired = retired_topic_name(topic, label)
    if not retired.startswith(RESOLVED_TOPIC_PREFIX):
        retired = f"{RESOLVED_TOPIC_PREFIX}{retired}"
    client.rename_topic(int(tail[-1]["id"]), retired)
    return retired


RETIRED_BY_REPLACEMENT = (
    "This task was not finished when its mission was replaced, so it is "
    "cancelled here. The replacement conversation says what carries forward."
)


def retire_task(client: ZulipClient, task: Task) -> str:
    """Take one unfinished task out of the queue. One report line.

    Cancelled and resolved, which is the same retirement the planner already
    performs on a task a re-plan dropped. The topic is *not* renamed: a task
    topic's name is minted from its mission's anchor id, so no later work can
    ever want it, and a late callback still has to find this conversation
    under the name it was delegated from.
    """
    live = live_topic_name(client, task.channel, task.topic)
    message_id = client.send_to_channel(task.channel, live, RETIRED_BY_REPLACEMENT)
    set_task_state(client, task, TASK_CANCELLED)
    client.resolve_topic(int(message_id), live)
    return f"cancelled and resolved {task.channel}/{task.topic}"


@dataclass(frozen=True)
class Replacement:
    """One mission retired, one opened in its place, and what moved."""

    retired: Mission
    #: Where the retired conversation is now — its resolved, renamed topic.
    retired_topic: str
    #: The new mission. Its `replaces` names `retired.mission_id`.
    mission: Mission
    #: Tasks that were finished and are referenced rather than repeated.
    carried: tuple[Task, ...] = ()
    #: Tasks that were unfinished and were cancelled with the old mission.
    dropped: tuple[Task, ...] = ()


def _task_reference(task: Task) -> str:
    title = task.title or f"task {task.serial}"
    return f"- task {task.serial} \u201c{title}\u201d \u2014 {task.channel}/{task.topic}"


def replacement_summary(replacement: Replacement, reason: str = "") -> str:
    """The visible post that says what a replacement carries and what it drops.

    Prose, because it is what a human reads to understand why the topic they
    were talking in became a different mission. The machine-readable half is
    the `[selfnote][replaces]` note; this is the half with the reasons in it.
    """
    retired = replacement.retired
    lines = [
        f"# Replacing mission {retired.label}",
        "",
        f"The previous plan for this request is retired. Its conversation is "
        f"`{retired.channel}/{replacement.retired_topic}` and its work channel "
        f"`{retired.work_channel}` is archived; this topic is now mission "
        f"{replacement.mission.label}.",
    ]
    if reason.strip():
        lines += ["", reason.strip()]
    if replacement.carried:
        lines += [
            "",
            "**Carried forward.** These tasks were finished before the "
            "replacement and are not repeated here; the new plan should build "
            "on them rather than re-ask for them:",
            *[_task_reference(task) for task in replacement.carried],
        ]
    else:
        lines += ["", "**Carried forward.** Nothing: no task of the retired "
                  "mission had been finished."]
    if replacement.dropped:
        lines += [
            "",
            "**Dropped.** These tasks were unfinished and were cancelled with "
            "the mission they belonged to:",
            *[_task_reference(task) for task in replacement.dropped],
        ]
    return "\n".join(lines)


def replace_mission(
    client: ZulipClient, mission: Mission, self_id: int, reason: str = "",
    mention: str = "",
) -> Replacement:
    """Retire `mission` and open its replacement under the name it frees.

    In this order, and the order is the workflow:

    1. every unfinished task is cancelled and resolved, and the finished ones
       are read out to be carried forward by reference;
    2. the mission is marked `replaced` and its work channel archived — from
       here nothing of the old mission is in anybody's queue;
    3. its conversation is renamed out of the `workplan-` vocabulary and
       resolved, which releases the display name;
    4. the replacement is opened under that freed name, with its own mission
       anchor, a `[selfnote][replaces]` note naming the retired anchor, and a
       visible post saying what it carries and what it drops.

    The caller then plans into the returned mission's conversation exactly as
    it would into a fresh one: it is a fresh one, with a predecessor.
    """
    tasks = mission_tasks(client, mission, self_id)
    carried = tuple(tasks[serial] for serial in sorted(tasks) if tasks[serial].finished)
    dropped = tuple(
        tasks[serial] for serial in sorted(tasks)
        if not tasks[serial].finished and tasks[serial].state != TASK_CANCELLED
    )
    for task in dropped:
        retire_task(client, task)
    set_mission_state(client, mission, MISSION_REPLACED)
    archived = _archive_work_channel(client, mission)
    log(f"retiring mission {mission.label}: {archived}")

    freed = mission.topic
    retired_topic = retire_conversation(client, mission.channel, mission.topic, mission.label)

    anchor = client.send_to_channel(mission.channel, freed, mission_note(mission.slug))
    client.send_to_channel(mission.channel, freed, replaces_note(mission.mission_id))
    replacement = Mission(
        int(anchor), mission.slug, mission.channel, freed,
        state=MISSION_PLANNED, replaces=mission.mission_id,
    )
    result = Replacement(mission, retired_topic, replacement, carried, dropped)
    summary = replacement_summary(result, reason)
    client.send_to_channel(
        mission.channel, freed, f"{mention}\n\n{summary}" if mention else summary
    )
    return result


def _archive_work_channel(client: ZulipClient, mission: Mission) -> str:
    """Archive a retired mission's `work-` channel, if it has one.

    An archived channel leaves every subscription list, so its `workrun-`
    topics leave the sweep whether or not each one was resolved. That is the
    coarse half of "retired work leaves the execution queue"; cancelling the
    tasks first is the half that is still true if the archive fails.
    """
    name = mission.work_channel
    try:
        existing = next(
            (row for row in client.channels() if str(row.get("name")) == name), None
        )
    except ZulipError as error:
        return f"could not look for {name}: {error}"
    if not existing or existing.get("stream_id") is None:
        return f"no {name} channel to archive"
    try:
        client.archive_channel(int(existing["stream_id"]))
    except ZulipError as error:
        return f"could not archive {name}: {error}"
    return f"archived {name}"


def predecessor(client: ZulipClient, mission: Mission, self_id: int) -> Mission | None:
    """The mission this one replaced, or None.

    None covers both "this mission replaced nothing" and "the mission it
    replaced has been deleted". A caller must not fall back to the topic name
    for the second case: this mission is very probably wearing that name.
    """
    if mission.replaces is None:
        return None
    return mission_at(client, mission.replaces, self_id)


# --- reconciling a fresh plan against the tasks that exist -----------------


@dataclass(frozen=True)
class TaskChange:
    """What one planning round does to one task serial.

    `document` is the task as the developer should read it — the same
    `# title\\n\\nbody` shape the superdirector wrote.
    """

    serial: int
    #: `created` | `updated` | `unchanged` | `cancelled` | `changed-after-done`
    action: str
    title: str
    document: str
    #: The task this change is about, when one already exists.
    task: Task | None = None


def plan_changes(existing: dict[int, Task], plan_dir: Path) -> list[TaskChange]:
    """Compare the `task[N].md` set against the tasks that exist. Pure.

    Matching is by **serial**:

    - the serial exists → its description is updated in place and its
      **state is left alone**, which is what keeps a completed task completed
      and the gate in front of the next one meaningful;
    - the serial is new → a task is created for it;
    - the serial disappeared from the split → its task is cancelled.

    A serial whose task is already completed and whose text changed is
    `changed-after-done`: the planning conversation deciding that finished
    work must happen again. It gets a fresh conversation of its own rather
    than reopening a record.
    """
    changes: list[TaskChange] = []
    seen: set[int] = set()
    for number, path in task_files(plan_dir):
        seen.add(number)
        title, body = split_document(path.read_text(encoding="utf-8"))
        document = compose_document(title, body)
        task = existing.get(number)
        if task is None:
            action = "created"
        elif task.document.strip() == document.strip():
            action = "unchanged"
        elif task.state in TASK_FINISHED:
            action = "changed-after-done"
        else:
            action = "updated"
        changes.append(TaskChange(number, action, title, document, task))
    for serial in sorted(set(existing) - seen):
        task = existing[serial]
        changes.append(TaskChange(serial, "cancelled", task.title, "", task))
    return changes


# --- the read-back the planner is given ------------------------------------


def write_mission_workspace(
    client: ZulipClient, directory: Path, channel: str, topic: str, self_id: int
) -> bool:
    """Mirror what is currently registered into `directory` as files.

    `mission.md` is the plan as it stands and `task1.md`, `task2.md`, … are
    the live tasks, so a re-planning run reads back exactly what it wrote
    last time — out of the conversations, which are now the only copy.
    Returns whether anything was written.
    """
    mission = read_mission(client, channel, topic, self_id)
    if mission is None or not mission.plan.strip():
        return False
    (directory / "mission.md").write_text(mission.plan, encoding="utf-8")
    tasks = mission_tasks(client, mission, self_id)
    for serial in sorted(tasks):
        task = tasks[serial]
        (directory / f"task{serial}.md").write_text(
            task.document or compose_document(f"task {serial}", ""), encoding="utf-8"
        )
    return True


# --- the gate in front of one task ----------------------------------------


@dataclass(frozen=True)
class RunTarget:
    """The task a `workrun-` serving runs, and the gate in front of it.

    `blocked_by` names the immediately preceding task when that one is not
    finished — the handler answers with it and never launches an agent.
    Serial 1 has no predecessor and is never blocked.

    `mission` comes along for the devlog: a task's record is filed under its
    mission, and the directory is named from both.
    """

    task: Task
    mission: Mission
    blocked_by: str | None = None


def run_target(client: ZulipClient, task: Task, self_id: int) -> RunTarget:
    """Resolve one task to its mission and decide whether it may run.

    The mission is found through the task's **anchor id**, so a `workplan-`
    topic that has since been renamed, resolved or moved is still found, and
    one that has been deleted is honestly missing.
    """
    mission = mission_at(client, task.mission_id, self_id)
    if mission is None:
        raise WorklogError(
            f"the mission this task belongs to (message {task.mission_id}) no longer "
            "exists, so there is nothing to run against"
        )
    if task.state == TASK_CANCELLED:
        raise WorklogError(f"task {task.serial} of {mission.label} is cancelled")
    blocked_by = None
    if task.serial > 1:
        previous = mission_tasks(client, mission, self_id).get(task.serial - 1)
        if previous is not None and not previous.finished:
            blocked_by = f"task {previous.serial} of {mission.label}"
    return RunTarget(task, mission, blocked_by)
