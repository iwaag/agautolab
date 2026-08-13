"""Bridge mission topics into idempotent project setup and the front window."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from agag.zulip import (
    ZulipClient,
    ZulipError,
    channel_name,
    is_channel_message_for_us,
    log,
    serve,
    topic_dump,
    topic_write,
)

from .project_init import PROJECT_NAME, init_project

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"
FRONT_WORKSPACE = AGAUTOLAB_ROOT / "agent" / "front"

MISSION_TOPIC_PREFIX = "mission-"
PROJECT_CHANNEL_PREFIX = "pj-"
DEFAULT_NODE_URL = "http://127.0.0.1:8791"
HISTORY_MESSAGES = 1000
SUBSCRIBE_INTERVAL_SECONDS = 60

__all__ = [
    "ZULIP_ENV",
    "accept",
    "format_chatlog",
    "handle_message",
    "main",
    "subscribe_project_channels",
    "window_prompt",
]


class ListenerError(RuntimeError):
    """One mission-topic workflow could not complete."""


def node_url() -> str:
    return os.environ.get("AUTOLAB_NODE_URL", DEFAULT_NODE_URL).rstrip("/")


def accept(message: dict, self_id: int) -> bool:
    """Channel messages in a live `mission-*` topic, any subscribed channel."""
    return is_channel_message_for_us(message, self_id) and str(
        message.get("subject", "")
    ).startswith(MISSION_TOPIC_PREFIX)


def project_from_channel(channel: str) -> str:
    if not channel.startswith(PROJECT_CHANNEL_PREFIX):
        raise ListenerError(f"mission topic is not in a {PROJECT_CHANNEL_PREFIX} channel: {channel}")
    project = channel.removeprefix(PROJECT_CHANNEL_PREFIX)
    if not PROJECT_NAME.fullmatch(project):
        raise ListenerError(f"channel does not contain a valid project name: {channel}")
    return project


def format_chatlog(messages: list[dict], self_id: int) -> str:
    lines = []
    for message in messages:
        speaker = message.get("sender_full_name") or f"user{message.get('sender_id')}"
        if message.get("sender_id") == self_id:
            speaker = f"{speaker} (you)"
        lines.append(f"[{speaker}] {str(message.get('content', '')).strip()}")
    return "\n".join(lines) + ("\n" if lines else "")


def window_prompt(dump_notice: str) -> str:
    return (
        f"{dump_notice}\n\n"
        "You are already running in the front workspace. The relative chat-log path above and "
        "`new_mission.py` exist beneath your current working directory. Use your tools directly: "
        "read the chat log, then run `uv run new_mission.py --help` to learn the interface. If "
        "you determine that the chat requests a mission, add it and report the result. Do not ask "
        "for path clarification unless you first run `pwd`, inspect both paths, and report the "
        "exact command error."
    )


def call_window(text: str) -> str:
    request = urllib.request.Request(
        f"{node_url()}/window",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=360) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise ListenerError(f"window returned HTTP {error.code}: {detail}") from error
    except (OSError, TimeoutError, json.JSONDecodeError) as error:
        raise ListenerError(f"window call failed: {error}") from error
    reply = payload.get("reply") if isinstance(payload, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        raise ListenerError("window response did not contain a non-empty reply")
    return reply


def handle_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Run the four mission-topic workflows in order."""
    channel = channel_name(message)
    topic = str(message.get("subject", ""))
    project = project_from_channel(channel)
    log(f"mission topic message #{message.get('id')} in {channel!r}/{topic!r}")

    history = client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)
    dump_notice = topic_dump(
        channel,
        topic,
        format_chatlog(history, self_id),
        cwd=FRONT_WORKSPACE,
    )
    init_project(project)
    reply = call_window(window_prompt(dump_notice))
    topic_write(topic, reply, channel=channel, client=client)


def observe_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Passive handler (`AUTOLAB_ZULIP_LOG_ONLY=1`): log, never mutate."""
    log(
        f"observed #{message.get('id')} in {channel_name(message)!r}/"
        f"{message.get('subject')!r}: {str(message.get('content', ''))[:200]!r}"
    )


def subscribe_project_channels(client: ZulipClient) -> list[str]:
    available = {
        str(row.get("name"))
        for row in client.channels()
        if str(row.get("name", "")).startswith(PROJECT_CHANNEL_PREFIX)
    }
    subscribed = {str(row.get("name")) for row in client.subscriptions()}
    missing = sorted(available - subscribed)
    if missing:
        client.subscribe_channels(missing)
        log(f"subscribed to project channels: {', '.join(missing)}")
    return missing


def subscription_loop(client: ZulipClient) -> None:
    while True:
        time.sleep(SUBSCRIBE_INTERVAL_SECONDS)
        try:
            subscribe_project_channels(client)
        except Exception as error:
            log(f"project channel subscription refresh failed: {error!r}")


def main() -> None:
    handler = handle_message
    if os.environ.get("AUTOLAB_ZULIP_LOG_ONLY") == "1":
        handler = observe_message
    client = ZulipClient.from_env(ZULIP_ENV)
    subscription_client = ZulipClient.from_env(ZULIP_ENV)
    try:
        subscribe_project_channels(subscription_client)
    except ZulipError as error:
        log(f"initial project channel subscription refresh failed: {error!r}")
    threading.Thread(target=subscription_loop, args=(subscription_client,), daemon=True).start()
    log(
        f"agautolab zulip listener starting (handler={handler.__name__}, node={node_url()})"
    )
    try:
        serve(client, handler, accept=accept)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
