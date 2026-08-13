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


DUMP_DIR = ".local/topics/pj-demo-project/mission-one/1"
DUMP_NOTICE = f"{DUMP_DIR}/chatlog.txt is the log."


class Client:
    def __init__(self, calls):
        self.calls = calls

    def topic_history(self, channel, topic, num_before):
        self.calls.append(("history", channel, topic, num_before))
        return [message()]


def wire(monkeypatch, tmp_path, calls, *, coding="split into 2 tasks", register="created PD-4"):
    monkeypatch.setattr(zulip_listener, "FRONT_WORKSPACE", tmp_path)
    monkeypatch.setattr(
        zulip_listener,
        "topic_dump",
        lambda channel, topic, chatlog, cwd: (
            calls.append(("dump", channel, topic, chatlog, cwd)) or DUMP_NOTICE
        ),
    )
    monkeypatch.setattr(
        zulip_listener, "init_project", lambda project: calls.append(("init", project)) or "success"
    )
    monkeypatch.setattr(
        zulip_listener,
        "call_window",
        lambda prompt: calls.append(("window", prompt)) or "mission.md written",
    )
    monkeypatch.setattr(
        zulip_listener,
        "run_coding",
        lambda dump_dir: calls.append(("coding", dump_dir)) or coding,
    )
    monkeypatch.setattr(
        zulip_listener,
        "register_mission",
        lambda dump_dir: calls.append(("register", dump_dir)) or register,
    )
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )


def test_handle_message_runs_the_six_steps_in_order(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)
    (tmp_path / DUMP_DIR).mkdir(parents=True)
    (tmp_path / DUMP_DIR / "mission.md").write_text("# Build it\n")

    zulip_listener.handle_message(client, message(), BOT_ID)

    assert [call[0] for call in calls] == [
        "history", "dump", "init", "window", "coding", "register", "write",
    ]
    assert calls[1][3] == "[Developer] Build it\n"
    # The window prompt is the dump sentence plus the guide file, nothing else.
    guide = zulip_listener.guide("front", "guide_mission_topic.md")
    assert calls[3][1].endswith(f"\n\n{guide}")
    # The window sees the chat log as an absolute path; the dump itself stays
    # front-relative.
    assert calls[3][1].startswith(str(tmp_path / DUMP_DIR / "chatlog.txt"))
    assert calls[4][1] == DUMP_DIR
    assert calls[5][1] == DUMP_DIR
    assert calls[6][1:3] == ("mission-one", "mission.md written\n\nsplit into 2 tasks\n\ncreated PD-4")
    assert calls[6][3] == {"channel": "pj-demo-project", "client": client}


def test_handle_message_skips_the_split_when_the_front_wrote_no_mission(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls, register="no mission")
    (tmp_path / DUMP_DIR).mkdir(parents=True)

    zulip_listener.handle_message(Client(calls), message(), BOT_ID)

    assert "coding" not in [call[0] for call in calls]
    assert "no mission.md was written" in calls[-1][2]
    assert calls[-1][2].endswith("no mission")


def test_handle_message_always_answers_the_topic_with_how_far_it_got(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    (tmp_path / DUMP_DIR).mkdir(parents=True)
    (tmp_path / DUMP_DIR / "mission.md").write_text("# Build it\n")

    def explode(dump_dir):
        raise zulip_listener.ListenerError("claude_code timed out")

    monkeypatch.setattr(zulip_listener, "run_coding", explode)

    zulip_listener.handle_message(Client(calls), message(), BOT_ID)

    reply = calls[-1][2]
    assert calls[-1][0] == "write"
    assert reply.startswith("mission.md written")
    assert "failed during task split: claude_code timed out" in reply
    assert "register" not in [call[0] for call in calls]


def test_handle_message_reports_a_channel_that_is_not_a_project(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    zulip_listener.handle_message(Client(calls), message(channel="another-channel"), BOT_ID)
    assert calls[-1][0] == "write"
    assert "failed during reading the topic" in calls[-1][2]


def test_coding_prompt_is_the_mission_path_plus_the_guide():
    prompt = zulip_listener.coding_prompt(f"{DUMP_DIR}/mission.md")
    assert prompt.startswith(f"{DUMP_DIR}/mission.md is the file that describes the mission.")
    assert prompt.endswith(zulip_listener.guide("coding", "guide_task_split.md"))


def test_guide_refuses_to_start_without_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.guide("front", "guide_mission_topic.md")


def test_next_record_path_numbers_like_the_gateway(tmp_path):
    assert zulip_listener.next_record_path(tmp_path).name == "run-0001.json"
    (tmp_path / "run-0001.json").write_text("{}")
    assert zulip_listener.next_record_path(tmp_path).name == "run-0002.json"


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
