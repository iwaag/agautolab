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
    assert calls[4][1:3] == ("mission-one", "mission added")
    assert calls[4][3] == {"channel": "pj-demo-project", "client": client}


def test_subscribe_project_channels_only_adds_missing_pj_channels():
    calls = []

    class Client:
        def channels(self):
            return [{"name": "general"}, {"name": "pj-one"}, {"name": "pj-two"}]

        def subscriptions(self):
            return [{"name": "pj-one"}, {"name": "general"}]

        def subscribe_channels(self, names):
            calls.append(names)

    assert zulip_listener.subscribe_project_channels(Client()) == ["pj-two"]
    assert calls == [["pj-two"]]


@pytest.mark.parametrize("channel", ["general", "pj-x", "pj-Bad_Name"])
def test_project_from_channel_rejects_non_project_channels(channel):
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.project_from_channel(channel)
