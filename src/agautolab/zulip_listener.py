"""Pull mission topics from Zulip into topic workspaces and the superdirector.

`agag.zulip.sweep_serve` finds every unresolved `mission-*` topic whose last
poster is not this bot, and `agag.topics.serve_topic` serves each one — the
skeleton shared with agforge: ack, generation workspace, chatlog, the steps,
always reply naming the failed step, then re-check for human posts that
arrived during the run.

Each serving cuts a new generation directory `<N>/`, the way agforge's create
topics do. Before that, one stable directory was reused forever, so a
continued conversation ran on top of the previous run's leftovers. `N` is the
workspace guard only: Sub-Work keys are one-per-serial for the life of the
mission, so a re-plan updates the issue behind a serial instead of cancelling
a generation and minting a new one — which is what lets a completed task stay
completed.

Planning also builds the surfaces the work is then done on: a `work-<label>`
channel per mission Work, holding one `run-task<N>-<label>` topic per
Sub-Work, in the folder of the project's own `pj-` channel and with its
subscribers.

The superdirector serves the topic alone — there is no front relay. It runs
in the persistent project folder, where `main/`, `direction/` and `devlog/`
are real directories (symlinking them into the workspace was tried first, and
harness file tools do not follow directory symlinks), and the serving's own
generation workspace is handed to it by absolute path — the same shape as the
create-topic answer flow. The chatlog and Plane mirror are read from there,
and `plan.md`, the task split and the flags are written back there, so
everything one run wrote stays behind in its own generation as evidence and
can never be acted on twice.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from agag.topics import (
    TopicResult,
    chatlog_path,
    format_chatlog,
    generation_dir as shared_generation_dir,
    guide as shared_guide,
    next_generation,
    next_record_path as shared_next_record_path,
    prompt_with_guide,
    serve_topic,
    topic_workspace as shared_topic_workspace,
)
from agag.zulip import (
    RESOLVED_TOPIC_PREFIX,
    ZulipClient,
    ZulipError,
    log,
    sweep_serve,
    topic_write,
)

from .mission import (
    ASSET_TOPIC_PREFIX,
    TaskChange,
    Work,
    asset_answer_context,
    asset_order,
    asset_topic,
    cancel_sub_works,
    compose_document,
    description_html,
    next_work,
    reconcile_task_files,
    report_work,
    split_document,
    transition_work,
    upsert_work,
    write_mission_workspace,
)
from .project_init import (
    AUTO_MARKER,
    PROJECT_NAME,
    PROJECTS_ROOT,
    commit_all_and_push,
    init_project,
    load_gitea_config,
)
from .role_run import run_role

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"
TOPICS_ROOT = AGAUTOLAB_ROOT / ".local" / "topics"
GUIDES = AGAUTOLAB_ROOT / "agent" / "guides"
RECORDS_ROOT = AGAUTOLAB_ROOT / ".local" / "agent"

MISSION_TOPIC_PREFIX = "mission-"
RUN_TOPIC_PREFIX = "run-"
CREATE_TOPIC_PREFIX = "create-"
BMINING_TOPIC_PREFIX = "bmining-"
PROJECT_CHANNEL_PREFIX = "pj-"
# One channel per mission Work, named after that Work's Plane label:
# `work-pa-12`. Its `run-task<N>-pa-12` topics are one conversation per task.
WORK_CHANNEL_PREFIX = "work-"
HISTORY_MESSAGES = 1000

# The channel description carries the binding a `run-` serving needs back.
# Parsing `work-pa-12` recovers the Work label and nothing else — not the
# project slug, not which mission topic planned it — so both travel here.
WORK_CHANNEL_BINDING = re.compile(
    r"project:\s*(?P<slug>\S+?)\s*;\s*mission:\s*(?P<channel>[^/;]+)/(?P<topic>[^;]+?)\s*$"
)
RUN_TOPIC_NAME = re.compile(r"^run-task(?P<serial>\d+)-(?P<work>.+)$")

ACK_TEXT = "Message received. Please wait for the reply."
EMPTY_REPLY = "There is nothing in this topic to answer yet."

# What a run that ended without a closing message contributes to its report.
# The harness no longer fails such a run (its work is its files, not its
# farewell), so every flow that quotes an agent's answer needs something to
# say instead — a topic that got only an ack and then silence would drop out
# of the sweep until a human posts again.
NO_CLOSING_MESSAGE = "(the run ended without a closing message)"

# The planning round's files, all of them in the serving's own generation
# workspace. The Plane mirror lives in `current/` inside that workspace so the
# read-back `task1.md`, `task2.md`, … can never be mistaken for a task split
# the superdirector wrote this run.
PLAN_FILE = "plan.md"
CURRENT_DIR = "current"

# --- the asset route -------------------------------------------------------
#
# agforge's request service, and the footer it puts on every delivery. A
# presigned download URL lives 60 minutes; the object behind it does not
# expire, so autolab keeps the *key* and asks for a fresh URL immediately
# before it launches a run that may take 1200 s. That ordering is the whole
# point — a URL recovered any earlier could die mid-run.
AGFORGE_URL = os.environ.get("AGFORGE_URL", "http://localhost:8092").rstrip("/")
S3_KEY_MARKER = "[S3KEY]"
S3_KEY_LINE = re.compile(rf"^\s*{re.escape(S3_KEY_MARKER)}\s+(?P<key>\S+)\s*$", re.MULTILINE)
RESIGN_TIMEOUT_SECONDS = 30

# The project's art direction, quoted into every asset order.
AESTHETICS_FILE = "aesthetics.md"

# --- answering agforge on a create- topic ----------------------------------
#
# The mention gate is the loop breaker between the two bots. agforge reacts to
# any `create-` post that is not its own; autolab reacts only when the last
# message mentions it by name. Without that asymmetry the two would answer
# each other forever, one paid agent run per lap.
CHATLOG_FILE = "chatlog.md"
TASK_FILE = "task.md"
ANSWER_FILE = "answer.md"
ANSWER_ROLE = "answer"

# Appended to the run guide when the work needs an asset. The director check
# that would normally weigh the delivered asset against the spec is
# deliberately skipped this episode, so the judgement is handed to the coding
# run itself, in the prompt.
ASSET_PROMPT_NOTE = (
    "\n\nNote: the asset required by this work can be downloaded from the URL "
    "below. If the asset does not match the spec, try to compromise; only if "
    "truly unacceptable, treat the work as failed:\n"
)

# One topic occupies the listener for at most this long; the sweep loop is
# single-threaded and serial, so this is also the delay before the next
# matching topic is looked at (events keep queueing meanwhile).
WORK_TIMEOUT_SECONDS = 1200
# The superdirector reads the whole project — `main/`, `direction/` and
# `devlog/` — and the chatlog before it plans, so it gets the work timeout.
SUPERDIRECTOR_TIMEOUT_SECONDS = WORK_TIMEOUT_SECONDS
# The director reads the whole direction clone and records notes into it, so
# it gets the work timeout too.
DIRECTOR_TIMEOUT_SECONDS = WORK_TIMEOUT_SECONDS

__all__ = [
    "RunProgress",
    "archive_work_channel",
    "ZULIP_ENV",
    "aesthetics_text",
    "answer_prompt",
    "asset_gate",
    "asset_order_text",
    "bmining_prompt",
    "bmining_work_directory",
    "direction_directory",
    "ensure_work_channel",
    "find_channel",
    "dispatch",
    "find_asset_key",
    "format_chatlog",
    "generation_dir",
    "guide",
    "handle_bmining",
    "handle_create",
    "handle_run",
    "handle_superdirector_response",
    "handle_topic",
    "live_topic_name",
    "mirror_task_changes",
    "main",
    "mentions_us",
    "next_record_path",
    "prepare_run_surfaces",
    "progress_line",
    "run_answer",
    "run_topic",
    "project_channel",
    "project_directory",
    "resign",
    "run_director",
    "run_superdirector",
    "superdirector_prompt",
    "serve_bmining",
    "run_work",
    "serve",
    "topic_workspace",
    "work_channel",
    "work_channel_description",
    "work_directory",
    "work_prompt",
]


class ListenerError(RuntimeError):
    """One mission-topic workflow could not complete."""


def project_from_channel(channel: str) -> str:
    if not channel.startswith(PROJECT_CHANNEL_PREFIX):
        raise ListenerError(f"mission topic is not in a {PROJECT_CHANNEL_PREFIX} channel: {channel}")
    project = channel.removeprefix(PROJECT_CHANNEL_PREFIX)
    if not PROJECT_NAME.fullmatch(project):
        raise ListenerError(f"channel does not contain a valid project name: {channel}")
    return project


def topic_workspace(channel: str, topic: str) -> Path:
    """`.local/topics/<channel>/<topic>/` — the topic's own directory."""
    return shared_topic_workspace(TOPICS_ROOT, channel, topic)


