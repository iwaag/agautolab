"""A Zulip realm small enough to hold in a test, honest about the parts that
matter to the work record.

The record lives in the conversations now, so a test of it is a test against
Zulip's own behaviour — and three of those behaviours are the whole reason
the record is shaped the way it is:

- **resolving a topic renames it** to `✔ <name>`, so a caller holding the old
  name reads an empty topic unless it follows the rename;
- **a message id survives every rename**, including a move to another
  channel, which is what makes an id usable as an anchor;
- **a deleted message is gone**, and a topic of the same name is not it.

`Realm` does all three. What it deliberately does *not* model is markdown
rendering, permissions or ordering subtleties — nothing under test depends on
them.
"""

from __future__ import annotations

from agag.zulip import RESOLVED_TOPIC_PREFIX

BOT_ID = 11
HUMAN_ID = 8


class Realm:
    """Channels, topics and messages, with ids that outlive names."""

    email = "autolab-bot@example.invalid"

    def __init__(self):
        self.messages: dict[int, dict] = {}
        self.order: list[int] = []
        self.streams: dict[str, int] = {}
        self.channel_rows: dict[str, dict] = {}
        self.next_id = 5000
        self.calls = 0
        self.archived: list[str] = []

    # --- building it up ----------------------------------------------------

    def add_channel(self, name: str, folder_id=None, subscribers=(HUMAN_ID, BOT_ID)) -> int:
        if name not in self.streams:
            self.streams[name] = len(self.streams) + 100
        self.channel_rows[name] = {
            "name": name,
            "stream_id": self.streams[name],
            "folder_id": folder_id,
            "subscribers": list(subscribers),
        }
        return self.streams[name]

    def post(self, channel: str, topic: str, content: str, sender_id: int = BOT_ID) -> int:
        self.add_channel(channel) if channel not in self.streams else None
        self.next_id += 1
        message = {
            "id": self.next_id,
            "sender_id": sender_id,
            "sender_full_name": "autolab" if sender_id == BOT_ID else "Developer",
            "display_recipient": channel,
            "subject": topic,
            "content": content,
        }
        self.messages[message["id"]] = message
        self.order.append(message["id"])
        return message["id"]

    def delete(self, message_id: int) -> None:
        self.messages.pop(int(message_id), None)

    def rename_topic(self, channel: str, topic: str, new_name: str) -> None:
        for message in self.messages.values():
            if message["display_recipient"] == channel and message["subject"] == topic:
                message["subject"] = new_name

    def topic_of(self, message_id: int) -> tuple[str, str]:
        message = self.messages[int(message_id)]
        return message["display_recipient"], message["subject"]

    def contents(self, channel: str, topic: str) -> list[str]:
        return [
            m["content"] for i in self.order if (m := self.messages.get(i))
            and m["display_recipient"] == channel and m["subject"] == topic
        ]

    # --- the client surface the code uses ----------------------------------

    def whoami(self, refresh: bool = False) -> dict:
        return {"user_id": BOT_ID, "full_name": "autolab-agstudio1"}

    def message(self, message_id: int) -> dict | None:
        self.calls += 1
        return self.messages.get(int(message_id))

    def send_to_channel(self, channel: str, topic: str, content: str) -> int:
        self.calls += 1
        return self.post(channel, topic, content)

    def topic_history(self, channel: str, topic: str, num_before: int = 50) -> list[dict]:
        self.calls += 1
        rows = [
            m for i in self.order if (m := self.messages.get(i))
            and m["display_recipient"] == channel and m["subject"] == topic
        ]
        return rows[-num_before:]

    def stream_id(self, name: str) -> int:
        self.calls += 1
        if name not in self.streams:
            from agag.zulip import ZulipError

            raise ZulipError(f"no such channel {name}")
        return self.streams[name]

    def channel_topics(self, stream_id: int) -> list[str]:
        self.calls += 1
        name = next(n for n, sid in self.streams.items() if sid == stream_id)
        seen: list[str] = []
        for i in reversed(self.order):
            message = self.messages.get(i)
            if message and message["display_recipient"] == name:
                if message["subject"] not in seen:
                    seen.append(message["subject"])
        return seen

    def channels(self) -> list[dict]:
        self.calls += 1
        return [dict(row) for row in self.channel_rows.values()]

    def channel_subscribers(self, stream_id: int) -> list[int]:
        self.calls += 1
        name = next(n for n, sid in self.streams.items() if sid == stream_id)
        return list(self.channel_rows[name]["subscribers"])

    def create_channel(self, name, description, principals, folder_id=None) -> dict:
        self.calls += 1
        self.add_channel(name, folder_id=folder_id, subscribers=principals or [BOT_ID])
        self.channel_rows[name]["description"] = description
        return {"result": "success"}

    def archive_channel(self, stream_id: int) -> dict:
        self.calls += 1
        name = next(n for n, sid in self.streams.items() if sid == stream_id)
        self.archived.append(name)
        return {"result": "success"}

    def resolve_topic(self, message_id: int, topic: str) -> None:
        self.calls += 1
        if topic.startswith(RESOLVED_TOPIC_PREFIX):
            return
        channel = self.messages[int(message_id)]["display_recipient"]
        self.rename_topic(channel, topic, f"{RESOLVED_TOPIC_PREFIX}{topic}")
