"""autolab's mission logic: `workplan-`, `workrun-` and `bmining-` topics.

The listener itself is `agautolab.listener` — `agag.agent.listener_main`
over autolab's `SPEC`, which sweeps, routes by prefix to the handlers here,
answers the instance's own channel through `agag.entrance`, and brings a
task back through `handle_mention` when an answer names this instance.
`agag.topics.serve_topic` serves each topic — the skeleton shared with the
other agents: ack, generation workspace, chatlog, the steps, always reply
naming the failed step, then re-check for human posts that arrived during
the run.

Each serving cuts a new generation directory `<N>/`. Before that, one stable
directory was reused forever, so a continued conversation ran on top of the
previous run's leftovers. `N` is the workspace guard only: a task is one per
serial for the life of the mission, so a re-plan rewrites the task behind a
serial instead of cancelling a generation and minting a new one — which is
what lets a completed task stay completed.

Planning also builds the surfaces the work is then done on: a `work-m<id>`
channel per mission, holding one `workrun-task<N>-m<id>` topic per task, in
the folder of the project's own `pj-` channel and with its subscribers.

**Since `refactor` p1 the conversations are the whole record.** There is no
Plane Work and no Sub-Work: the plan is a post in the `workplan-` topic, each
task's executable description is a post in its `workrun-` topic, and a result
is posted where the task ran. What a program must answer without guessing —
which mission, which serial, which post is the current document, what state
the work is in — is a selfnote (`anchor.py`), and a mission's identity is the
message id of its own note, so no name decides anything. `agautolab.worklog`
holds the model and the reasoning.

A `workrun-` topic still **says what it is for**: it is opened with a
`[selfnote][rootchat]` note naming the mission conversation and a
`[selfnote][task]` note naming the mission and the serial, before the visible
task description, so the description stays the topic's last real post and
opening a topic fires nothing.

The same phase moved delegation onto the chat. A task that asks another agent
posts, ends, and is served again when the answer names this instance — and
that serving **replies at home**, in the task's own topic, with the topic
that answered placed beside the chatlog as a thread. Speaking to the other
agent is a deliberate `agentchat send` inside the run. There is no
participation ledger; the memory is the root note `agentchat send` wrote, and
a callback that has been answered is marked with a `[selfnote][served]` note
so a listener restart does not run the supercoder again.

The superdirector serves the topic alone — there is no front relay. It runs
in the persistent project folder, where `main/`, `direction/` and `devlog/`
are real directories (symlinking them into the workspace was tried first, and
harness file tools do not follow directory symlinks), and the serving's own
generation workspace is handed to it by absolute path. The chatlog and the
read-back of what is currently registered are read from there, and `plan.md`,
the task split and the flags are written back there, so
everything one run wrote stays behind in its own generation as evidence and
can never be acted on twice.
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from agag.agent import SWEEP_ACK as ACK_TEXT, exec_options_for
from agag.entrance import EMPTY_REPLY, NO_ANSWER as NO_CLOSING_MESSAGE, handle_entrance
from agag.execopt import Selection, exec_note
from agag.intro import agents_file_path, write_agents_md
from agag.topics import (
    TopicContext,
    TopicResult,
    chatlog_path,
    format_chatlog,
    generation_dir,
    guide as shared_guide,
    next_generation,
    next_record_path,
    prompt_with_guide,
    serve_topic,
    threads_placement,
    topic_workspace,
    write_threads,
)
from agag.zulip import (
    ZulipClient,
    ZulipError,
    live_topic_name,
    log,
    note_served,
    remotes_for_home,
    rootchat_home,
    topic_write,
)

from .anchor import Conversation, own_rootchat, rootchat_note

from .worklog import (
    MISSION_CANCELLED,
    MISSION_STARTED,
    Mission,
    RunTarget,
    Task,
    TaskChange,
    WorklogError,
    anchor_task,
    cancel_tasks,
    compose_document,
    mission_tasks,
    plan_changes,
    post_document,
    read_mission,
    read_task,
    record_plan,
    record_result,
    rerun_topic_name,
    run_target,
    run_topic_name,
    set_mission_state,
    set_task_state,
    split_document,
    work_channel_name,
    write_mission_workspace,
)
from .project_init import (
    AUTO_MARKER,
    PROJECT_NAME,
    PROJECTS_ROOT,
    commit_all_and_push,
    init_project,
    load_gitea_config,
    push_main_repository,
)
from .instance import (
    AGAUTOLAB_ROOT,
    BMINING_TOPIC_PREFIX,
    PROJECT_CHANNEL_PREFIX,
    PROVISIONER_ENV,
    SPEC,
    WORKPLAN_TOPIC_PREFIX,
    WORKRUN_TOPIC_PREFIX,
)
from .role_run import run_role

# The skeleton's paths, named here so a test can point a serving elsewhere.
ZULIP_ENV = SPEC.zulip_env
TOPICS_ROOT = SPEC.topics_root
GUIDES = SPEC.guides
RECORDS_ROOT = SPEC.records_root
# One channel per mission, named after the mission's anchor id: `work-m5512`.
# Its `workrun-task<N>-m5512` topics are one conversation per task. The names
# are `agautolab.worklog`'s, because the id they carry is that module's model
# of what a mission is.
HISTORY_MESSAGES = 1000

# The channel description carries the binding in human-readable form —
# `project: …; mission: …` — because somebody opening `work-m5512` should be
# able to see what it is for. The code reads the topics' own selfnotes
# instead (`anchor.py`), so nothing parses this back.

# `ACK_TEXT`, `EMPTY_REPLY` and `NO_CLOSING_MESSAGE` are the skeleton's: the
# ack that makes this bot the last poster while a run is in flight, the reply
# to a topic with nothing in it, and what a run that ended without a closing
# message contributes to its report (a topic that got only an ack and then
# silence would otherwise drop out of the sweep until a human posts again).

# The planning round's files, all of them in the serving's own generation
# workspace. The read-back of what is currently registered lives in `current/`
# inside that workspace so its `task1.md`, `task2.md`, … can never be mistaken
# for a task split the superdirector wrote this run.
PLAN_FILE = "plan.md"
CURRENT_DIR = "current"

# How much of a mission title the devlog directory name keeps. Long enough to
# recognise, short enough to stay one readable path component.
MISSION_DIR_TITLE_CHARS = 48

CHATLOG_FILE = "chatlog.md"

# One topic occupies the listener for at most this long; the sweep loop is
# single-threaded and serial, so this is also the delay before the next
# matching topic is looked at (events keep queueing meanwhile).
#
# 1200 again since `agent_standardize` p7. p6 raised it to 3600 on the p5
# precedent, reasoning that a delegating task waits out forge's whole path.
# It was never the binding constraint — the supercoder that failed used 254 s
# of the 3600 and ended its own run on purpose. A delegating task now posts,
# finishes, and is served again when the answer names this instance, so no
# single run has anybody to wait for.
WORK_TIMEOUT_SECONDS = 1200
# The superdirector reads the whole project — `main/`, `direction/` and
# `devlog/` — and the chatlog before it plans. It does not wait on anyone, so
# it keeps the pre-p6 ceiling.
SUPERDIRECTOR_TIMEOUT_SECONDS = 1200
# The director reads the whole direction clone and records notes into it. Like
# the superdirector it waits on nobody, so it keeps the pre-p6 ceiling.
DIRECTOR_TIMEOUT_SECONDS = SUPERDIRECTOR_TIMEOUT_SECONDS

__all__ = [
    "RunProgress",
    "TopicContext",
    "archive_work_channel",
    "bmining_prompt",
    "bmining_work_directory",
    "direction_directory",
    "ensure_work_channel",
    "find_channel",
    "format_chatlog",
    "guide",
    "handle_bmining",
    "handle_mention",
    "handle_workrun",
    "handle_superdirector_response",
    "handle_topic",
    "devlog_directory",
    "live_topic_name",
    "mirror_task_changes",
    "mission_directory",
    "prepare_run_surfaces",
    "run_binding",
    "progress_line",
    "workrun_supercoder",
    "run_topic",
    "run_target",
    "project_channel",
    "project_directory",
    "record_task_in_devlog",
    "run_director",
    "run_superdirector",
    "superdirector_prompt",
    "serve_bmining",
    "serve",
    "serve_run",
    "supercoder_prompt",
    "title_slug",
    "work_channel",
    "work_channel_description",
    "rerun_topic",
]


class ListenerError(RuntimeError):
    """One workplan-topic workflow could not complete."""


def project_from_channel(channel: str) -> str:
    if not channel.startswith(PROJECT_CHANNEL_PREFIX):
        raise ListenerError(
            f"workplan topic is not in a {PROJECT_CHANNEL_PREFIX} channel: {channel}"
        )
    project = channel.removeprefix(PROJECT_CHANNEL_PREFIX)
    if not PROJECT_NAME.fullmatch(project):
        raise ListenerError(f"channel does not contain a valid project name: {channel}")
    return project


def guide(*parts: str) -> str:
    return shared_guide(GUIDES, *parts)


def superdirector_prompt(bot_name: str, workspace: Path, current_files: bool) -> str:
    """The placement lines, then the guide: read from and write to the
    workspace by absolute path, work in the project itself."""
    lines = [
        f'The conversation with the requester ("{CHATLOG_FILE}") is placed in '
        f'"{workspace}". You are {bot_name!r} in the chatlog.',
        f'The other agents\' own introductions are placed in '
        f'"{agents_file_path(workspace)}".',
    ]
    if current_files:
        lines.append(
            "The currently registered mission and tasks are placed in "
            f'"{workspace / CURRENT_DIR}".'
        )
    lines.append(
        f'Write every file this guide asks for — "{PLAN_FILE}", "task[N].md", '
        f'the flags — into "{workspace}".'
    )
    lines.append("Your working directory is the project itself.")
    return prompt_with_guide(
        lines, guide("workplan_superdirector", "guide.md")
    )


def serve(context) -> TopicResult:
    """agautolab's part of one serving: project setup, the read-back of what
    is currently registered, and one superdirector run in the project folder,
    reading from and writing to the serving's generation workspace by
    absolute path.

    `handle_superdirector_response` then acts on what the superdirector
    *wrote* — its answer is relayed verbatim and never parsed. A run that
    wrote nothing changed nothing: the reply (a question, usually) is the
    whole outcome.
    """
    project = project_from_channel(context.channel)
    number = next_generation(topic_workspace(TOPICS_ROOT, context.channel, context.topic))
    workspace = generation_dir(TOPICS_ROOT, context.channel, context.topic, number, "superdirector")
    chatlog_path(workspace).write_text(
        format_chatlog(context.history, context.self_id), encoding="utf-8"
    )

    context.step = "harvest"
    write_agents_md(context.client, workspace)

    context.step = "project setup"
    init_project(project)

    context.step = "channel folder"
    file_project_channel(project)

    context.step = "read-back"
    current = workspace / CURRENT_DIR
    current.mkdir(exist_ok=True)
    current_files = write_mission_workspace(
        context.client, current, context.channel, context.topic, context.self_id
    )
    if not current_files:
        current.rmdir()

    context.step = "superdirector"
    sections = [
        run_superdirector(
            superdirector_prompt(context.bot_name, workspace, current_files),
            project_directory(project),
            conversation=(context.channel, context.topic),
            selection=context.selection,
        )
    ]

    context.step = "response handling"
    response_sections, resolve_after = handle_superdirector_response(
        context.client, context.channel, context.topic, project, workspace,
        context.self_id, context.selection,
    )
    sections.extend(response_sections)
    return TopicResult(sections, resolve_after=resolve_after)


def at_the_entrance(client: ZulipClient, channel: str, topic: str) -> bool:
    """A prefixed topic in this instance's own channel is a question, not work.

    The skeleton routes by prefix first, which is right for an agent whose
    requests live in its own channel (forge). autolab's do not: the channel
    is what says which project work is for, and the entrance is not one. So
    a `workplan-`/`workrun-`/`bmining-` name there is answered by the
    entrance like any other topic, and nothing runs.
    """
    if channel != SPEC.instance_name():
        return False
    handle_entrance(SPEC, client, channel, topic)
    return True


def in_project_channel(channel: str, topic: str) -> bool:
    """`workplan-` and `bmining-` topics need a `pj-*` channel.

    Elsewhere they are ignored silently: with `#general` swept, a stray
    `workplan-` topic there would otherwise get an error posted into it on
    every sweep. (`workrun-` topics come from anywhere; `serve_run` decides
    whether one is bound to a task.)
    """
    if channel.startswith(PROJECT_CHANNEL_PREFIX):
        return True
    log(f"ignoring {topic!r}: {channel!r} is not a project channel")
    return False


def handle_topic(client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one awaiting workplan topic through the shared skeleton."""
    if at_the_entrance(client, channel, topic) or not in_project_channel(channel, topic):
        return
    log(f"workplan topic {channel!r}/{topic!r}")
    serve_topic(client, channel, topic, serve, ack_text=ACK_TEXT, empty_reply=EMPTY_REPLY,
                exec_options=exec_options_for(SPEC, client))


