"""Pull mission topics from Zulip into topic workspaces and the front agent.

`agag.zulip.sweep_serve` finds every unresolved `mission-*` topic whose last
poster is not this bot, and `agag.topics.serve_topic` serves each one — the
skeleton shared with agforge: ack, generation workspace, chatlog, the steps,
always reply naming the failed step, then re-check for human posts that
arrived during the run.

Each serving cuts a new generation directory `<N>/`, the way agforge's create
topics do. Before that, one stable `front/` directory was reused forever, so a
continued conversation ran on top of the previous run's leftovers and a
separate `generation` counter file had to keep Sub-Work keys apart. `N` is now
both the workspace and that key.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from agag.topics import (
    TopicResult,
    chatlog_placement,
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
    ZulipClient,
    ZulipError,
    log,
    sweep_serve,
    topic_write,
)

from .mission import (
    ASSET_TOPIC_PREFIX,
    Work,
    asset_answer_context,
    asset_order,
    asset_topic,
    cancel_sub_works,
    compose_document,
    description_html,
    next_work,
    register_task_files,
    report_work,
    split_document,
    task_files,
    transition_work,
    upsert_work,
    write_mission_workspace,
)
from .project_init import PROJECT_NAME, PROJECTS_ROOT, init_project
from .role_run import run_role

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"
TOPICS_ROOT = AGAUTOLAB_ROOT / ".local" / "topics"
GUIDES = AGAUTOLAB_ROOT / "agent" / "guides"
RECORDS_ROOT = AGAUTOLAB_ROOT / ".local" / "agent"

MISSION_TOPIC_PREFIX = "mission-"
RUN_TOPIC_PREFIX = "run-"
CREATE_TOPIC_PREFIX = "create-"
PROJECT_CHANNEL_PREFIX = "pj-"
GENERAL_CHANNEL = "general"
HISTORY_MESSAGES = 1000
SUBSCRIBE_INTERVAL_SECONDS = 60

ACK_TEXT = "Message received. Please wait for the reply."
EMPTY_REPLY = "There is nothing in this topic to answer yet."

# The planning round's files, all of them in the persistent project folder.
MISSION_FILE = "new_mission.md"
PLAN_FILE = "plan.md"

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

# One topic occupies the listener for at most the sum of these; the sweep
# loop is single-threaded and serial, so that sum is also the delay before
# the next matching topic is looked at (events keep queueing meanwhile).
FRONT_TIMEOUT_SECONDS = 360
# Real work, not a task split: an order of magnitude more room.
WORK_TIMEOUT_SECONDS = 1200
# The superdirector reads the whole project before it plans. The disposable
# coding split it replaced ran on 600 s while seeing only `new_mission.md`;
# this one sees `main/`, `direction/` and `devlog/`, so it gets the work
# timeout rather than the split's.
SUPERDIRECTOR_TIMEOUT_SECONDS = WORK_TIMEOUT_SECONDS

__all__ = [
    "ZULIP_ENV",
    "aesthetics_text",
    "answer_prompt",
    "asset_gate",
    "asset_order_text",
    "dispatch",
    "find_asset_key",
    "format_chatlog",
    "front_prompt",
    "generation_dir",
    "guide",
    "handle_create",
    "handle_front_response",
    "handle_run",
    "handle_topic",
    "main",
    "mentions_us",
    "next_record_path",
    "run_answer",
    "project_channel",
    "project_directory",
    "resign",
    "run_front",
    "run_superdirector",
    "run_work",
    "subscribe_project_channels",
    "serve",
    "topic_workspace",
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


def front_prompt(bot_name: str, plane_files: bool) -> str:
    lines = [chatlog_placement(bot_name)]
    if plane_files:
        lines.append("The current mission and tasks are also placed in the working directory.")
    return prompt_with_guide(lines, guide("mission_front", "guide.md"))


def run_front(prompt: str, cwd: Path) -> str:
    """One front run in the topic workspace, with its `ag.agent-run.v1` record."""
    record = next_record_path(RECORDS_ROOT / "front")
    output, _, exit_code = run_role(
        "front",
        prompt,
        cwd=cwd,
        timeout=FRONT_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"front run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def serve(context) -> TopicResult:
    """agautolab's part of one serving: project setup, Plane read-back, front.

    `handle_front_response` then acts on what the front *wrote* — its answer
    is relayed verbatim and never parsed.
    """
    project = project_from_channel(context.channel)
    number = next_generation(topic_workspace(context.channel, context.topic))
    front_dir = generation_dir(context.channel, context.topic, number, "front")
    chatlog_path(front_dir).write_text(
        format_chatlog(context.history, context.self_id), encoding="utf-8"
    )

    context.step = "project setup"
    init_project(project)

    context.step = "plane read-back"
    plane_files = write_mission_workspace(front_dir, project, context.channel, context.topic)

    context.step = "front"
    sections = [run_front(front_prompt(context.bot_name, plane_files), front_dir)]

    context.step = "response handling"
    response_sections, resolve_after = handle_front_response(
        context.channel, context.topic, project, front_dir, number
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


def run_superdirector(project_dir: Path) -> str:
    """One mission-planning run in the project folder, with its record.

    This replaced a `coding` run in a disposable `<N>/coding/` workspace that
    saw nothing but the copied `new_mission.md`. Planning a mission means
    weighing the code against the direction documents and the devlog, so the
    run happens where all three are — and the role is its own, not `coding`.
    """
    record = next_record_path(RECORDS_ROOT / "superdirector")
    output, _, exit_code = run_role(
        "superdirector",
        guide("mission_superdirector", "guide.md"),
        cwd=project_dir,
        timeout=SUPERDIRECTOR_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"superdirector run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def clear_planning_files(project_dir: Path) -> None:
    """Remove the planning round's files from the project folder.

    Called only after Plane has accepted the whole round. Plane holds the
    canonical copies from that moment on, and Step 6 reads them back from
    there; leaving the files would mean a later round's registration could
    pick up a stale `task[N].md` that the superdirector did not write.
    """
    for path in [
        project_dir / MISSION_FILE,
        project_dir / PLAN_FILE,
        *(path for _, path in task_files(project_dir)),
    ]:
        path.unlink(missing_ok=True)


def handle_front_response(
    channel: str, topic: str, project: str, front_dir: Path, number: int
) -> tuple[list[str], bool]:
    """Act on what the front wrote: `new_mission.md`, then the flags.

    Returns the report sections and whether the topic should be resolved
    after the final reply.

    The front's own workspace is still a fresh generation `<N>/` and nothing
    in it is deleted — the generation number is the guard, and the leftovers
    stay as evidence of what that run was told. `N` is also the Sub-Work
    generation key, so keys from a cancelled generation (which live in Plane
    forever) cannot be reused.

    The *planning* files are different. They live in the persistent project
    folder, where no generation number separates one round from the next, so
    they are cleared once Plane has taken them — and deliberately left behind
    when it has not, so a retry finds the plan instead of paying for it twice.
    """
    sections: list[str] = []
    resolve_after = False

    new_mission = front_dir / MISSION_FILE
    if new_mission.is_file():
        project_dir = project_directory(project)
        project_dir.mkdir(parents=True, exist_ok=True)
        mission_text = new_mission.read_text(encoding="utf-8")
        (project_dir / MISSION_FILE).write_text(mission_text, encoding="utf-8")

        sections.append(run_superdirector(project_dir))

        plan = project_dir / PLAN_FILE
        if not plan.is_file():
            raise ListenerError(
                f"the superdirector wrote no {PLAN_FILE} in {project_dir}"
            )
        # Title from the mission, description from the plan: the Work is the
        # mission the requester asked for, described by what the
        # superdirector decided to do about it. Step 6 reads this back as
        # `plan.md`, so the whole file travels, heading included.
        title, _ = split_document(mission_text)
        sections.append(
            upsert_work(project, channel, topic, title, plan.read_text(encoding="utf-8"))
        )
        cancelled = cancel_sub_works(project, channel, topic)
        if cancelled:
            sections.append(f"cancelled {cancelled} existing sub-work(s)")
        sections.extend(register_task_files(project, channel, topic, project_dir, number))
        clear_planning_files(project_dir)

    start_flag = front_dir / "start.flag"
    if start_flag.is_file():
        label = transition_work(project, channel, topic, "started")
        sections.append(f"mission {label} is now In Progress")

    cancel_flag = front_dir / "cancel.flag"
    if cancel_flag.is_file():
        cancelled = cancel_sub_works(project, channel, topic)
        label = transition_work(project, channel, topic, "cancelled")
        suffix = f" along with {cancelled} sub-work(s)" if cancelled else ""
        sections.append(f"mission {label} is cancelled{suffix}; resolving this topic")
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


def run_work(workspace: Path, asset_url: str | None = None) -> str:
    """One work run in a project's `main` clone, with its `ag.agent-run.v1` record."""
    record = next_record_path(RECORDS_ROOT / "run")
    output, _, exit_code = run_role(
        "coding",
        work_prompt(asset_url),
        cwd=workspace,
        timeout=WORK_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"work run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


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
        sections.append(run_work(workspace, asset_url))

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


def subscribe_project_channels(client: ZulipClient) -> list[str]:
    """Put every active realm user in every `pj-*` channel and in `#general`.

    A project channel is a shared room, not the autolab bot's private inbox:
    all agents participate and each one filters for the topics it owns. This
    also covers a channel a human created by hand, which no agent would
    otherwise be in — and Zulip delivers no events for an unsubscribed channel,
    so the sweep can only see subscribed channels. `#general` is reconciled the
    same way because it is where `run-` topics live.
    """
    everyone = {
        int(user["user_id"]) for user in client.users() if user.get("is_active", True)
    }
    reconciled = []
    for channel in client.channels():
        name = str(channel.get("name", ""))
        if not (name.startswith(PROJECT_CHANNEL_PREFIX) or name == GENERAL_CHANNEL):
            continue
        missing = sorted(everyone - set(client.channel_subscribers(channel["stream_id"])))
        if missing:
            client.subscribe_channels([name], principals=missing)
            reconciled.append(name)
            log(f"subscribed {len(missing)} user(s) to {name}")
    return reconciled


def subscription_loop(client: ZulipClient) -> None:
    while True:
        time.sleep(SUBSCRIBE_INTERVAL_SECONDS)
        try:
            subscribe_project_channels(client)
        except Exception as error:
            log(f"project channel subscription refresh failed: {error!r}")


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
    handle_topic(client, channel, topic)


def main() -> None:
    client = ZulipClient.from_env(ZULIP_ENV)
    if os.environ.get("AUTOLAB_ZULIP_LOG_ONLY") == "1":
        handler = observe_topic
    else:
        def handler(channel: str, topic: str) -> None:
            dispatch(client, channel, topic)

    subscription_client = ZulipClient.from_env(ZULIP_ENV)
    try:
        subscribe_project_channels(subscription_client)
    except ZulipError as error:
        log(f"initial project channel subscription refresh failed: {error!r}")
    threading.Thread(target=subscription_loop, args=(subscription_client,), daemon=True).start()
    prefixes = (MISSION_TOPIC_PREFIX, RUN_TOPIC_PREFIX, CREATE_TOPIC_PREFIX)
    log(f"agautolab zulip listener starting (pull sweep, prefixes {prefixes})")
    try:
        sweep_serve(client, handler, topic_filter=prefixes)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
