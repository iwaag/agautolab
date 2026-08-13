from pathlib import Path

import pytest

from agautolab import zulip_listener


BOT_ID = 11
HUMAN_ID = 8


def message(channel="pj-demo-project", topic="mission-one", content="Build it"):
    return {
        "id": 1,
        "type": "stream",
        "sender_id": HUMAN_ID,
        "sender_full_name": "Developer",
        "display_recipient": channel,
        "subject": topic,
        "content": content,
    }


def test_accept_keeps_topic_prefix_rule_channel_independent():
    assert zulip_listener.accept(message(), BOT_ID)
    assert zulip_listener.accept(message(channel="another-channel"), BOT_ID)
    assert not zulip_listener.accept(message(topic="discussion"), BOT_ID)
    assert not zulip_listener.accept({**message(), "sender_id": BOT_ID}, BOT_ID)


def test_handle_message_runs_four_workflows_in_order(monkeypatch, tmp_path):
    calls = []

    class Client:
        def topic_history(self, channel, topic, num_before):
            calls.append(("history", channel, topic, num_before))
            return [message()]

    client = Client()
    monkeypatch.setattr(zulip_listener, "FRONT_WORKSPACE", tmp_path)
    monkeypatch.setattr(
        zulip_listener,
        "topic_dump",
        lambda channel, topic, chatlog, cwd: (
            calls.append(("dump", channel, topic, chatlog, cwd))
            or ".local/topics/pj-demo-project/mission-one/1/chatlog.txt is the log."
        ),
    )
    monkeypatch.setattr(
        zulip_listener, "init_project", lambda project: calls.append(("init", project)) or "success"
    )
    monkeypatch.setattr(
        zulip_listener,
        "call_window",
        lambda prompt: calls.append(("window", prompt)) or "mission added",
    )
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )

    zulip_listener.handle_message(client, message(), BOT_ID)

    assert [call[0] for call in calls] == ["history", "dump", "init", "window", "write"]
    assert calls[1][3] == "[Developer] Build it\n"
    assert "uv run new_mission.py --help" in calls[3][1]
    assert "already running in the front workspace" in calls[3][1]
    assert "Do not ask for path clarification" in calls[3][1]
    # The window sees the chat log as an absolute path; the dump itself stays
    # front-relative.
    assert str(tmp_path / ".local/topics/pj-demo-project/mission-one/1/chatlog.txt") in calls[3][1]
    assert not calls[3][1].startswith(".local/")
    assert calls[4][1:3] == ("mission-one", "mission added")
    assert calls[4][3] == {"channel": "pj-demo-project", "client": client}


def test_subscribe_project_channels_puts_every_active_user_in_pj_channels():
    calls = []

    class Client:
        def users(self):
            return [
                {"user_id": 7, "is_active": True},
                {"user_id": 8, "is_active": True},
                {"user_id": 9, "is_active": False},
            ]

        def channels(self):
            return [
                {"name": "general", "stream_id": 1},
                {"name": "pj-one", "stream_id": 2},
                {"name": "pj-two", "stream_id": 3},
            ]

        def channel_subscribers(self, stream_id):
            return {2: [7, 8], 3: [7]}[stream_id]

        def subscribe_channels(self, names, principals=None):
            calls.append((names, principals))

    # `general` is left alone, `pj-one` is already complete, and the
    # deactivated user is never subscribed anywhere.
    assert zulip_listener.subscribe_project_channels(Client()) == ["pj-two"]
    assert calls == [(["pj-two"], [8])]


@pytest.mark.parametrize("channel", ["general", "pj-x", "pj-Bad_Name"])
def test_project_from_channel_rejects_non_project_channels(channel):
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.project_from_channel(channel)