def project_directory(project: str) -> Path:
    """`.local/projects/<slug>/` — the folder holding `main/`, `direction/`
    and `devlog/`, which `init_project` clones and every later serving reuses."""
    return PROJECTS_ROOT / project


def conversation_meta(conversation: tuple[str, str] | None) -> dict | None:
    """`channel`/`topic` for the run record of a run that happens *outside*
    its conversation's workspace — autolab's roles run in the project clone,
    so `agag.topics.workspace_identity` finds nothing in their cwd and the
    cost gauge would have to guess from mtimes."""
    if conversation is None:
        return None
    channel, topic = conversation
    return {"channel": channel, "topic": topic}


def run_superdirector(prompt: str, cwd: Path,
                      conversation: tuple[str, str] | None = None,
                      selection: Selection | None = None) -> str:
    """One mission-planning run in the project folder, with its record.

    Planning a mission means weighing the chatlog against the code, the
    direction documents and the devlog, so the run happens where the clones
    are real directories; the chatlog and its outputs travel by absolute
    workspace path in the prompt.
    """
    record = next_record_path(RECORDS_ROOT / "superdirector")
    output, _, exit_code = run_role(
        "superdirector",
        prompt,
        cwd=cwd,
        timeout=SUPERDIRECTOR_TIMEOUT_SECONDS,
        record=record,
        selection=selection,
        extra_meta=conversation_meta(conversation),
    )
    if exit_code != 0:
        raise ListenerError(f"superdirector run exited {exit_code}: {output.strip()[:500]}")
    # A planning run's outcome is `plan.md` and the flags it wrote, which
    # `handle_superdirector_response` reads next; the answer is only the
    # covering note, so its absence is reported, not raised.
    return output.strip() or NO_CLOSING_MESSAGE


