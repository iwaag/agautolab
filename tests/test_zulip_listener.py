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

    def resolve_topic(self, message_id, topic):
        self.calls.append(("resolve", message_id, topic))


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


def wire_response(monkeypatch, calls, *, coding="split done", cancelled=0):
    monkeypatch.setattr(
        zulip_listener,
        "upsert_work",
        lambda project, channel, topic, title, description: (
            calls.append(("upsert", project, channel, topic, title, description))
            or f'updated PD-4 "{title}"'
        ),
    )
    monkeypatch.setattr(
        zulip_listener,
        "cancel_sub_works",
        lambda project, channel, topic: calls.append(("cancel-subs",)) or cancelled,
    )
    monkeypatch.setattr(
        zulip_listener,
        "run_coding",
        lambda coding_dir: calls.append(("coding", coding_dir)) or coding,
    )
    monkeypatch.setattr(
        zulip_listener,
        "register_task_files",
        lambda project, channel, topic, coding_dir, rev: (
            calls.append(("register", coding_dir, rev)) or [f"created sub-work rev {rev}"]
        ),
    )
    monkeypatch.setattr(
        zulip_listener,
        "transition_work",
        lambda project, channel, topic, group: calls.append(("transition", group)) or "PD-4",
    )


def front_dir(tmp_path):
    return tmp_path / "topics" / CHANNEL / TOPIC / "front"


