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
    register_task_files,
    split_document,
    task_files,
    transition_work,
    upsert_work,
    write_mission_workspace,
)
from .project_init import PROJECT_NAME, init_project
from .role_run import run_role

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"
TOPICS_ROOT = AGAUTOLAB_ROOT / ".local" / "topics"
GUIDES = AGAUTOLAB_ROOT / "agent" / "guides"
RECORDS_ROOT = AGAUTOLAB_ROOT / ".local" / "agent"

MISSION_TOPIC_PREFIX = "mission-"
PROJECT_CHANNEL_PREFIX = "pj-"
HISTORY_MESSAGES = 1000
SUBSCRIBE_INTERVAL_SECONDS = 60

ACK_TEXT = "Message received. Please wait for the reply."

# One topic occupies the listener for at most the sum of these; the sweep
# loop is single-threaded and serial, so that sum is also the delay before
# the next matching topic is looked at (events keep queueing meanwhile).
FRONT_TIMEOUT_SECONDS = 360
CODING_TIMEOUT_SECONDS = 600

__all__ = [
    "ZULIP_ENV",
    "format_chatlog",
    "front_prompt",
    "generation",
    "guide",
    "handle_front_response",
    "handle_topic",
    "main",
    "next_record_path",
    "run_coding",
    "run_front",
    "subscribe_project_channels",
    "topic_workspace",
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
    """Serve one awaiting mission topic and always answer it.

    `sweep_serve` logs and survives handler exceptions, but a topic that gets
    an ack and then silence would stay dormant until a human posts again —
    so every exit path after the ack writes back how far the topic got.
    """
    log(f"mission topic {channel!r}/{topic!r}")
    self_user = client.whoami()
    self_id = int(self_user["user_id"])
    bot_name = str(self_user.get("full_name") or client.email)

    topic_write(topic, ACK_TEXT, channel=channel, client=client)

    sections: list[str] = []
    step = "reading the topic"
    try:
        project = project_from_channel(channel)
        front_dir = topic_workspace(channel, topic) / "front"
        front_dir.mkdir(parents=True, exist_ok=True)

        step = "chatlog"
        history = client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)
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
    except Exception as error:  # noqa: BLE001 - the topic is the error channel
        log(f"mission topic workflow failed during {step}: {error!r}")
        sections.append(f"failed during {step}: {error}")
        resolve_after = False

    topic_write(topic, "\n\n".join(section for section in sections if section),
                channel=channel, client=client)

    if resolve_after:
        # After the final reply, so the whole conversation moves under the
        # ✔ name; a resolved topic stops matching the sweep, which is the
        # desired end state of a cancelled mission.
        try:
            history = client.topic_history(channel, topic, num_before=1)
            if history:
                client.resolve_topic(int(history[-1]["id"]), topic)
        except Exception as error:  # noqa: BLE001
            log(f"could not resolve {channel!r}/{topic!r}: {error!r}")


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


def subscribe_project_channels(client: ZulipClient) -> list[str]:
    """Put every active realm user in every `pj-*` channel.

    A project channel is a shared room, not the autolab bot's private inbox:
    all agents participate and each one filters for the topics it owns. This
    also covers a channel a human created by hand, which no agent would
    otherwise be in — and Zulip delivers no events for an unsubscribed channel,
    so the sweep can only see subscribed channels.
    """
    everyone = {
        int(user["user_id"]) for user in client.users() if user.get("is_active", True)
    }
    reconciled = []
    for channel in client.channels():
        name = str(channel.get("name", ""))
        if not name.startswith(PROJECT_CHANNEL_PREFIX):
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


def main() -> None:
    client = ZulipClient.from_env(ZULIP_ENV)
    if os.environ.get("AUTOLAB_ZULIP_LOG_ONLY") == "1":
        handler = observe_topic
    else:
        def handler(channel: str, topic: str) -> None:
            handle_topic(client, channel, topic)

    subscription_client = ZulipClient.from_env(ZULIP_ENV)
    try:
        subscribe_project_channels(subscription_client)
    except ZulipError as error:
        log(f"initial project channel subscription refresh failed: {error!r}")
    threading.Thread(target=subscription_loop, args=(subscription_client,), daemon=True).start()
    log("agautolab zulip listener starting (pull sweep, prefix "
        f"{MISSION_TOPIC_PREFIX!r})")
    try:
        sweep_serve(client, handler, topic_filter=MISSION_TOPIC_PREFIX)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
