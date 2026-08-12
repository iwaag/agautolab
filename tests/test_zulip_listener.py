"""The mission-topic bridge: accept rule, briefing wrapper, and reporting."""

import pytest

from agautolab import zulip_listener

BOT_ID = 11
SENDER_ID = 9


def mission_message(topic="mission-20260812-235959-abc123", sender=SENDER_ID):
    return {
        "id": 5,
        "type": "stream",
        "sender_id": sender,
        "content": "Build the game described here.",
        "display_recipient": "pj-whack-a-mole",
        "subject": topic,
    }


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_to_channel(self, channel, topic, content):
        self.sent.append((channel, topic, content))
        return 1


def test_accept_fires_only_on_live_mission_topics():
    assert zulip_listener.accept(mission_message(), BOT_ID)
    assert not zulip_listener.accept(mission_message(sender=BOT_ID), BOT_ID)
    assert not zulip_listener.accept(mission_message(topic="create-x"), BOT_ID)
    assert not zulip_listener.accept(mission_message(topic="✔ mission-x"), BOT_ID)
    dm = mission_message()
    dm["type"] = "private"
    assert not zulip_listener.accept(dm, BOT_ID)


def test_briefing_carries_budget_and_content():
    text = zulip_listener.bridge_briefing("mission body", 20)
    assert "max_sessions=20" in text
    assert text.endswith("mission body")


def test_started_mission_reports_start_and_terminal_outcome(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(zulip_listener, "post_window", lambda text: {
        "outcome": "done", "mission": {"status": 202, "accepted": True, "run": 7}})
    monkeypatch.setattr(zulip_listener, "wait_for_terminal_status", lambda: {
        "driver": {"running": False, "exit_code": 0}, "done": "shipped it"})
    zulip_listener.handle_message(client, mission_message(), BOT_ID)
    assert len(client.sent) == 2
    assert "run 7" in client.sent[0][2]
    assert "shipped it" in client.sent[1][2]
    assert client.sent[0][:2] == ("pj-whack-a-mole", "mission-20260812-235959-abc123")


def test_busy_node_is_reported_in_topic(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(zulip_listener, "post_window", lambda text: {
        "outcome": "done", "mission": {"status": 409, "error": "a mission is already running"}})
    zulip_listener.handle_message(client, mission_message(), BOT_ID)
    assert len(client.sent) == 1
    assert "already running" in client.sent[0][2]


def test_window_answer_without_mission_is_relayed(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(zulip_listener, "post_window", lambda text: {
        "outcome": "done", "reply": "here is what I know"})
    zulip_listener.handle_message(client, mission_message(), BOT_ID)
    assert client.sent == [
        ("pj-whack-a-mole", "mission-20260812-235959-abc123", "here is what I know")
    ]


def test_window_failure_is_reported(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(zulip_listener, "post_window", lambda text: {
        "outcome": "failed", "failure": "opencode timed out after 300s"})
    zulip_listener.handle_message(client, mission_message(), BOT_ID)
    assert "failed to answer" in client.sent[0][2]
    assert "timed out" in client.sent[0][2]


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        ({"driver": {"exit_code": 0}, "done": "note"}, "note"),
        ({"driver": {"exit_code": 10}, "done": None}, "budget ran out"),
        ({"driver": {"exit_code": 11}, "done": None}, "never consumed"),
        ({"driver": {"exit_code": 1}, "done": None}, "without a done note"),
    ],
)
def test_terminal_messages(status, needle):
    assert needle in zulip_listener.terminal_message(status)
