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

from .mission import write_mission_workspace
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

# One topic occupies the listener for at most this long; the sweep loop is
# single-threaded and serial, so this is also the delay before the next
# matching topic is looked at (events keep queueing meanwhile).
FRONT_TIMEOUT_SECONDS = 360

__all__ = [
    "ZULIP_ENV",
    "format_chatlog",
    "front_prompt",
    "guide",
    "handle_topic",
    "main",
    "next_record_path",
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
        sections.extend(handle_front_response(front_dir))
    except Exception as error:  # noqa: BLE001 - the topic is the error channel
        log(f"mission topic workflow failed during {step}: {error!r}")
        sections.append(f"failed during {step}: {error}")

    topic_write(topic, "\n\n".join(section for section in sections if section),
                channel=channel, client=client)


def handle_front_response(front_dir: Path) -> list[str]:
    """Act on what the front wrote (`new_mission.md`, flags). Step 3 fills this in."""
    return []


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