# --- the mission's run surfaces --------------------------------------------
#
# Planning a mission builds the surfaces the work is then done on: one
# `work-m<id>` channel per mission, and one `workrun-task<N>-m<id>` topic
# in it per task. The autolab bot posts the task content itself and is
# therefore the topic's last poster, which keeps the sweep quiet — the topic
# waits, by design, until a human posts into it.

UPDATED_BY_PLANNER = "Updated by planner."
CANCELLED_BY_PLANNER = "Cancelled by planner."
CHANGED_AFTER_DONE = (
    "This task was changed by the planner after it had been completed. This "
    "fresh topic is the approved rework; post here to start it."
)
#: A plan with no task file runs nothing, and saying so is the whole answer.
#: Seen live 2026-09-08 (workplan-trend7): the mission was reported started,
#: nothing could run, and the requester had to ask for a re-plan.
NO_TASK_FILES = (
    "the superdirector wrote no task files, so the mission has no task and "
    "nothing can run: a mission runs only through its `task[N].md` files, and "
    "a single-task mission still needs `task1.md`. Re-plan with the task "
    "file(s) written."
)


def work_channel(mission: Mission) -> str:
    """`work-m5512` — one channel per mission, named by its anchor id."""
    return work_channel_name(mission.mission_id)


def run_topic(mission: Mission, serial: int) -> str:
    """`workrun-task3-m5512` — one topic per task serial."""
    return run_topic_name(mission.mission_id, serial)


def rerun_topic(mission: Mission, serial: int) -> str:
    """A fresh execution surface when a completed task is changed.

    The completed topic keeps its record and its `completed` state; the
    rework gets its own topic, its own anchor and its own task identity, so
    a finished task is never reopened and a rework is never mistaken for it.
    """
    return rerun_topic_name(mission.mission_id, serial)


def work_channel_description(mission: Mission) -> str:
    """What a human opening `work-m5512` should be able to read.

    The channel name gives back the mission's anchor id and nothing else, so
    the project and the conversation that planned it travel here. `[AUTO]`
    marks the channel as one this system made. Nothing parses this back: the
    topics say what they are (`anchor.py`).
    """
    return (
        f"{AUTO_MARKER} project: {mission.slug}; "
        f"mission: {mission.channel}/{mission.topic}"
    )


def find_channel(client: ZulipClient, name: str) -> dict | None:
    return next((row for row in client.channels() if str(row.get("name")) == name), None)


def project_folder_description(slug: str) -> str:
    return f"{slug} project channel and its work channels"


def admin_client() -> ZulipClient | None:
    """The realm-administrator credential this node was provisioned with.

    A `pj-` channel is opened by a human, and Zulip lets only its creator or
    an organization administrator administer it — this bot may create and
    file its own `work-` channels but not move the project channel they
    inherit from. `agag provision` already hands autolab the provisioner
    path for the same reason. A node without the file is not equipped, and
    the serving goes on: filing is housekeeping, not the mission.
    """
    return ZulipClient.from_env(PROVISIONER_ENV) if PROVISIONER_ENV.is_file() else None