def generation_dir(channel: str, topic: str, number: int, role: str) -> Path:
    """`.local/topics/<channel>/<topic>/<N>/<role>/`.

    Generations are never deleted. Cutting a new one is what stops a previous
    generation's `new_mission.md` or task split from being acted on twice.
    """
    return shared_generation_dir(TOPICS_ROOT, channel, topic, number, role)


def guide(*parts: str) -> str:
    return shared_guide(GUIDES, *parts)


def next_record_path(directory: Path) -> Path:
    return shared_next_record_path(directory)


def superdirector_prompt(bot_name: str, workspace: Path, plane_files: bool) -> str:
    """The placement lines, then the guide — the `answer_prompt` shape:
    read from and write to the workspace by absolute path, work in the
    project itself."""
    lines = [
        f'The conversation with the requester ("{CHATLOG_FILE}") is placed in '
        f'"{workspace}". You are {bot_name!r} in the chatlog.',
    ]
    if plane_files:
        lines.append(
            "The currently registered mission and tasks are placed in "
            f'"{workspace / CURRENT_DIR}".'
        )
    lines.append(
        f'Write every file this guide asks for — "{PLAN_FILE}", "task[N].md", '
        f'the flags — into "{workspace}".'
    )
    lines.append("Your working directory is the project itself.")
    return prompt_with_guide(lines, guide("mission_superdirector", "guide.md"))


