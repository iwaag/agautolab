from pathlib import Path

import pytest

from agautolab import zulip_listener


BOT_ID = 11
HUMAN_ID = 8
CHANNEL = "pj-demo-project"
TOPIC = "mission-one"


def history_message(sender_id=HUMAN_ID, name="Developer", content="Build it"):
    return {
        "id": 1,
        "type": "stream",
        "sender_id": sender_id,
        "sender_full_name": name,
        "display_recipient": CHANNEL,
        "subject": TOPIC,
        "content": content,
    }


class Client:
    email = "autolab-bot@example.invalid"

    def __init__(self, calls, history=None):
        self.calls = calls
        self.history = history if history is not None else [history_message()]

    def whoami(self):
        self.calls.append(("whoami",))
        return {"user_id": BOT_ID, "full_name": "Autolab"}

    def topic_history(self, channel, topic, num_before):
        self.calls.append(("history", channel, topic, num_before))
        return self.history


def wire(monkeypatch, tmp_path, calls, *, plane_files=False, front="front says hi"):
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    monkeypatch.setattr(zulip_listener, "RECORDS_ROOT", tmp_path / "records")
    monkeypatch.setattr(
        zulip_listener, "init_project", lambda project: calls.append(("init", project)) or "success"
    )
    monkeypatch.setattr(
        zulip_listener,
        "write_mission_workspace",
        lambda directory, project, channel, topic: (
            calls.append(("plane", directory, project, channel, topic)) or plane_files
        ),
    )
    monkeypatch.setattr(
        zulip_listener,
        "run_front",
        lambda prompt, cwd: calls.append(("front", prompt, cwd)) or front,
    )
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )


def front_dir(tmp_path):
    return tmp_path / "topics" / CHANNEL / TOPIC / "front"


def test_handle_topic_acks_then_runs_the_steps_in_order(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    assert [call[0] for call in calls] == [
        "whoami", "write", "history", "init", "plane", "front", "write",
    ]
    # The ack is the first post, before any work: it makes the bot the last
    # poster so a later sweep skips the topic while this run is in flight.
    assert calls[1][1:3] == (TOPIC, zulip_listener.ACK_TEXT)
    assert calls[1][3] == {"channel": CHANNEL, "client": client}
    # The chatlog lands in the stable topic workspace.
    assert (front_dir(tmp_path) / "chatlog.md").read_text() == "[Developer] Build it\n"
    assert calls[4][1] == front_dir(tmp_path)
    # The front runs in the topic workspace with the chatlog-only prompt.
    assert calls[5][2] == front_dir(tmp_path)
    assert "mission and tasks" not in calls[5][1]
    assert calls[6][1:3] == (TOPIC, "front says hi")


def test_handle_topic_mentions_plane_files_when_they_were_written(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls, plane_files=True)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    prompt = next(call[1] for call in calls if call[0] == "front")
    assert "The current mission and tasks are also placed in the working directory." in prompt


def test_handle_topic_marks_the_bots_own_lines_in_the_chatlog(monkeypatch, tmp_path):
    calls = []
    client = Client(
        calls,
        history=[
            history_message(),
            history_message(sender_id=BOT_ID, name="Autolab", content="ack"),
        ],
    )
    wire(monkeypatch, tmp_path, calls)

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    assert (front_dir(tmp_path) / "chatlog.md").read_text() == (
        "[Developer] Build it\n[Autolab (you)] ack\n"
    )


def test_handle_topic_always_answers_with_how_far_it_got(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)

    def explode(prompt, cwd):
        raise zulip_listener.ListenerError("claude_code timed out")

    monkeypatch.setattr(zulip_listener, "run_front", explode)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    assert calls[-1][0] == "write"
    assert "failed during front: claude_code timed out" in calls[-1][2]


def test_handle_topic_reports_a_channel_that_is_not_a_project(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    zulip_listener.handle_topic(Client(calls), "another-channel", TOPIC)
    # The ack still goes out; the failure is reported in the reply.
    assert [call[0] for call in calls if call[0] == "write"][0:2] == ["write", "write"]
    assert "failed during reading the topic" in calls[-1][2]


def test_front_prompt_is_the_placement_lines_plus_the_guide(monkeypatch, tmp_path):
    guide_dir = tmp_path / "mission_front"
    guide_dir.mkdir(parents=True)
    (guide_dir / "guide_mission_topic.md").write_text("GUIDE TEXT\n")
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)

    prompt = zulip_listener.front_prompt("Autolab", plane_files=False)
    assert prompt == (
        "The chatlog is placed in the working directory. "
        "You are 'Autolab' in the chatlog.\n\nGUIDE TEXT"
    )
    with_plane = zulip_listener.front_prompt("Autolab", plane_files=True)
    assert with_plane == (
        "The chatlog is placed in the working directory. "
        "You are 'Autolab' in the chatlog.\n"
        "The current mission and tasks are also placed in the working directory."
        "\n\nGUIDE TEXT"
    )


def test_topic_workspace_is_stable_and_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path)
    first = zulip_listener.topic_workspace(CHANNEL, TOPIC)
    assert first == tmp_path / CHANNEL / TOPIC
    assert zulip_listener.topic_workspace(CHANNEL, TOPIC) == first  # reused, no <N>
    for bad in ("../outside", "a/b", ""):
        with pytest.raises(ValueError):
            zulip_listener.topic_workspace(bad, TOPIC)
        with pytest.raises(ValueError):
            zulip_listener.topic_workspace(CHANNEL, bad)


def test_guide_refuses_to_start_without_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.guide("mission_front", "guide_mission_topic.md")


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