def ensure_project_folder(client: ZulipClient, slug: str) -> int | None:
    """File `pj-<slug>` in the channel folder of the same name, minting it.

    One folder per project is the standard, and the folder is derived from
    the channel's name, so nobody has to decide anything — including the
    human who opened the channel. Idempotent, and run on every serving of a
    project channel (beside `init_project`), because that is what makes it
    self-healing: a channel filed wrongly by hand is re-filed on its next
    post, and every `work-` channel opened after that inherits the right
    folder. Before this ran on every serving, one mis-filed project channel
    carried twenty work channels into the wrong folder with it.

    Returns the folder id, or None when the channel is not visible.
    """
    name = project_channel(slug)
    channel = find_channel(client, name)
    if channel is None or channel.get("stream_id") is None:
        return None
    folder = client.channel_folder_by_name(name)
    folder_id = (
        int(folder["id"]) if folder
        else client.create_channel_folder(name, project_folder_description(slug))
    )
    current = channel.get("folder_id")
    if current is None or int(current) != folder_id:
        client.set_channel_folder(int(channel["stream_id"]), folder_id)
    return folder_id


def file_project_channel(slug: str) -> int | None:
    admin = admin_client()
    return ensure_project_folder(admin, slug) if admin is not None else None


def ensure_work_channel(client: ZulipClient, mission: Mission) -> str:
    """Create (or re-join) the mission's `work-` channel and return its name.

    Its members are the parent `pj-` channel's subscribers, so the developer
    and this bot are both in it without anyone deciding again who the work
    goes to. Its folder is the parent channel's folder — whatever that is,
    including none: this is not the place to invent a folder structure.

    `create_channel` is subscribe-based and therefore idempotent, which is
    what makes re-planning safe.
    """
    name = work_channel(mission)
    parent = find_channel(client, project_channel(mission.slug))
    principals: list[int] = []
    folder_id = None
    if parent and parent.get("stream_id") is not None:
        principals = client.channel_subscribers(int(parent["stream_id"]))
        raw_folder = parent.get("folder_id")
        folder_id = int(raw_folder) if raw_folder is not None else None
    client.create_channel(
        name,
        work_channel_description(mission),
        principals,
        folder_id=folder_id,
    )
    return name


def anchor_run_topic(
    client: ZulipClient,
    channel: str,
    topic: str,
    mission: Mission,
    serial: int,
    self_id: int,
    selection: Selection | None = None,
) -> Task:
    """Write the selfnotes that say what this `workrun-` topic is for.

    Before the visible task description, so the description stays the topic's
    last real post and opening a topic fires nothing — a selfnote is never
    somebody speaking. `agautolab.anchor` has the shapes and the reasoning.

    Three notes. `[rootchat]` names the mission conversation, the ordinary
    note every agent writes when it speaks somewhere on behalf of one of its
    own conversations; it is what routes a callback home. `[task]` names the
    mission's anchor id and this topic's serial, and **its own message id is
    this task** — which is why it is returned rather than recomputed.

    The third exists when the mission conversation had an execution option:
    `[selfnote][exec]`, a **snapshot** of what the plan was running under
    (`ag.exec-options.v1` §5). A snapshot, not a reference — a child's work
    is already under way, so a later change in the plan reaches the *next*
    task topic and not this one — and a child overrides it by carrying its
    own command, which is newer and wins by message order alone.

    Idempotent by the task note: a topic already anchored is returned as it
    stands, so a re-plan that re-creates a serial does not write a second set.
    """
    conversation = Conversation(mission.channel, mission.topic)
    extra = [rootchat_note(conversation)]
    if selection is not None and selection.explicit:
        extra.append(exec_note(selection.option, conversation, selection.message_id))
    return anchor_task(
        client, channel, topic, mission.mission_id, serial, self_id, extra_notes=extra
    )


def mirror_task_changes(
    client: ZulipClient, channel: str, mission: Mission, changes: list[TaskChange],
    self_id: int, selection: Selection | None = None,
) -> list[str]:
    """Mirror one planning round onto the mission's `workrun-` topics.

    Created tasks get a topic, updated live tasks get the new description
    posted, and cancelled tasks are told, marked cancelled and resolved. A
    task changed after completion gets a distinct, freshly anchored rerun
    topic: the planning conversation is deciding that finished work must
    happen again, while a human post in the new topic still starts it.
    Unchanged tasks are left silent, so a re-plan that only touched task 3
    does not disturb tasks 1 and 2.

    `selection` is what the planning serving was running under, snapshotted
    into every topic this opens. A re-plan therefore hands its *current*
    selection to the tasks it creates now, and leaves the ones it created
    before with what they were opened with.
    """
    lines: list[str] = []
    for change in changes:
        if change.action == "created":
            topic = run_topic(mission, change.serial)
            anchor_run_topic(client, channel, topic, mission, change.serial, self_id, selection)
            post_document(client, channel, topic, change.document)
            # Saying where is not enough: agforge learned in p8 to say that
            # posting there is what starts it, and p9 watched a supervisor
            # read "opened work-…/workrun-task1-…" as "it is running now" and
            # then wait for a task nobody had triggered.
            lines.append(f"opened {channel}/{topic}; post there to start it")
        elif change.action == "updated" and change.task is not None:
            post_document(
                client, change.task.channel, change.task.topic, change.document,
                preface=UPDATED_BY_PLANNER,
            )
            lines.append(f"updated {change.task.channel}/{change.task.topic}")
        elif change.action == "cancelled" and change.task is not None:
            task = change.task
            message_id = client.send_to_channel(
                task.channel, live_topic_name(client, task.channel, task.topic),
                CANCELLED_BY_PLANNER,
            )
            set_task_state(client, task, "cancelled")
            client.resolve_topic(int(message_id), task.topic)
            lines.append(f"cancelled and resolved {task.channel}/{task.topic}")
        elif change.action == "changed-after-done":
            redo = rerun_topic(mission, change.serial)
            anchor_run_topic(client, channel, redo, mission, change.serial, self_id, selection)
            post_document(client, channel, redo, change.document, preface=CHANGED_AFTER_DONE)
            lines.append(f"opened {channel}/{redo}; post there to start the rework")
    return lines


