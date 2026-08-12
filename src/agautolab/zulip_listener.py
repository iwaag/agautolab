"""agautolab's chat entrance — the stub. `mission-*` topics are still heard.

The standing-channel / disposable-topic pattern (zulip_channel_topic2): each
autolab project has a `#pj-<name>` channel, each mission is one `mission-*`
topic there. This listener was a **transport, not an agent**: it forwarded the
topic's text to a node's `POST /window`, replied in-topic, and followed
`GET /status` until the mission ended.

The transport half is gone with the implementation behind it (the
`discard_garbage` episode). What remains is the surface: the acceptance rule
(topic-prefix-only and channel-agnostic, exactly like agforge's `create-*`
rule, so a resolved `✔ mission-…` topic stops matching by itself), the
briefing wrapper that defines what a node would have been sent, and the
log-only switch. A mission topic now gets one reply saying the node is a stub,
and nothing is started.

Configuration (environment):
- ``AUTOLAB_NODE_URL``      — the node gateway (default http://agautolab1.local:8791)
- ``AUTOLAB_MAX_SESSIONS``  — session budget that would travel with a briefing
                              (default 20; S5 died on a window-chosen budget of 2)
- ``AUTOLAB_ZULIP_LOG_ONLY``— "1" observes and never posts
"""

from __future__ import annotations

import os
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

STUB_REPLY = (
    "This briefing was heard but not started: the autolab node is a stub. Its "
    "entrance and its agent configuration are intact, but the auto-development "
    "loop behind them has been removed, so no mission runs and no outcome will "
    "be posted here."
)

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
    briefing; this wrapper only adds the start instruction and the budget.

    Nothing sends this any more. It is kept because it, not the transport, is
    what defined the contract between a topic and a node.
    """
    return (
        f"Start a mission with max_sessions={budget} (emit the "
        f"<<mission max_sessions={budget}>> block around the whole briefing "
        f"below and close it with <</mission>>).\n\n{content}"
    )


def handle_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Answer one mission-topic message. Bridges nothing, blocks on nothing.

    The old handler polled the node's `/status` until the driver stopped. A
    stub must not inherit that loop: there is no driver to stop, and the
    listener serves one message at a time.
    """
    channel = channel_name(message)
    topic = str(message.get("subject", ""))
    content = str(message.get("content", ""))
    log(f"mission topic message #{message.get('id')} in {channel!r}/{topic!r}")
    log(
        f"not bridged to {node_url()}; the briefing would have been: "
        f"{bridge_briefing(content, max_sessions())[:200]!r}"
    )
    client.send_to_channel(channel, topic, STUB_REPLY)


def observe_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Passive handler (`AUTOLAB_ZULIP_LOG_ONLY=1`): log, never post."""
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
        f"agautolab zulip listener starting (stub, handler={handler.__name__}, "
        f"node={node_url()}, max_sessions={max_sessions()})"
    )
    try:
        serve(client, handler, accept=accept)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