def serve(context) -> TopicResult:
    """agautolab's part of one serving: project setup, Plane read-back, and
    one superdirector run in the project folder, reading from and writing to
    the serving's generation workspace by absolute path.

    `handle_superdirector_response` then acts on what the superdirector
    *wrote* — its answer is relayed verbatim and never parsed. A run that
    wrote nothing changed nothing: the reply (a question, usually) is the
    whole outcome.
    """
    project = project_from_channel(context.channel)
    number = next_generation(topic_workspace(context.channel, context.topic))
    workspace = generation_dir(context.channel, context.topic, number, "superdirector")
    chatlog_path(workspace).write_text(
        format_chatlog(context.history, context.self_id), encoding="utf-8"
    )

    context.step = "project setup"
    init_project(project)

    context.step = "plane read-back"
    current = workspace / CURRENT_DIR
    current.mkdir(exist_ok=True)
    plane_files = write_mission_workspace(current, project, context.channel, context.topic)
    if not plane_files:
        current.rmdir()

    context.step = "superdirector"
    sections = [
        run_superdirector(
            superdirector_prompt(context.bot_name, workspace, plane_files),
            project_directory(project),
        )
    ]

    context.step = "response handling"
    response_sections, resolve_after = handle_superdirector_response(
        context.client, context.channel, context.topic, project, workspace
    )
    sections.extend(response_sections)
    return TopicResult(sections, resolve_after=resolve_after)