def prepare_run_surfaces(
    client: ZulipClient, mission: Mission, changes: list[TaskChange],
    self_id: int, selection: Selection | None = None,
) -> list[str]:
    """The whole Zulip side of one planning round.

    Every task topic this opens is anchored back to the mission — that pair
    of notes is what a serving reads its project and its task off, and what
    the channel description used to carry alone.
    """
    name = ensure_work_channel(client, mission)
    return [
        f"work channel {name} is ready",
        *mirror_task_changes(client, name, mission, changes, self_id, selection),
    ]


def archive_work_channel(client: ZulipClient, mission: Mission) -> str:
    """Retire a cancelled mission's channel. One report line.

    Mission cancel is the only path that ever gets here and nothing is ever
    re-created after it. The channel's retained name cannot collide with a
    later one either, because a mission's name is its anchor id and no second
    mission has that id.
    """
    name = work_channel(mission)
    existing = find_channel(client, name)
    if not existing or existing.get("stream_id") is None:
        return f"no {name} channel to archive"
    client.archive_channel(int(existing["stream_id"]))
    return f"archived {name}"


def handle_superdirector_response(
    client: ZulipClient, channel: str, topic: str, project: str, workspace: Path,
    self_id: int, selection: Selection | None = None,
) -> tuple[list[str], bool]:
    """Act on what the superdirector wrote: `plan.md`, then the flags.

    Returns the report sections and whether the topic should be resolved
    after the final reply.

    The workspace is a fresh generation `<N>/` and nothing in it is deleted —
    the generation number is the workspace's double-act guard, and the
    leftovers stay as evidence of what that run was told. It appears in no
    key at all: a re-plan reconciles the tasks onto their serials instead of
    cancelling a generation and minting a new one, which is what lets a
    completed task stay completed.

    A `plan.md` also builds the mission's run surfaces — the `work-m<id>`
    channel and one `workrun-task<N>-m<id>` topic per task — so the
    conversation about doing the work has somewhere to happen.

    A run that wrote no `plan.md` and no flag asked a question instead;
    nothing changes state.
    """
    sections: list[str] = []
    resolve_after = False
    mission: Mission | None = None

    plan = workspace / PLAN_FILE
    if plan.is_file():
        # The whole file travels, heading included: the plan is what the
        # superdirector decided the mission means, and the conversation holds
        # it verbatim.
        line, mission = record_plan(
            client, channel, topic, project, plan.read_text(encoding="utf-8"), self_id
        )
        sections.append(line)
        changes = plan_changes(mission_tasks(client, mission, self_id), workspace)
        sections.extend(_change_lines(changes))
        sections.extend(prepare_run_surfaces(client, mission, changes, self_id, selection))
        if not changes:
            sections.append(NO_TASK_FILES)

    start_flag = workspace / "start.flag"
    if start_flag.is_file():
        mission = _mission_or_error(client, channel, topic, self_id, mission)
        set_mission_state(client, mission, MISSION_STARTED)
        sections.append(
            f"mission {mission.label} is now in progress; each task waits for a "
            f"post in its own `{WORKRUN_TOPIC_PREFIX}…` topic, and nothing runs "
            f"until somebody makes it"
        )

    cancel_flag = workspace / "cancel.flag"
    if cancel_flag.is_file():
        # The only remaining cancel-everything path: the mission is over, so
        # its live tasks are cancelled and its whole channel is retired.
        mission = _mission_or_error(client, channel, topic, self_id, mission)
        cancelled = cancel_tasks(client, mission_tasks(client, mission, self_id))
        set_mission_state(client, mission, MISSION_CANCELLED)
        suffix = f" along with {cancelled} task(s)" if cancelled else ""
        sections.append(f"mission {mission.label} is cancelled{suffix}; resolving this topic")
        sections.append(archive_work_channel(client, mission))
        resolve_after = True

    return sections, resolve_after


def _mission_or_error(
    client: ZulipClient, channel: str, topic: str, self_id: int, known: Mission | None
) -> Mission:
    """The mission this conversation holds, for a flag that needs one.

    A flag without a plan is a run saying "start it" about a mission nobody
    has planned. That is a question answered "there is nothing to start", not
    something to invent an empty mission for.
    """
    mission = known or read_mission(client, channel, topic, self_id)
    if mission is None:
        raise WorklogError(
            f"no mission is registered for {channel}/{topic}; plan one first"
        )
    return mission


def _change_lines(changes: list[TaskChange]) -> list[str]:
    return [f'{change.action} task {change.serial} "{change.title}"' for change in changes]


def supercoder_prompt(bot_name: str, workspace: Path, task: str, threads=()) -> str:
    """The placement lines, the task, then the guide — `superdirector_prompt`'s
    shape: read from and write to the workspace by absolute path, work in the
    project itself.

    The task text is the description its own topic holds, not a file in the
    workspace: the conversation is the record from planning onwards, and the
    `task[N].md` the superdirector wrote lives in another generation's
    directory.
    """
    lines = [
        f'The conversation with the developer ("{CHATLOG_FILE}") is placed in '
        f'"{workspace}". You are {bot_name!r} in the chatlog.',
    ]
    if placement := threads_placement(threads):
        # Absolute, like every other path in this prompt: the run's working
        # directory is the project, not the workspace.
        lines.append(placement)
    lines += [
        f'The other agents\' own introductions are placed in '
        f'"{agents_file_path(workspace)}".',
        f'Write "{REPORT_FILE}" — and any other file this guide asks for — '
        f'into "{workspace}".',
        "Your working directory is the project itself.",
        "",
        "The task this topic is for:",
        "",
        task.strip(),
    ]
    return prompt_with_guide(lines, guide("workrun_supercoder", "guide.md"))


