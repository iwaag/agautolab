"""agautolab's chat entrance: `mission-*` topics bridge to the node window.

The standing-channel / disposable-topic pattern (zulip_channel_topic2): each
autolab project has a `#pj-<name>` channel, each mission is one `mission-*`
topic there. This listener is a **transport, not an agent**: it forwards the
topic's text to the configured node's `POST /window` — the node's single
desire entrance — replies in-topic with the window's answer, and when a
mission starts it follows `GET /status` and posts the terminal outcome in
the same topic. The acceptance rule is topic-prefix-only and
channel-agnostic, exactly like agforge's `create-*` rule: disjoint prefixes
are the whole filter, and a resolved `✔ mission-…` topic stops matching by
itself.

Configuration (environment):
- ``AUTOLAB_NODE_URL``      — the node gateway (default http://agautolab1.local:8791)
- ``AUTOLAB_MAX_SESSIONS``  — session budget passed with each briefing (default 20;
                              S5 died on a window-chosen budget of 2)
- ``AUTOLAB_ZULIP_LOG_ONLY``— "1" observes and never posts or bridges
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from agag.zulip import (
    ZulipClient,
    channel_name,
    is_channel_message_for_us,
    log,
    serve,
)

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"

MISSION_TOPIC_PREFIX = "mission-"

DEFAULT_NODE_URL = "http://agautolab1.local:8791"
WINDOW_TIMEOUT_SECONDS = 320  # the window's own budget is 300s
STATUS_POLL_SECONDS = 30

__all__ = ["ZULIP_ENV", "accept", "bridge_briefing", "handle_message", "main"]


def node_url() -> str:
    return os.environ.get("AUTOLAB_NODE_URL", DEFAULT_NODE_URL).rstrip("/")


def max_sessions() -> int:
    return int(os.environ.get("AUTOLAB_MAX_SESSIONS", "20"))


def accept(message: dict, self_id: int) -> bool:
    """Channel messages in a live `mission-*` topic, any subscribed channel."""
    return is_channel_message_for_us(message, self_id) and str(
        message.get("subject", "")
    ).startswith(MISSION_TOPIC_PREFIX)


def bridge_briefing(content: str, budget: int) -> str:
    """The window text for a mission briefing. The topic text is the whole
    briefing; this wrapper only adds the start instruction and the budget."""
    return (
        f"Start a mission with max_sessions={budget} (emit the "
        f"<<mission max_sessions={budget}>> block around the whole briefing "
        f"below and close it with <</mission>>).\n\n{content}"
    )


def post_window(text: str) -> dict:
    """One POST /window call; the response record is the answer."""
    request = urllib.request.Request(
        f"{node_url()}/window",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WINDOW_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read())
        except ValueError:
            return {"outcome": "failed", "failure": f"window HTTP {error.code}"}


def get_status() -> dict:
    with urllib.request.urlopen(f"{node_url()}/status", timeout=30) as response:
        return json.loads(response.read())


def wait_for_terminal_status(poll_seconds: float = STATUS_POLL_SECONDS) -> dict:
    """Follow /status until the driver stops; network blips just retry."""
    while True:
        try:
            status = get_status()
            if not status.get("driver", {}).get("running"):
                return status
        except (OSError, ValueError) as error:
            log(f"status poll failed ({error}); retrying")
        time.sleep(poll_seconds)


def terminal_message(status: dict) -> str:
    done = status.get("done")
    exit_code = status.get("driver", {}).get("exit_code")
    if done:
        return f"Mission ended (driver exit {exit_code}). The agent's note:\n\n{done}"
    if exit_code == 10:
        return "Mission ended: the session budget ran out with the agent still working (driver exit 10)."
    if exit_code == 11:
        return (
            "Mission failed: three consecutive sessions never consumed the "
            "mission (driver exit 11)."
        )
    return f"Mission ended without a done note (driver exit {exit_code})."


def handle_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Bridge one mission-topic message to the node window and report back."""
    channel = channel_name(message)
    topic = str(message.get("subject", ""))
    content = str(message.get("content", ""))
    log(f"mission topic message #{message.get('id')} in {channel!r}/{topic!r}")
    budget = max_sessions()
    record = post_window(bridge_briefing(content, budget))
    mission = record.get("mission")
    if record.get("outcome") != "done":
        client.send_to_channel(
            channel, topic,
            f"The node window failed to answer: {record.get('failure', 'no detail')}",
        )
        return
    if mission is None:
        reply = record.get("reply") or "(the window answered without starting a mission)"
        client.send_to_channel(channel, topic, reply)
        return
    if mission.get("status") == 409:
        client.send_to_channel(
            channel, topic,
            "The node is already running a mission; this briefing was not started. "
            "Post again once the current mission ends.",
        )
        return
    if not mission.get("accepted"):
        client.send_to_channel(
            channel, topic,
            f"The node refused the mission (HTTP {mission.get('status')}): "
            f"{mission.get('error', 'no detail')}",
        )
        return
    run = mission.get("run")
    client.send_to_channel(
        channel, topic,
        f"Mission started on the node (run {run}, max_sessions={budget}). "
        f"I will post the outcome here when the driver ends.",
    )
    status = wait_for_terminal_status()
    client.send_to_channel(channel, topic, terminal_message(status))


def observe_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Passive handler (`AUTOLAB_ZULIP_LOG_ONLY=1`): log, never bridge."""
    log(
        f"observed #{message.get('id')} in {channel_name(message)!r}/"
        f"{message.get('subject')!r}: {str(message.get('content', ''))[:200]!r}"
    )


def main() -> None:
    handler = handle_message
    if os.environ.get("AUTOLAB_ZULIP_LOG_ONLY") == "1":
        handler = observe_message
    client = ZulipClient.from_env(ZULIP_ENV)
    log(
        f"agautolab zulip listener starting (handler={handler.__name__}, "
        f"node={node_url()}, max_sessions={max_sessions()})"
    )
    try:
        serve(client, handler, accept=accept)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
