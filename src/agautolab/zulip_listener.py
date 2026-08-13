"""Bridge mission topics into idempotent project setup and the front window."""

from __future__ import annotations

import json
import os
import subprocess
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
from .role_run import run_role

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"
FRONT_WORKSPACE = AGAUTOLAB_ROOT / "agent" / "front"
GUIDES = AGAUTOLAB_ROOT / "agent" / "guides"
CODING_RECORDS = AGAUTOLAB_ROOT / ".local" / "agent" / "coding"

MISSION_TOPIC_PREFIX = "mission-"
PROJECT_CHANNEL_PREFIX = "pj-"
DEFAULT_NODE_URL = "http://127.0.0.1:8791"
HISTORY_MESSAGES = 1000
SUBSCRIBE_INTERVAL_SECONDS = 60

# One message occupies the listener for at most the sum of these; `serve()` is
# single-threaded, so that sum is also the delay before the next mission topic
# is looked at. Kept together so the total stays visible.
WINDOW_TIMEOUT_SECONDS = 360
CODING_TIMEOUT_SECONDS = 600
REGISTER_TIMEOUT_SECONDS = 180

__all__ = [
    "ZULIP_ENV",
    "absolute_dump_notice",
    "accept",
    "coding_prompt",
    "format_chatlog",
    "guide",
    "handle_message",
    "main",
    "register_mission",
    "run_coding",
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


def absolute_dump_notice(notice: str) -> str:
    """Rewrite `topic_dump`'s front-relative path as an absolute one.

    The dump stays front-relative on disk; only the sentence handed to the
    window is absolutised. Measured on the local front profile: the relative
    form is reconstructed against a guessed root (home, or the repository root)
    and fails most runs, while the absolute form is opened on the first turn.
    """
    relative, separator, rest = notice.partition(" ")
    return f"{FRONT_WORKSPACE / relative}{separator}{rest}"


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


def window_prompt(dump_notice: str) -> str:
    return f"{dump_notice}\n\n{guide('front', 'guide_mission_topic.md')}"


def coding_prompt(mission_path: str) -> str:
    return (
        f"{mission_path} is the file that describes the mission.\n\n"
        f"{guide('coding', 'guide_task_split.md')}"
    )


def call_window(text: str) -> str:
    request = urllib.request.Request(
        f"{node_url()}/window",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WINDOW_TIMEOUT_SECONDS) as response:
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


def dump_directory(dump_notice: str) -> str:
    """The front-relative `<N>/` directory carried by `topic_dump`'s sentence."""
    relative = dump_notice.partition(" ")[0]
    if not relative.endswith("chatlog.txt"):
        raise ListenerError(f"unexpected dump notice: {dump_notice!r}")
    return str(Path(relative).parent)


def next_record_path(directory: Path) -> Path:
    """`run-NNNN.json`, numbered the same way the gateway numbers window runs."""
    directory.mkdir(parents=True, exist_ok=True)
    number = 1
    while (directory / f"run-{number:04d}.json").exists():
        number += 1
    return directory / f"run-{number:04d}.json"


def run_coding(dump_dir: str) -> str:
    """Split the mission into task files, in the front workspace.

    Called directly rather than through the gateway `/window`: that route is
    the front's single entrance, and giving it a role parameter would end it.
    """
    record = next_record_path(CODING_RECORDS)
    output, _, exit_code = run_role(
        "coding",
        coding_prompt(f"{dump_dir}/mission.md"),
        cwd=FRONT_WORKSPACE,
        timeout=CODING_TIMEOUT_SECONDS,
        record=record,
    )
    if exit_code != 0:
        raise ListenerError(f"coding run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def register_mission(dump_dir: str) -> str:
    """Run `new_mission.py` over the dump directory and return what it printed."""
    try:
        completed = subprocess.run(
            ["uv", "run", "new_mission.py", dump_dir],
            cwd=FRONT_WORKSPACE,
            capture_output=True,
            text=True,
            timeout=REGISTER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ListenerError(f"new_mission.py could not be run: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[:500]
        raise ListenerError(f"new_mission.py exited {completed.returncode}: {detail}")
    return completed.stdout.strip()


def handle_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Run the six mission-topic steps in order and always answer the topic.

    `agag.zulip.serve()` swallows handler exceptions, so a step that raises
    without this would leave the topic silent and the evidence in a log nobody
    opens. Every exit path writes back how far the message got.
    """
    channel = channel_name(message)
    topic = str(message.get("subject", ""))
    log(f"mission topic message #{message.get('id')} in {channel!r}/{topic!r}")

    sections: list[str] = []
    step = "reading the topic"
    try:
        project = project_from_channel(channel)
        history = client.topic_history(channel, topic, num_before=HISTORY_MESSAGES)
        dump_notice = topic_dump(
            channel,
            topic,
            format_chatlog(history, self_id),
            cwd=FRONT_WORKSPACE,
        )
        dump_dir = dump_directory(dump_notice)

        step = "project setup"
        init_project(project)

        step = "front"
        # The front's answer is not inspected: `mission.md` on disk is the
        # whole decision. It is still relayed verbatim.
        sections.append(call_window(window_prompt(absolute_dump_notice(dump_notice))))

        step = "task split"
        if (FRONT_WORKSPACE / dump_dir / "mission.md").is_file():
            sections.append(run_coding(dump_dir))
        else:
            sections.append("no mission.md was written, so no task split was run.")

        step = "mission registration"
        sections.append(register_mission(dump_dir))
    except Exception as error:  # noqa: BLE001 - the topic is the error channel
        log(f"mission topic workflow failed during {step}: {error!r}")
        sections.append(f"failed during {step}: {error}")

    topic_write(topic, "\n\n".join(section for section in sections if section),
                channel=channel, client=client)


def observe_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Passive handler (`AUTOLAB_ZULIP_LOG_ONLY=1`): log, never mutate."""
    log(
        f"observed #{message.get('id')} in {channel_name(message)!r}/"
        f"{message.get('subject')!r}: {str(message.get('content', ''))[:200]!r}"
    )


def subscribe_project_channels(client: ZulipClient) -> list[str]:
    """Put every active realm user in every `pj-*` channel.

    A project channel is a shared room, not the autolab bot's private inbox:
    all agents participate and each one filters for the topics it owns. This
    also covers a channel a human created by hand, which no agent would
    otherwise be in — and Zulip delivers no events for an unsubscribed channel.
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