def test_handle_topic_acks_then_runs_the_steps_in_order(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    # The trailing history read is the post-run re-check for human messages
    # that arrived during the run (none here, so the handler leaves).
    assert [call[0] for call in calls] == [
        "whoami", "write", "history", "init", "plane", "front", "write", "history",
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


def test_new_mission_updates_plane_and_reruns_the_split(monkeypatch, tmp_path):
    calls = []
    wire_response(monkeypatch, calls, cancelled=2)
    front = tmp_path / "front"
    front.mkdir(parents=True)
    (front / "new_mission.md").write_text("# Build it\n\nThe body.")
    coding = tmp_path / "coding"
    coding.mkdir()
    (coding / "task1.md").write_text("# stale split from an earlier generation")

    sections, resolve_after = zulip_listener.handle_front_response(
        CHANNEL, TOPIC, "demo-project", front
    )

    assert [call[0] for call in calls] == ["upsert", "cancel-subs", "coding", "register"]
    assert calls[0][4:6] == ("Build it", "The body.")
    assert calls[2][1] == coding
    # The mission text travels into coding/, the stale split does not survive.
    assert (coding / "new_mission.md").read_text() == "# Build it\n\nThe body."
    assert not (coding / "task1.md").exists()
    # First generation; the counter persists beside front/ and coding/.
    assert calls[3][2] == 1
    assert (tmp_path / "generation").read_text() == "1\n"
    # The command file is consumed so a later run does not replay it.
    assert not (front / "new_mission.md").exists()
    assert sections == [
        'updated PD-4 "Build it"',
        "cancelled 2 existing sub-work(s)",
        "split done",
        "created sub-work rev 1",
    ]
    assert resolve_after is False


def test_generation_counter_increments_per_accepted_mission(tmp_path):
    assert zulip_listener.generation(tmp_path) == 1
    assert zulip_listener.generation(tmp_path) == 2
    assert zulip_listener.generation(tmp_path) == 3


def test_start_flag_moves_the_work_to_in_progress(monkeypatch, tmp_path):
    calls = []
    wire_response(monkeypatch, calls)
    front = tmp_path / "front"
    front.mkdir(parents=True)
    (front / "start.flag").touch()

    sections, resolve_after = zulip_listener.handle_front_response(
        CHANNEL, TOPIC, "demo-project", front
    )

    assert calls == [("transition", "started")]
    assert sections == ["mission PD-4 is now In Progress"]
    assert not (front / "start.flag").exists()
    assert resolve_after is False


def test_cancel_flag_cancels_everything_and_requests_resolution(monkeypatch, tmp_path):
    calls = []
    wire_response(monkeypatch, calls, cancelled=3)
    front = tmp_path / "front"
    front.mkdir(parents=True)
    (front / "cancel.flag").touch()

    sections, resolve_after = zulip_listener.handle_front_response(
        CHANNEL, TOPIC, "demo-project", front
    )

    assert calls == [("cancel-subs",), ("transition", "cancelled")]
    assert sections == ["mission PD-4 is cancelled along with 3 sub-work(s); resolving this topic"]
    assert not (front / "cancel.flag").exists()
    assert resolve_after is True


def test_handle_topic_resolves_the_topic_after_the_final_reply(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)
    wire_response(monkeypatch, calls)
    monkeypatch.setattr(
        zulip_listener,
        "run_front",
        lambda prompt, cwd: (front_dir(tmp_path) / "cancel.flag").touch() or "cancelling",
    )

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    # The ✔ rename comes after the final reply so the whole thread moves.
    assert [call[0] for call in calls][-3:] == ["write", "history", "resolve"]
    assert calls[-1] == ("resolve", 1, TOPIC)


def test_handle_topic_reports_a_response_handling_failure(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    wire_response(monkeypatch, calls)

    def explode(project, channel, topic, group):
        raise zulip_listener.ListenerError("plane is down")

    monkeypatch.setattr(zulip_listener, "transition_work", explode)
    monkeypatch.setattr(
        zulip_listener,
        "run_front",
        lambda prompt, cwd: (front_dir(tmp_path) / "start.flag").touch() or "starting",
    )

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    assert "failed during response handling: plane is down" in calls[-1][2]
    assert not any(call[0] == "resolve" for call in calls)


def test_handle_topic_reprocesses_when_a_human_posted_during_the_run(monkeypatch, tmp_path):
    """The final reply makes the bot the last poster and hides the topic from
    the sweep, so a mid-run human post must be caught by the handler itself."""
    calls = []
    wire(monkeypatch, tmp_path, calls)
    first = history_message()
    mid_run = {**history_message(content="one more thing"), "id": 2}

    class ScriptedClient(Client):
        def __init__(self):
            super().__init__(calls)
            # chatlog read, re-check (fresh human post), chatlog read, re-check
            self.scripts = [[first], [first, mid_run], [first, mid_run], [first, mid_run]]

        def topic_history(self, channel, topic, num_before):
            calls.append(("history", channel, topic, num_before))
            return self.scripts.pop(0)

    zulip_listener.handle_topic(ScriptedClient(), CHANNEL, TOPIC)

    assert [call[0] for call in calls].count("front") == 2
    acks = [call for call in calls if call[0] == "write" and call[2] == zulip_listener.ACK_TEXT]
    assert len(acks) == 2
    # The second round's chatlog carries the mid-run post.
    assert (front_dir(tmp_path) / "chatlog.md").read_text().endswith("one more thing\n")


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


RUN_TOPIC = "run-1"


def wire_run(monkeypatch, tmp_path, calls, *, chosen, report=None, success=False,
             output="work done"):
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(zulip_listener, "next_work", lambda: chosen)
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text)) or "success",
    )

    def work_run(workspace):
        calls.append(("run", workspace))
        work = workspace / ".local" / "work"
        if report is not None:
            (work / "report.md").write_text(report)
        if success:
            (work / "success.flag").touch()
        return output

    monkeypatch.setattr(zulip_listener, "run_work", work_run)
    monkeypatch.setattr(
        zulip_listener,
        "report_work",
        lambda project_id, issue_id, text, ok: (
            calls.append(("report", project_id, issue_id, text, ok)) or ("PD-7", bool(text), ok)
        ),
    )


CHOSEN = ("demo-project", "Add the README", "Write it.", "p1", "i1")


