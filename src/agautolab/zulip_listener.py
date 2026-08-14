"""Pull mission topics from Zulip into topic workspaces and the front agent.

Phase 3 shape: `agag.zulip.sweep_serve` finds every unresolved `mission-*`
topic whose last poster is not this bot, and `handle_topic` serves each one —
ack, workspace, chatlog, Plane read-back, one front run, reply. The ack makes
the bot the last poster, so a topic being worked on is skipped by later
sweeps; a human posting during the run re-arms it and the post-run sweep
reprocesses with the fuller chatlog.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from agag.zulip import (
    ZulipClient,
    ZulipError,
    _safe_topic_component,
    log,
    sweep_serve,
    topic_write,
)

from .mission import (
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
PROJECT_CHANNEL_PREFIX = "pj-"
GENERAL_CHANNEL = "general"
HISTORY_MESSAGES = 1000
SUBSCRIBE_INTERVAL_SECONDS = 60

ACK_TEXT = "Message received. Please wait for the reply."

# One topic occupies the listener for at most the sum of these; the sweep
# loop is single-threaded and serial, so that sum is also the delay before
# the next matching topic is looked at (events keep queueing meanwhile).
FRONT_TIMEOUT_SECONDS = 360
CODING_TIMEOUT_SECONDS = 600
# Real work, not a task split: an order of magnitude more room.
WORK_TIMEOUT_SECONDS = 1200

__all__ = [
    "ZULIP_ENV",
    "dispatch",
    "format_chatlog",
    "front_prompt",
    "generation",
    "guide",
    "handle_front_response",
    "handle_run",
    "handle_topic",
    "main",
    "next_record_path",
    "run_coding",
    "run_front",
    "run_work",
    "subscribe_project_channels",
    "topic_workspace",
    "work_directory",
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
    """`.local/topics/<channel>/<topic>/` — stable, reused across runs.

    Leftovers from earlier runs are continuity, not garbage; nothing here is
    versioned or deleted.
    """
    return (
        TOPICS_ROOT
        / _safe_topic_component(channel, "channel")
        / _safe_topic_component(topic, "topic")
    )


def format_chatlog(messages: list[dict], self_id: int) -> str:
    lines = []
    for message in messages:
        speaker = message.get("sender_full_name") or f"user{message.get('sender_id')}"
        if message.get("sender_id") == self_id:
            speaker = f"{speaker} (you)"
        lines.append(f"[{speaker}] {str(message.get('content', '')).strip()}")
    return "\n".join(lines) + ("\n" if lines else "")


def guide(*parts: str) -> str:
    """Read one guide file under `agent/guides/`.

    The instruction text belongs to the agents, not to this transport. A
    missing guide is fatal on purpose: a listener that starts without it would
    silently send a prompt with no instruction in it.
    """
    path = GUIDES.joinpath(*parts)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ListenerError(f"cannot read guide {path}: {error}") from error
    if not text:
        raise ListenerError(f"guide is empty: {path}")
    return text


def front_prompt(bot_name: str, plane_files: bool) -> str:
    lines = [
        f"The chatlog is placed in the working directory. You are {bot_name!r} in the chatlog."
    ]
    if plane_files:
        lines.append("The current mission and tasks are also placed in the working directory.")
    return "\n".join(lines) + f"\n\n{guide('mission_front', 'guide_mission_topic.md')}"


def next_record_path(directory: Path) -> Path:
    """`run-NNNN.json`, numbered the same way the gateway numbers window runs."""
    directory.mkdir(parents=True, exist_ok=True)
    number = 1
    while (directory / f"run-{number:04d}.json").exists():
        number += 1
    return directory / f"run-{number:04d}.json"


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


def handle_topic(client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one awaiting mission topic, and always answer it.

    `sweep_serve` logs and survives handler exceptions, but a topic that gets
    an ack and then silence would stay dormant until a human posts again —
    so every exit path after the ack writes back how far the topic got.

    A human posting *during* a run is not lost either: the final reply makes
    this bot the last poster, which hides the topic from the next sweep, so
    before leaving this re-checks for human messages newer than the chatlog
    it processed and serves the topic again with the fuller chatlog.
    """
    log(f"mission topic {channel!r}/{topic!r}")
    self_user = client.whoami()
    self_id = int(self_user["user_id"])
    bot_name = str(self_user.get("full_name") or client.email)

    while True:
        topic_write(topic, ACK_TEXT, channel=channel, client=client)

        sections: list[str] = []
        processed_up_to = 0
        completed = False
        resolve_after = False
        step = "reading the topic"
        try:
            project = project_from_channel(channel)
            front_dir = topic_workspace(channel, topic) / "front"
            front_dir.mkdir(parents=True, exist_ok=True)

            step = "chatlog"
            history = client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)
            processed_up_to = max((int(m.get("id", 0)) for m in history), default=0)
            (front_dir / "chatlog.md").write_text(
                format_chatlog(history, self_id), encoding="utf-8"
            )

            step = "project setup"
            init_project(project)

            step = "plane read-back"
            plane_files = write_mission_workspace(front_dir, project, channel, topic)

            step = "front"
            # The front's answer is relayed verbatim; what it wrote in the
            # workspace (not what it said) drives the follow-up.
            sections.append(run_front(front_prompt(bot_name, plane_files), front_dir))

            step = "response handling"
            response_sections, resolve_after = handle_front_response(
                channel, topic, project, front_dir
            )
            sections.extend(response_sections)
            completed = True
        except Exception as error:  # noqa: BLE001 - the topic is the error channel
            log(f"mission topic workflow failed during {step}: {error!r}")
            sections.append(f"failed during {step}: {error}")

        topic_write(topic, "\n\n".join(section for section in sections if section),
                    channel=channel, client=client)

        if resolve_after:
            # After the final reply, so the whole conversation moves under
            # the ✔ name; a resolved topic stops matching the sweep, which
            # is the desired end state of a cancelled mission.
            try:
                history = client.topic_history(channel, topic, num_before=1)
                if history:
                    client.resolve_topic(int(history[-1]["id"]), topic)
            except Exception as error:  # noqa: BLE001
                log(f"could not resolve {channel!r}/{topic!r}: {error!r}")
            return
        if not completed:
            return  # do not loop on a failing topic; a human post re-arms it

        try:
            tail = client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)
        except ZulipError as error:
            log(f"post-run re-check failed for {channel!r}/{topic!r}: {error!r}")
            return
        if not any(
            m.get("sender_id") != self_id and int(m.get("id", 0)) > processed_up_to
            for m in tail
        ):
            return
        log(f"reprocessing {channel!r}/{topic!r}: human posts arrived during the run")