def handle_topic(client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one awaiting mission topic through the shared skeleton."""
    log(f"mission topic {channel!r}/{topic!r}")
    serve_topic(client, channel, topic, serve, ack_text=ACK_TEXT, empty_reply=EMPTY_REPLY)


def project_directory(project: str) -> Path:
    """`.local/projects/<slug>/` — the folder holding `main/`, `direction/`
    and `devlog/`, which `init_project` clones and every later serving reuses."""
    return PROJECTS_ROOT / project


def run_superdirector(prompt: str, cwd: Path) -> str:
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
# `work-<label>` channel per mission Work, and one `run-task<N>-<label>` topic
# in it per Sub-Work. The autolab bot posts the task content itself and is
# therefore the topic's last poster, which keeps the sweep quiet — the topic
# waits, by design, until a human posts into it.

UPDATED_BY_PLANNER = "Updated by planner."
CANCELLED_BY_PLANNER = "Cancelled by planner."
CHANGED_AFTER_DONE = (
    "This task was changed by the planner after it had been completed. The "
    "topic is left as it is; whether to redo it is the mission's call."
)


def work_channel(label: str) -> str:
    """`work-pa-12` — one channel per mission Work, named after its label."""
    return f"{WORK_CHANNEL_PREFIX}{label.lower()}"


def run_topic(serial: int, label: str) -> str:
    """`run-task3-pa-12` — one topic per Sub-Work serial."""
    return f"{RUN_TOPIC_PREFIX}task{serial}-{label.lower()}"


def work_channel_description(slug: str, channel: str, topic: str) -> str:
    """The binding a `run-` serving reads back out of the channel.

    The channel name gives back the Work label and nothing else, so the
    project slug and the mission topic that planned it travel here. `[AUTO]`
    marks the channel as one this system made.
    """
    return f"{AUTO_MARKER} project: {slug}; mission: {channel}/{topic}"


def find_channel(client: ZulipClient, name: str) -> dict | None:
    return next((row for row in client.channels() if str(row.get("name")) == name), None)


def ensure_work_channel(
    client: ZulipClient, slug: str, channel: str, topic: str, label: str
) -> str:
    """Create (or re-join) the mission's `work-` channel and return its name.

    Its members are the parent `pj-` channel's subscribers, so the developer
    and this bot are both in it without anyone deciding again who the work
    goes to. Its folder is the parent channel's folder — whatever that is,
    including none: this is not the place to invent a folder structure.

    `create_channel` is subscribe-based and therefore idempotent, which is
    what makes re-planning safe.
    """
    name = work_channel(label)
    parent = find_channel(client, project_channel(slug))
    principals: list[int] = []
    folder_id = None
    if parent and parent.get("stream_id") is not None:
        principals = client.channel_subscribers(int(parent["stream_id"]))
        raw_folder = parent.get("folder_id")
        folder_id = int(raw_folder) if raw_folder is not None else None
    client.create_channel(
        name,
        work_channel_description(slug, channel, topic),
        principals,
        folder_id=folder_id,
    )
    return name


def live_topic_name(client: ZulipClient, channel: str, topic: str) -> str:
    """`topic`, or its resolved `\u2714 ` name when that is what exists.

    A resolved topic is a *renamed* topic, so posting under the bare name
    would open a second, unresolved one beside it.
    """
    resolved = f"{RESOLVED_TOPIC_PREFIX}{topic}"
    try:
        names = client.channel_topics(client.stream_id(channel))
    except Exception as error:  # noqa: BLE001 - a note is never worth a failure
        log(f"could not list topics of {channel!r}: {error!r}")
        return topic
    return resolved if resolved in names and topic not in names else topic


def mirror_task_changes(
    client: ZulipClient, channel: str, label: str, changes: list[TaskChange]
) -> list[str]:
    """Mirror one re-plan onto the mission's `run-` topics, one to one.

    Created and updated tasks get their content posted; a cancelled one is
    told so and resolved; a task the planner changed *after* it was completed
    gets a note and nothing else — redoing it is the mission conversation's
    decision, not this handler's. An unchanged task is left silent, so a
    re-plan that only touched task 3 does not disturb tasks 1 and 2.
    """
    lines: list[str] = []
    for change in changes:
        topic = run_topic(change.serial, label)
        if change.action == "created":
            topic_write(topic, change.document, channel=channel, client=client)
            lines.append(f"opened {channel}/{topic}")
        elif change.action == "updated":
            topic_write(
                topic,
                f"{UPDATED_BY_PLANNER}\n\n{change.document}",
                channel=channel,
                client=client,
            )
            lines.append(f"updated {channel}/{topic}")
        elif change.action == "cancelled":
            message_id = client.send_to_channel(channel, topic, CANCELLED_BY_PLANNER)
            client.resolve_topic(int(message_id), topic)
            lines.append(f"cancelled and resolved {channel}/{topic}")
        elif change.action == "changed-after-done":
            client.send_to_channel(
                channel, live_topic_name(client, channel, topic), CHANGED_AFTER_DONE
            )
            lines.append(f"noted a post-completion change in {channel}/{topic}")
    return lines


def prepare_run_surfaces(
    client: ZulipClient, slug: str, channel: str, topic: str, label: str,
    changes: list[TaskChange],
) -> list[str]:
    """The whole Zulip side of one planning round."""
    name = ensure_work_channel(client, slug, channel, topic, label)
    return [f"work channel {name} is ready", *mirror_task_changes(client, name, label, changes)]


def archive_work_channel(client: ZulipClient, label: str) -> str:
    """Retire a cancelled mission's channel. One report line.

    Mission cancel is the only path that ever gets here and nothing is ever
    re-created after it, so the archived channel's retained name cannot
    collide with a later one.
    """
    name = work_channel(label)
    existing = find_channel(client, name)
    if not existing or existing.get("stream_id") is None:
        return f"no {name} channel to archive"
    client.archive_channel(int(existing["stream_id"]))
    return f"archived {name}"


def handle_superdirector_response(
    client: ZulipClient, channel: str, topic: str, project: str, workspace: Path
) -> tuple[list[str], bool]:
    """Act on what the superdirector wrote: `plan.md`, then the flags.

    Returns the report sections and whether the topic should be resolved
    after the final reply.

    The workspace is a fresh generation `<N>/` and nothing in it is deleted —
    the generation number is the workspace's double-act guard, and the
    leftovers stay as evidence of what that run was told. It no longer
    appears in any Plane key: a re-plan reconciles the Sub-Works onto their
    serials instead of cancelling a generation and minting a new one, which
    is what lets a completed task stay completed.

    A `plan.md` also builds the mission's run surfaces — the `work-<label>`
    channel and one `run-task<N>-<label>` topic per Sub-Work — so the
    conversation about doing the work has somewhere to happen.

    A run that wrote no `plan.md` and no flag asked a question instead;
    nothing changes state.
    """
    sections: list[str] = []
    resolve_after = False
    label: str | None = None

    plan = workspace / PLAN_FILE
    if plan.is_file():
        # Title and description both from the plan: the Work is what the
        # superdirector decided the mission means. The asset answer flow
        # reads the description back as `plan.md`, so the whole file travels,
        # heading included.
        plan_text = plan.read_text(encoding="utf-8")
        title, _ = split_document(plan_text)
        line, label = upsert_work(project, channel, topic, title, plan_text)
        sections.append(line)
        lines, changes = reconcile_task_files(project, channel, topic, workspace)
        sections.extend(lines)
        sections.extend(
            prepare_run_surfaces(client, project, channel, topic, label, changes)
        )

    start_flag = workspace / "start.flag"
    if start_flag.is_file():
        label = transition_work(project, channel, topic, "started")
        sections.append(f"mission {label} is now In Progress")

    cancel_flag = workspace / "cancel.flag"
    if cancel_flag.is_file():
        # The only remaining cancel-everything path: the mission is over, so
        # its live Sub-Works are cancelled and its whole channel is retired.
        cancelled = cancel_sub_works(project, channel, topic)
        label = transition_work(project, channel, topic, "cancelled")
        suffix = f" along with {cancelled} sub-work(s)" if cancelled else ""
        sections.append(f"mission {label} is cancelled{suffix}; resolving this topic")
        sections.append(archive_work_channel(client, label))
        resolve_after = True

    return sections, resolve_after


def work_directory(slug: str) -> Path:
    """`.local/work/` inside the project's shared `main` clone.

    The work run happens in that clone, the same directory every later run
    reuses; `.local/work/` is the one place this phase writes and deletes.
    """
    return PROJECTS_ROOT / slug / "main" / ".local" / "work"


def work_prompt(asset_url: str | None) -> str:
    """The run guide, plus the asset note when there is an asset to fetch."""
    prompt = guide("run_coding", "guide.md")
    return f"{prompt}{ASSET_PROMPT_NOTE}{asset_url}" if asset_url else prompt


# --- live progress on run- topics -------------------------------------------
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
    """Accumulate harness events and post them to the run- topic, throttled.

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


def run_work(workspace: Path, asset_url: str | None = None,
             on_event: Callable[[dict], None] | None = None) -> str:
    """One work run in a project's `main` clone, with its `ag.agent-run.v1` record."""
    record = next_record_path(RECORDS_ROOT / "run")
    output, _, exit_code = run_role(
        "coding",
        work_prompt(asset_url),
        cwd=workspace,
        timeout=WORK_TIMEOUT_SECONDS,
        record=record,
        on_event=on_event,
    )
    if exit_code != 0:
        raise ListenerError(f"work run exited {exit_code}: {output.strip()[:500]}")
    # Whether the work succeeded is read from `report.md` and `success.flag`
    # by the caller, never from this text: a run that edited files for
    # fourteen turns and then stopped without a farewell still did the work.
    return output.strip() or NO_CLOSING_MESSAGE


# --- the asset state machine -----------------------------------------------


def project_channel(slug: str) -> str:
    return f"{PROJECT_CHANNEL_PREFIX}{slug}"


def aesthetics_text(slug: str) -> str:
    """The project's art direction, or empty when it has none yet."""
    path = PROJECTS_ROOT / slug / "direction" / AESTHETICS_FILE
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def asset_order_text(work: Work) -> str:
    """The order agforge reads.

    Written to stand alone: agforge's front reads the whole topic chatlog and
    its generator plans from that, so everything the asset has to satisfy —
    what it is, what it is for, and the project's house style — has to be in
    this one post. Nothing here points at a file agforge cannot open.
    """
    parts = [f"# {work.name}", work.description.strip()]
    if aesthetics := aesthetics_text(work.slug):
        parts.append(f"Art direction for this project:\n{aesthetics}")
    return "\n\n".join(part for part in parts if part)


def find_asset_key(client: ZulipClient, channel: str, topic: str) -> str | None:
    """The newest `[S3KEY] <key>` footer in the asset topic, or None.

    agforge puts that footer on the delivery post (Step 4). Reading it back
    out of the topic needs no new Plane API and no shared code between the two
    agents — only the marker, which is documented on both sides.
    """
    for message in reversed(client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)):
        if match := S3_KEY_LINE.search(str(message.get("content", ""))):
            return match.group("key")
    return None