def test_handle_run_executes_comments_completes_and_cleans_up(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN, report="all good", success=True)

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    workspace = tmp_path / "projects" / "demo-project" / "main"
    assert [call[0] for call in calls] == ["write", "run", "report", "write"]
    assert calls[0][2] == zulip_listener.ACK_TEXT
    assert calls[1][1] == workspace
    assert calls[2][1:] == ("p1", "i1", "all good", True)
    outcome = calls[-1][2]
    assert 'running "Add the README" in demo-project' in outcome
    assert "work done" in outcome
    assert "work PD-7: commented yes, Done yes" in outcome
    # `.local/work/` is gone; the workspace itself stays.
    assert not (workspace / ".local" / "work").exists()
    assert workspace.is_dir()


def test_handle_run_writes_the_work_file_before_running(monkeypatch, tmp_path):
    calls = []
    seen = {}
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN)
    real_run = zulip_listener.run_work

    def capture(workspace):
        seen["work.md"] = (workspace / ".local" / "work" / "work.md").read_text()
        return real_run(workspace)

    monkeypatch.setattr(zulip_listener, "run_work", capture)
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)
    assert seen["work.md"] == "# Add the README\n\nWrite it.\n"


def test_handle_run_without_eligible_work_posts_no_work(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=None)
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)
    assert [call[2] for call in calls] == [zulip_listener.ACK_TEXT, "no work"]


def test_handle_run_refuses_a_dirty_workspace_and_keeps_it(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN)
    leftover = tmp_path / "projects" / "demo-project" / "main" / ".local" / "work"
    leftover.mkdir(parents=True)
    (leftover / "work.md").write_text("from a crashed run")

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    assert not any(call[0] == "run" for call in calls)
    assert "work dirty" in calls[-1][2]
    assert (leftover / "work.md").exists()  # manual cleanup is the recovery


def test_handle_run_without_a_report_says_so_and_leaves_the_work_open(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN)
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)
    assert calls[2][1:] == ("p1", "i1", None, False)
    assert "no report" in calls[-1][2]
    assert "work PD-7: commented no, Done no" in calls[-1][2]


def test_handle_run_reports_a_failure_and_still_cleans_up(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN)

    def explode(workspace):
        calls.append(("run", workspace))
        raise zulip_listener.ListenerError("claude_code timed out")

    monkeypatch.setattr(zulip_listener, "run_work", explode)
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    assert "failed during work run: claude_code timed out" in calls[-1][2]
    assert not (tmp_path / "projects" / "demo-project" / "main" / ".local" / "work").exists()


def test_dispatch_routes_run_topics_anywhere_and_mission_topics_only_in_projects(monkeypatch):
    routed = []
    monkeypatch.setattr(
        zulip_listener, "handle_run",
        lambda client, channel, topic: routed.append(("run", channel, topic)),
    )
    monkeypatch.setattr(
        zulip_listener, "handle_topic",
        lambda client, channel, topic: routed.append(("mission", channel, topic)),
    )

    zulip_listener.dispatch(None, "general", "run-1")
    zulip_listener.dispatch(None, CHANNEL, "run-2")
    zulip_listener.dispatch(None, CHANNEL, TOPIC)
    zulip_listener.dispatch(None, "general", "mission-stray")  # silently ignored

    assert routed == [
        ("run", "general", "run-1"),
        ("run", CHANNEL, "run-2"),
        ("mission", CHANNEL, TOPIC),
    ]


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
            return {1: [7], 2: [7, 8], 3: [7]}[stream_id]

        def subscribe_channels(self, names, principals=None):
            calls.append((names, principals))

    # `general` is reconciled like a project channel (that is what makes
    # `run-` topics visible to the sweep), `pj-one` is already complete, and
    # the deactivated user is never subscribed anywhere.
    assert zulip_listener.subscribe_project_channels(Client()) == ["general", "pj-two"]
    assert calls == [(["general"], [8]), (["pj-two"], [8])]


@pytest.mark.parametrize("channel", ["general", "pj-x", "pj-Bad_Name"])
def test_project_from_channel_rejects_non_project_channels(channel):
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.project_from_channel(channel)