def workrun_supercoder(prompt: str, cwd: Path,
                   on_event: Callable[[dict], None] | None = None,
                   home: tuple[str, str] | None = None,
                   selection: Selection | None = None) -> str:
    """One task-serving run in the project folder, with its record.

    Like the superdirector it runs where `main/`, `direction/` and `devlog/`
    are real directories, and its serving workspace travels by absolute path.

    `home` is the `workrun-` conversation this task is. A request this run
    posts to another agent is recorded against it, so the answer brings the
    task back rather than being waited for inside this run.
    """
    record = next_record_path(RECORDS_ROOT / "supercoder")
    output, _, exit_code = run_role(
        "supercoder",
        prompt,
        cwd=cwd,
        timeout=WORK_TIMEOUT_SECONDS,
        record=record,
        home=home,
        on_event=on_event,
        selection=selection,
        # `home` is the task's conversation, which is also where this run
        # is filed for the cost gauge; the project clone it runs in says
        # nothing about that (`gauge_panel` step 4).
        extra_meta=conversation_meta(home),
    )
    if exit_code != 0:
        raise ListenerError(f"supercoder run exited {exit_code}: {output.strip()[:500]}")
    # Whether the task is done is read from `report.md`, never from this text:
    # a run that edited files for fourteen turns and then stopped without a
    # farewell still did the work.
    return output.strip() or NO_CLOSING_MESSAGE


# --- live progress on workrun- topics ----------------------------------------
#
# The harness streams its conversation events (run_harness `on_event`) while
# the coding run is underway, and RunProgress turns them into topic posts.
# Editing one growing message would be tidier, but the realm's
# message_content_edit_limit_seconds (10 minutes on a default Zulip) is
# shorter than WORK_TIMEOUT_SECONDS, so an edit-based display would start
# failing mid-run; appending a throttled post survives any run length.

PROGRESS_INTERVAL_SECONDS = 120
PROGRESS_LINE_CHARS = 160

# The one argument of a tool call worth showing, tried in this order. Covers
# claude_code's tools (Bash/Read/Write/Edit/Glob/Grep/WebFetch) and agcode's
# (run/read/write/list) without naming either harness.
PROGRESS_DETAIL_KEYS = ("command", "file_path", "path", "pattern", "url")


def progress_line(block: dict) -> str | None:
    """One display line for one content block of an assistant event, or None
    for the block types progress does not show (thinking, tool results)."""
    kind = block.get("type")
    if kind == "text":
        text = " ".join(str(block.get("text", "")).split())
        return f"💬 {text[:PROGRESS_LINE_CHARS]}" if text else None
    if kind == "tool_use":
        name = str(block.get("name", "?"))
        arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
        detail = next(
            (str(arguments[key]) for key in PROGRESS_DETAIL_KEYS if arguments.get(key)),
            "",
        )
        detail = " ".join(detail.split())[:PROGRESS_LINE_CHARS]
        return f"🔧 {name}: {detail}" if detail else f"🔧 {name}"
    return None


class RunProgress:
    """Accumulate harness events, posted to the workrun- topic, throttled.

    `__call__` runs on run_harness's reader thread while the listener thread
    is blocked inside the run, and `flush` only after the run has returned
    (run_harness joins its reader before returning) — so the two never touch
    `pending` concurrently.

    A failed post is logged and its lines are dropped. Dropping keeps a
    Zulip outage from growing `pending` for the rest of a twenty-minute run,
    and the log line is what tells a reader that the topic went quiet
    because the display broke, not because the run stalled.
    """

    def __init__(self, client: ZulipClient, channel: str, topic: str,
                 interval_s: float = PROGRESS_INTERVAL_SECONDS):
        self.client = client
        self.channel = channel
        self.topic = topic
        self.interval_s = interval_s
        self.pending: list[str] = []
        self.last_post = time.monotonic()

    def __call__(self, event: dict) -> None:
        if event.get("type") != "assistant":
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and (line := progress_line(block)):
                self.pending.append(line)
        if self.pending and time.monotonic() - self.last_post >= self.interval_s:
            self.flush()

    def flush(self) -> None:
        """Post whatever accumulated; called on the interval and once after
        the run, so the last actions before an outcome are never lost."""
        if not self.pending:
            return
        body = "\n".join(self.pending)
        lines = len(self.pending)
        self.pending = []
        self.last_post = time.monotonic()
        try:
            topic_write(self.topic, body, channel=self.channel, client=self.client)
        except Exception as error:  # noqa: BLE001 - progress never kills a run
            log(
                f"could not post progress to {self.channel!r}/{self.topic!r}, "
                f"dropping {lines} line(s): {error!r}"
            )


def project_channel(slug: str) -> str:
    return f"{PROJECT_CHANNEL_PREFIX}{slug}"