def generation(topic_dir: Path) -> int:
    """Increment and persist the topic's Sub-Work generation counter.

    Each accepted `new_mission.md` is one generation; the counter keeps new
    Sub-Work external keys clear of cancelled generations' keys, which live
    in Plane forever.
    """
    path = topic_dir / "generation"
    try:
        rev = int(path.read_text(encoding="utf-8").strip()) + 1
    except (OSError, ValueError):
        rev = 1
    path.write_text(f"{rev}\n", encoding="utf-8")
    return rev


def run_coding(coding_dir: Path) -> str:
    """One task-split run in the topic's coding workspace, with its record."""
    record = next_record_path(RECORDS_ROOT / "coding")
    output, _, exit_code = run_role(
        "coding",
        guide("mission_coding", "guide_task_split.md"),
        cwd=coding_dir,
        timeout=CODING_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"coding run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def handle_front_response(
    channel: str, topic: str, project: str, front_dir: Path
) -> tuple[list[str], bool]:
    """Act on what the front wrote: `new_mission.md`, then the flags.

    Returns the report sections and whether the topic should be resolved
    after the final reply. The command files are consumed once acted on —
    the workspace is stable and reused, so a leftover command would replay
    on every later run; the mission's canonical text lives in Plane and
    comes back as `mission.md` on the next read-back.
    """
    sections: list[str] = []
    resolve_after = False

    new_mission = front_dir / "new_mission.md"
    if new_mission.is_file():
        title, description = split_document(new_mission.read_text(encoding="utf-8"))
        sections.append(upsert_work(project, channel, topic, title, description))
        cancelled = cancel_sub_works(project, channel, topic)
        if cancelled:
            sections.append(f"cancelled {cancelled} existing sub-work(s)")
        coding_dir = front_dir.parent / "coding"
        coding_dir.mkdir(parents=True, exist_ok=True)
        for _, stale in task_files(coding_dir):
            stale.unlink()  # earlier generations' splits must not re-register
        (coding_dir / "new_mission.md").write_text(
            new_mission.read_text(encoding="utf-8"), encoding="utf-8"
        )
        sections.append(run_coding(coding_dir))
        rev = generation(front_dir.parent)
        sections.extend(register_task_files(project, channel, topic, coding_dir, rev))
        new_mission.unlink()

    start_flag = front_dir / "start.flag"
    if start_flag.is_file():
        label = transition_work(project, channel, topic, "started")
        sections.append(f"mission {label} is now In Progress")
        start_flag.unlink()

    cancel_flag = front_dir / "cancel.flag"
    if cancel_flag.is_file():
        cancelled = cancel_sub_works(project, channel, topic)
        label = transition_work(project, channel, topic, "cancelled")
        suffix = f" along with {cancelled} sub-work(s)" if cancelled else ""
        sections.append(f"mission {label} is cancelled{suffix}; resolving this topic")
        cancel_flag.unlink()
        resolve_after = True

    return sections, resolve_after