def resign(key: str) -> str:
    """A fresh download URL for `key`, from agforge's `POST /api/resign`."""
    request = urllib.request.Request(
        f"{AGFORGE_URL}/api/resign",
        data=json.dumps({"key": key}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=RESIGN_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise ListenerError(f"could not re-sign {key}: {error}") from error
    url = payload.get("url") if isinstance(payload, dict) else None
    if not url:
        raise ListenerError(f"re-signing {key} returned no url: {payload!r}")
    return str(url)


def asset_gate(client: ZulipClient, work: Work) -> tuple[list[str], str | None]:
    """What an `asset`-labelled Work needs before it can be coded.

    Returns `(report sections, download url)`. A `None` url means the serving
    finishes here — the order was just placed, or agforge is still on it. The
    autolab Work is deliberately left `unstarted` in both cases, so
    `next_work` keeps choosing it and everything behind it waits for the
    asset. Blocking the queue is accepted for this episode.

    Plane is the ledger for all three states; no local file records that an
    order was placed.
    """
    channel = project_channel(work.slug)
    topic = asset_topic(work.issue_id)
    state, _ = asset_order(work.project_id, channel, work.issue_id)

    if state == "absent":
        topic_write(topic, asset_order_text(work), channel=channel, client=client)
        return [f"asset ordered in {channel}/{topic}"], None
    if state != "done":
        # No `runcreate-` post is emitted: the Omni Agent fires that by hand
        # this episode (Deus Ex Machina, recorded in the episode doc).
        return [f"asset in progress in {channel}/{topic}"], None

    key = find_asset_key(client, channel, topic)
    if not key:
        raise ListenerError(
            f"the asset work for {channel}/{topic} is completed but the topic "
            f"carries no {S3_KEY_MARKER} footer"
        )
    return [f"asset ready ({key})"], resign(key)


def remove_work_directory(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


def handle_run(client: ZulipClient, channel: str, topic: str) -> None:
    """Execute one Work, triggered by any non-bot post in a `run-` topic.

    The chatlog is never read: a `run-` topic is a button, not a conversation.
    Whatever the topic gets, one eligible Work is chosen from Plane
    (`next_work`), executed in its project's `main` clone, and reported back
    to both Plane and the topic. Every exit path after the ack posts
    something, the same discipline `handle_topic` follows.

    A Work carrying the `asset` label goes through `asset_gate` first, and
    two of its three outcomes end the serving before any coding run.
    """
    log(f"run topic {channel!r}/{topic!r}")
    topic_write(topic, ACK_TEXT, channel=channel, client=client)

    sections: list[str] = []
    work_dir: Path | None = None
    step = "choosing the work"
    try:
        chosen = next_work()
        if chosen is None:
            topic_write(topic, "no work", channel=channel, client=client)
            return
        workspace = PROJECTS_ROOT / chosen.slug / "main"
        candidate = work_directory(chosen.slug)
        if candidate.is_dir() and any(candidate.iterdir()):
            topic_write(
                topic,
                f"work dirty: {chosen.slug}/main has a leftover .local/work/; "
                "remove it by hand and trigger again",
                channel=channel,
                client=client,
            )
            return

        asset_url: str | None = None
        if chosen.is_asset:
            step = "asset check"
            asset_sections, asset_url = asset_gate(client, chosen)
            sections.append(f'asset work "{chosen.name}" in {chosen.slug}')
            sections.extend(asset_sections)
            if asset_url is None:
                # The Work stays unstarted; the next trigger looks again.
                topic_write(topic, "\n\n".join(sections), channel=channel, client=client)
                return

        sections.append(f'running "{chosen.name}" in {chosen.slug}')

        step = "writing the work"
        # Only now does the directory become this run's to delete.
        work_dir = candidate
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "work.md").write_text(
            compose_document(chosen.name, description_html(chosen.description)),
            encoding="utf-8",
        )

        step = "work run"
        progress = RunProgress(client, channel, topic)
        try:
            sections.append(run_work(workspace, asset_url, on_event=progress))
        finally:
            # The tail of the stream — what the run was doing when it ended —
            # posts before the outcome does, whichever outcome it is.
            progress.flush()

        step = "reporting to plane"
        report_path = work_dir / "report.md"
        report = report_path.read_text(encoding="utf-8") if report_path.is_file() else None
        success = (work_dir / "success.flag").exists()
        if report is None:
            sections.append("no report")
        work_label, commented, completed = report_work(
            chosen.project_id, chosen.issue_id, report, success
        )
        sections.append(
            f"work {work_label}: commented {'yes' if commented else 'no'}, "
            f"Done {'yes' if completed else 'no'}"
        )
    except Exception as error:  # noqa: BLE001 - the topic is the error channel
        log(f"run topic workflow failed during {step}: {error!r}")
        sections.append(f"failed during {step}: {error}")
    finally:
        if work_dir is not None:
            remove_work_directory(work_dir)

    topic_write(topic, "\n\n".join(section for section in sections if section),
                channel=channel, client=client)


# --- answering agforge's questions on create- topics -----------------------


def mentions_us(content: str, bot_name: str, self_id: int) -> bool:
    """Whether a message mentions this bot.

    Zulip writes a mention as `@**Name**`, silences it as `@_**Name**`, and
    disambiguates duplicate names as `@**Name|<id>**`. All three are the same
    intent, so all three open the gate; `apply_markdown: false` means the raw
    syntax is what arrives.
    """
    pattern = rf"@_?\*\*{re.escape(bot_name)}(\|{self_id})?\*\*"
    return re.search(pattern, content) is not None


def answer_prompt(answer_dir: Path) -> str:
    """The placement lines, then the answer guide."""
    return prompt_with_guide(
        [
            f'The conversation with the asset creator ("{CHATLOG_FILE}"), the plan '
            f'it belongs to ("{PLAN_FILE}") and the task it is for ("{TASK_FILE}") '
            f'are placed in "{answer_dir}".',
            f'Write your answer to "{answer_dir / ANSWER_FILE}".',
            "Your working directory is the project itself.",
        ],
        guide("create_answer_superdirector", "guide.md"),
    )


def run_answer(answer_dir: Path, project_dir: Path) -> str:
    """One answer run: reads from `answer_dir`, but works in the project."""
    record = next_record_path(RECORDS_ROOT / ANSWER_ROLE)
    output, _, exit_code = run_role(
        "superdirector",
        answer_prompt(answer_dir),
        cwd=project_dir,
        timeout=SUPERDIRECTOR_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"answer run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def handle_create(client: ZulipClient, channel: str, topic: str) -> None:
    """Answer agforge's question on one `create-asset_<work_id>` topic.

    Deliberately not `serve_topic`-shaped: there is no ack. An ack would make
    autolab the topic's last poster, and agforge's sweep would resume the
    conversation on it — before the answer exists. The single post at the end
    is both the answer and the hand-back.

    Two gates come before any cost is incurred. The topic name must be an
    asset topic, and the last message must mention this bot. Failing either,
    the handler returns without posting: it is then still not the last poster,
    so the sweep will look again next time, at the price of one history read.
    """
    log(f"create topic {channel!r}/{topic!r}")
    if not topic.startswith(ASSET_TOPIC_PREFIX):
        log(f"ignoring {topic!r}: not an asset topic")
        return

    self_user = client.whoami()
    self_id = int(self_user["user_id"])
    bot_name = str(self_user.get("full_name") or client.email)

    history = client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)
    if not history:
        return
    last = history[-1]
    if last.get("sender_id") == self_id:
        return
    if not mentions_us(str(last.get("content", "")), bot_name, self_id):
        log(f"ignoring {channel!r}/{topic!r}: the last message does not mention {bot_name!r}")
        return

    project = project_from_channel(channel)
    work_id = topic.removeprefix(ASSET_TOPIC_PREFIX)
    plan, task = asset_answer_context(project, work_id)

    number = next_generation(topic_workspace(channel, topic))
    answer_dir = generation_dir(channel, topic, number, ANSWER_ROLE)
    chatlog_path(answer_dir).write_text(
        format_chatlog(history, self_id), encoding="utf-8"
    )
    (answer_dir / PLAN_FILE).write_text(plan, encoding="utf-8")
    (answer_dir / TASK_FILE).write_text(task, encoding="utf-8")

    run_answer(answer_dir, project_directory(project))

    answer = answer_dir / ANSWER_FILE
    if not answer.is_file():
        raise ListenerError(f"the answer run wrote no {ANSWER_FILE} in {answer_dir}")
    # Posting it makes autolab the last non-forge poster, which is exactly
    # what lets agforge's sweep pick the conversation back up.
    topic_write(topic, answer.read_text(encoding="utf-8").strip(),
                channel=channel, client=client)


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


def run_director(prompt: str, cwd: Path) -> str:
    """One discussion run in the direction clone, with its record."""
    record = next_record_path(RECORDS_ROOT / "director")
    output, _, exit_code = run_role(
        "director",
        prompt,
        cwd=cwd,
        timeout=DIRECTOR_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"director run exited {exit_code}: {output.strip()[:500]}")
    # The discussion notes the director recorded are committed by the caller
    # whatever it said here.
    return output.strip() or NO_CLOSING_MESSAGE


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
    work_dir = bmining_work_directory(project)
    try:
        context.step = "chatlog placement"
        work_dir.mkdir(parents=True, exist_ok=True)
        chatlog_path(work_dir).write_text(
            format_chatlog(context.history, context.self_id), encoding="utf-8"
        )

        context.step = "director"
        sections = [run_director(bmining_prompt(context.bot_name), direction_dir)]

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
    log(f"bmining topic {channel!r}/{topic!r}")
    serve_topic(client, channel, topic, serve_bmining, ack_text=ACK_TEXT, empty_reply=EMPTY_REPLY)


def observe_topic(channel: str, topic: str) -> None:
    """Passive handler (`AUTOLAB_ZULIP_LOG_ONLY=1`): log sweep matches, never act."""
    log(f"observed sweep match {channel!r}/{topic!r}")


def dispatch(client: ZulipClient, channel: str, topic: str) -> None:
    """Route one swept topic to its handler.

    `run-` topics work in any channel — they carry no project of their own,
    the project comes from the chosen Work. `mission-` and `create-` topics
    still need a `pj-*` channel and are silently ignored elsewhere: with
    `#general` now swept, a stray `mission-` topic there would otherwise get
    an error posted into it on every sweep, and agforge's own `create-` topics
    in `#FreeForge` are none of autolab's business.
    """
    if topic.startswith(RUN_TOPIC_PREFIX):
        handle_run(client, channel, topic)
        return
    if not channel.startswith(PROJECT_CHANNEL_PREFIX):
        log(f"ignoring {topic!r}: {channel!r} is not a project channel")
        return
    if topic.startswith(CREATE_TOPIC_PREFIX):
        handle_create(client, channel, topic)
        return
    if topic.startswith(BMINING_TOPIC_PREFIX):
        handle_bmining(client, channel, topic)
        return
    handle_topic(client, channel, topic)


def main() -> None:
    client = ZulipClient.from_env(ZULIP_ENV)
    if os.environ.get("AUTOLAB_ZULIP_LOG_ONLY") == "1":
        handler = observe_topic
    else:
        def handler(channel: str, topic: str) -> None:
            dispatch(client, channel, topic)

    # No subscription reconciliation: what this listener is subscribed to is
    # the project creator's decision about who the work goes to, not something
    # a listener may widen on its own. See pyagag's README, "Subscription is
    # the routing decision".
    prefixes = (
        MISSION_TOPIC_PREFIX,
        RUN_TOPIC_PREFIX,
        CREATE_TOPIC_PREFIX,
        BMINING_TOPIC_PREFIX,
    )
    log(f"agautolab zulip listener starting (pull sweep, prefixes {prefixes})")
    try:
        sweep_serve(client, handler, topic_filter=prefixes)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