def remove_work_directory(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


# --- serving one task on a workrun- topic ------------------------------------
#
# A `workrun-` topic is no longer a channel-agnostic button that picks whatever
# Work is next. It lives in one mission's `work-` channel, it is bound to one
# task by its own notes, and it is a conversation: every human post
# re-serves it, so finishing a task is something the developer and the
# supercoder agree on rather than something one agent run decides alone.

REPORT_FILE = "report.md"
WRONG_PLACE_REPLY = (
    "This `workrun-` topic is not bound to any task. A workrun topic is "
    "opened by planning a mission, and says which task it runs; a topic made "
    "by hand says nothing and runs nothing. Post in the workplan topic to "
    "plan or re-plan, and the topics will appear."
)
PREVIOUS_WORK_REPLY = "Please complete previous work"


def run_binding(client: ZulipClient, channel: str, topic: str, self_id: int) -> Task | None:
    """The task this `workrun-` conversation is bound to, or None.

    Its `[selfnote][task]` note was written when the topic was opened
    (`anchor_run_topic`), and the note's own message id is the task. A topic
    carrying no such note is not one of ours — a `workrun-` name somebody
    typed by hand — and runs nothing.

    Until `agent_standardize` p9 this was parsed out of the `work-` channel's
    description; until `refactor` p1 the note named a Plane Sub-Work and the
    project came out of the root note's channel. Both are gone: the record is
    the notes, and the project comes off the mission the task names.
    """
    return read_task(client, channel, topic, self_id)


def devlog_directory(slug: str) -> Path:
    """`.local/projects/<slug>/devlog/` — the clone the task records go into."""
    return PROJECTS_ROOT / slug / "devlog"


def title_slug(title: str) -> str:
    """A directory-safe stem for a Work title: `Fix title screen` → `fix-title-screen`."""
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return stem[:MISSION_DIR_TITLE_CHARS].rstrip("-") or "mission"


def mission_directory(devlog: Path, label: str, title: str) -> Path:
    """The mission's devlog directory, minted once and then found by prefix.

    The name freezes the mission title as it was at the *first* write,
    because a later re-plan may rewrite that title and a record that moved
    would stop being a record. The current title lives inside `work.md`
    anyway, so nothing is lost by the freeze.
    """
    prefix = f"{label.lower()}-"
    if devlog.is_dir():
        existing = sorted(
            path for path in devlog.iterdir()
            if path.is_dir() and path.name.startswith(prefix)
        )
        if existing:
            return existing[0]
    return devlog / f"{prefix}{title_slug(title)}"


def record_task_in_devlog(target: RunTarget, workspace: Path, report: str) -> str:
    """File the task and its report in the devlog clone, and push. One line.

    Deterministic handler code, the `serve_bmining` pattern: the agent is
    never asked to run git, and what it wrote travels by copy rather than by
    trust.

    Git is where a durable record belongs, and this is that record: the
    conversation carries the work while it happens, the devlog carries it
    afterwards. Nothing here reads back out of Zulip.
    """
    mission = target.mission
    devlog = devlog_directory(mission.slug)
    directory = mission_directory(devlog, mission.label, mission.title)
    task_dir = directory / f"task-{target.task.serial}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "work.md").write_text(target.task.document, encoding="utf-8")
    (task_dir / REPORT_FILE).write_text(report, encoding="utf-8")
    relative = task_dir.relative_to(devlog)
    if not (devlog / ".git").exists():
        # A main-only project (`init_project --main-only`): the record is a
        # plain local folder, read by the next plan and pushed nowhere.
        return f"recorded {relative} in devlog locally (not a repository)"
    pushed = commit_all_and_push(
        load_gitea_config(),
        devlog,
        f"{AUTO_MARKER} task {target.task.serial} report for {mission.label}",
    )
    return (
        f"recorded {relative} in devlog and pushed" if pushed
        else f"recorded {relative} in devlog (nothing to commit)"
    )


def push_main(slug: str) -> str:
    """Publish what the run committed in `main`. One line.

    `scheduled_routine` p2 left this open: the supercoder commits `main` —
    approving the task is what authorises that — and nothing ever pushed, so
    the Gitea repository aged while the local clone grew. The close-out
    publishes it, for the same reason `record_task_in_devlog` publishes the
    devlog: deterministic handler code, so the agent is never asked to run git,
    and a repository the requester can read is what the work was for.
    """
    main = project_directory(slug) / "main"
    if not (main / ".git").exists():
        return "main is not a repository; nothing pushed"
    carried = push_main_repository(load_gitea_config(), main)
    if not carried:
        return "main was already level with Gitea"
    return f"pushed main to Gitea ({carried} commit{'' if carried == 1 else 's'})"


def serve_run(context) -> TopicResult:
    """One serving of a `workrun-` topic: gate, agent, and — only if the run
    wrote a report — the close-out.

    The report file is the agreement signal the guide asks for ("if the
    developer agreed that the task was done, create report.md"), and the
    serving's own generation directory is what stops one report from being
    acted on twice.
    """
    context.step = "reading the binding"
    task = run_binding(context.client, context.channel, context.topic, context.self_id)
    if task is None:
        return TopicResult([WRONG_PLACE_REPLY])

    context.step = "the previous-work gate"
    target = run_target(context.client, task, context.self_id)
    if target.blocked_by:
        # Handler-side, before any cost: no agent run happens behind a gate.
        return TopicResult([f"{PREVIOUS_WORK_REPLY} ({target.blocked_by})"])
    slug = target.mission.slug

    sections: list[str] = []

    context.step = "project setup"
    init_project(slug)

    number = next_generation(topic_workspace(TOPICS_ROOT, context.channel, context.topic))
    workspace = generation_dir(TOPICS_ROOT, context.channel, context.topic, number, "supercoder")
    chatlog_path(workspace).write_text(
        format_chatlog(context.history, context.self_id), encoding="utf-8"
    )

    context.step = "threads"
    threads = write_threads(
        context.client,
        workspace,
        [
            conversation.as_pair()
            for conversation in remotes_for_home(
                context.client, context.channel, context.topic
            )
        ],
        context.self_id,
    )

    context.step = "harvest"
    write_agents_md(context.client, workspace)

    context.step = "supercoder"
    task_text = target.task.document
    # Into the task's own topic, whichever conversation this serving answers
    # in: progress belongs where the task lives.
    progress = RunProgress(context.client, context.channel, context.topic)
    try:
        sections.append(
            workrun_supercoder(
                supercoder_prompt(context.bot_name, workspace, task_text, threads),
                project_directory(slug),
                on_event=progress,
                home=(context.channel, context.topic),
                selection=context.selection,
            )
        )
    finally:
        # The tail of the stream — what the run was doing when it ended —
        # posts before the outcome does, whichever outcome it is.
        progress.flush()

    report_path = workspace / REPORT_FILE
    if not report_path.is_file():
        # Not a failure: the conversation simply is not finished. The topic
        # stays open and the next human post serves it again.
        return TopicResult(sections)

    context.step = "closing the task"
    report = report_path.read_text(encoding="utf-8")
    record_result(context.client, target.task, report)
    sections.append(
        f"task {target.task.serial} of {target.mission.label} is completed and "
        "its result is posted above; resolving this topic"
    )

    context.step = "publishing main"
    sections.append(push_main(slug))

    context.step = "devlog record"
    sections.append(record_task_in_devlog(target, workspace, report))
    return TopicResult(sections, resolve_after=True)


def handle_workrun(client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one awaiting `workrun-` topic through the shared skeleton.

    There is no `reply_to` any more: a serving brought back by a mention
    answers here, in the task's own topic, like every other one.
    """
    if at_the_entrance(client, channel, topic):
        return
    log(f"workrun topic {channel!r}/{topic!r}")
    serve_topic(
        client, channel, topic, serve_run,
        ack_text=ACK_TEXT, empty_reply=EMPTY_REPLY,
        exec_options=exec_options_for(SPEC, client),
    )


# --- brain-mining discussion on bmining- topics -----------------------------


def direction_directory(slug: str) -> Path:
    """`.local/projects/<slug>/direction/` — the clone the director works in."""
    return PROJECTS_ROOT / slug / "direction"


def bmining_work_directory(slug: str) -> Path:
    """`.local/work/` inside the direction clone — holds only the chatlog.

    Re-created with a fresh chatlog on every serving and removed after the
    reply; what the director *records* lives in the clone proper.
    """
    return direction_directory(slug) / ".local" / "work"


def bmining_prompt(bot_name: str) -> str:
    """The chatlog placement, then the discussion guide."""
    return prompt_with_guide(
        [
            f'The chatlog is placed at ".local/work/{CHATLOG_FILE}" in the '
            f"working directory. You are {bot_name!r} in the chatlog.",
        ],
        guide("bmining_director", "guide.md"),
    )


def run_director(prompt: str, cwd: Path,
                 conversation: tuple[str, str] | None = None,
                 selection: Selection | None = None) -> str:
    """One discussion run in the direction clone, with its record."""
    record = next_record_path(RECORDS_ROOT / "director")
    output, _, exit_code = run_role(
        "director",
        prompt,
        cwd=cwd,
        timeout=DIRECTOR_TIMEOUT_SECONDS,
        record=record,
        selection=selection,
        extra_meta=conversation_meta(conversation),
    )
    if exit_code != 0:
        raise ListenerError(f"director run exited {exit_code}: {output.strip()[:500]}")
    # The discussion notes the director recorded are committed by the caller
    # whatever it said here.
    return output.strip() or NO_CLOSING_MESSAGE


NO_DIRECTION_REPLY = (
    "This project has no `direction/` repository (it was set up main-only), "
    "so there is nowhere to record a brain-mining discussion. Ask in a "
    "`workplan-` topic instead, or re-initialise the project with a direction repository."
)


def serve_bmining(context) -> TopicResult:
    """One discussion serving: chatlog in, director run, notes pushed.

    The commit/push of whatever the director recorded is deterministic
    handler code — the agent is never asked to run git, and `.gitignore`
    (not the cleanup) is what keeps the chatlog out of the commit.
    """
    project = project_from_channel(context.channel)

    context.step = "project setup"
    init_project(project)

    direction_dir = direction_directory(project)
    if not (direction_dir / ".git").exists():
        return TopicResult([NO_DIRECTION_REPLY])
    work_dir = bmining_work_directory(project)
    try:
        context.step = "chatlog placement"
        work_dir.mkdir(parents=True, exist_ok=True)
        chatlog_path(work_dir).write_text(
            format_chatlog(context.history, context.self_id), encoding="utf-8"
        )

        context.step = "director"
        sections = [run_director(bmining_prompt(context.bot_name), direction_dir,
                                 conversation=(context.channel, context.topic),
                                 selection=context.selection)]

        context.step = "recording"
        if commit_all_and_push(
            load_gitea_config(),
            direction_dir,
            f"{AUTO_MARKER} bmining notes from {context.topic}",
        ):
            sections.append("recorded notes committed and pushed")
        return TopicResult(sections)
    finally:
        remove_work_directory(work_dir)


def handle_bmining(client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one awaiting bmining topic through the shared skeleton."""
    if at_the_entrance(client, channel, topic) or not in_project_channel(channel, topic):
        return
    log(f"bmining topic {channel!r}/{topic!r}")
    serve_topic(client, channel, topic, serve_bmining, ack_text=ACK_TEXT,
                empty_reply=EMPTY_REPLY, exec_options=exec_options_for(SPEC, client))


def handle_mention(client: ZulipClient, channel: str, topic: str) -> None:
    """This instance was named in a topic it does not own: serve the task it
    was speaking for.

    A `workrun-` task that delegates posts into another agent's topic and
    ends. The topic itself says which task that was — the root note
    `agentchat send` wrote there before the first real post — so when the
    answer names this instance, that task is served again: its workspace, its
    chatlog, its task record, and the topic that answered placed beside
    them as a thread.

    **The reply goes home**, into the `workrun-` topic, which is
    `agent_standardize` p9 for autolab and p8 everywhere else. Until now it
    went back into the topic that asked, so every progress report was a post
    in another agent's conversation — and a post in somebody's topic serves
    them. Anything this instance wants to say to that agent is now a
    deliberate `agentchat send` inside the run.

    Afterwards the callback is marked served in the `workrun-` topic. Because
    the reply went home, this bot never becomes the last poster where it was
    named, so without the mark a listener restart would serve every finished
    delegation again — and here that is a supercoder run against a live
    repository, not a duplicate sentence.

    Only `workrun-` topics delegate today, so a root note pointing anywhere
    else is logged and dropped rather than guessed at.
    """
    self_id = int(client.whoami()["user_id"])
    home = rootchat_home(client, channel, topic, self_id)
    if home is None:
        log(f"mention in {channel!r}/{topic!r} carries no root note of ours; ignoring")
        return
    if not home.topic.startswith(WORKRUN_TOPIC_PREFIX):
        log(f"mention in {channel!r}/{topic!r} is for {home}, which is not a task; ignoring")
        return
    log(f"mention in {channel!r}/{topic!r} serves {home}")
    handle_workrun(client, home.channel, home.topic)
    served = note_served(client, home, channel, topic)
    if served is None:
        log(f"nothing to mark served in {channel!r}/{topic!r}")
    else:
        log(f"marked {channel!r}/{topic!r} served up to {served} in {home}")