def work_directory(slug: str) -> Path:
    """`.local/work/` inside the project's shared `main` clone.

    The work run happens in that clone, the same directory every later run
    reuses; `.local/work/` is the one place this phase writes and deletes.
    """
    return PROJECTS_ROOT / slug / "main" / ".local" / "work"


def run_work(workspace: Path) -> str:
    """One work run in a project's `main` clone, with its `ag.agent-run.v1` record."""
    record = next_record_path(RECORDS_ROOT / "run")
    output, _, exit_code = run_role(
        "coding",
        guide("run_coding", "guide_run_coding.md"),
        cwd=workspace,
        timeout=WORK_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"work run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def remove_work_directory(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


def handle_run(client: ZulipClient, channel: str, topic: str) -> None:
    """Execute one Work, triggered by any non-bot post in a `run-` topic.

    The chatlog is never read: a `run-` topic is a button, not a conversation.
    Whatever the topic gets, one eligible Work is chosen from Plane
    (`next_work`), executed in its project's `main` clone, and reported back
    to both Plane and the topic. Every exit path after the ack posts
    something, the same discipline `handle_topic` follows.
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
        slug, name, description, project_id, issue_id = chosen
        workspace = PROJECTS_ROOT / slug / "main"
        candidate = work_directory(slug)
        if candidate.is_dir() and any(candidate.iterdir()):
            topic_write(
                topic,
                f"work dirty: {slug}/main has a leftover .local/work/; "
                "remove it by hand and trigger again",
                channel=channel,
                client=client,
            )
            return
        sections.append(f'running "{name}" in {slug}')

        step = "writing the work"
        # Only now does the directory become this run's to delete.
        work_dir = candidate
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "work.md").write_text(
            compose_document(name, description_html(description)), encoding="utf-8"
        )

        step = "work run"
        sections.append(run_work(workspace))

        step = "reporting to plane"
        report_path = work_dir / "report.md"
        report = report_path.read_text(encoding="utf-8") if report_path.is_file() else None
        success = (work_dir / "success.flag").exists()
        if report is None:
            sections.append("no report")
        work_label, commented, completed = report_work(project_id, issue_id, report, success)
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
    the project comes from the chosen Work. `mission-` topics still need a
    `pj-*` channel and are silently ignored elsewhere: with `#general` now
    swept, a stray `mission-` topic there would otherwise get an error posted
    into it on every sweep.
    """
    if topic.startswith(RUN_TOPIC_PREFIX):
        handle_run(client, channel, topic)
        return
    if not channel.startswith(PROJECT_CHANNEL_PREFIX):
        log(f"ignoring {topic!r}: {channel!r} is not a project channel")
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
    prefixes = (MISSION_TOPIC_PREFIX, RUN_TOPIC_PREFIX)
    log(f"agautolab zulip listener starting (pull sweep, prefixes {prefixes})")
    try:
        sweep_serve(client, handler, topic_filter=prefixes)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
