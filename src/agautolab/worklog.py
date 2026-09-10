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

## The four states, and why they are four

`open` → `completed` → `accepted` for a task, with `cancelled` off to one
side. They are deliberately not collapsed:

- `completed` is the *run* saying it did the work and the developer agreeing
  in the conversation. It is what the next task's gate waits for.
- `accepted` is a **human** accepting the request as a whole, which happens
  in the operation room and not in the task's own topic.
- resolving the topic (Zulip's `✔ `) is neither: it closes the conversation.

A mission's own states are `planned`, `started`, `cancelled` and `done`.

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
    own_state,
    own_task,
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
MISSION_DONE = "done"

__all__ = [
    "HISTORY_MESSAGES",
    "MISSION_CANCELLED",
    "MISSION_DONE",
    "MISSION_PLANNED",
    "MISSION_STARTED",
    "Mission",
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
    "read_mission",
    "read_task",
    "record_plan",
    "record_result",
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
