import re
from pathlib import Path

import pytest

from agag import participation, topics
from agag.topics import GuideError

from agautolab import zulip_listener


BOT_ID = 11
FORGE_INTRO = "# agforge\n\nOpen an `assetplan-…` topic in `agforge-agstudio1`."
HUMAN_ID = 8
CHANNEL = "pj-demo-project"
TOPIC = "workplan-one"


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


#: The shared `#agents` board every run's harvest reads. Serving it here — and
#: not recording the reads — keeps the harvest out of the call sequences the
#: flow tests assert on, while still exercising the real code path.
AGENTS_STREAM = 3


class Client:
    email = "autolab-bot@example.invalid"

    #: `{intro topic: body}`. Empty by default: an empty board is a fact the
    #: harvest states honestly, so most tests need say nothing about it.
    board: dict[str, str] = {}

    def __init__(self, calls, history=None, board=None):
        self.calls = calls
        self.history = history if history is not None else [history_message()]
        if board is not None:
            self.board = board

    def whoami(self):
        self.calls.append(("whoami",))
        return {"user_id": BOT_ID, "full_name": "Autolab"}

    def topic_history(self, channel, topic, num_before):
        if channel == "agents":
            return [history_message(sender_id=13, name="Forge", content=self.board[topic])]
        self.calls.append(("history", channel, topic, num_before))
        return self.history

    def resolve_topic(self, message_id, topic):
        self.calls.append(("resolve", message_id, topic))

    # --- the work- channel surface (run rework Step 2) ---------------------

    #: The realm as these tests see it: one project channel, in no folder
    #: unless a test says otherwise.
    channels_list = [{"name": CHANNEL, "stream_id": 7, "folder_id": None}]

    def channels(self):
        self.calls.append(("channels",))
        return [dict(row) for row in self.channels_list]

    def channel_subscribers(self, stream_id):
        self.calls.append(("subscribers", stream_id))
        return [HUMAN_ID, BOT_ID]

    def create_channel(self, name, description, principals, folder_id=None):
        self.calls.append(("create-channel", name, description, principals, folder_id))
        return {"subscribed": {}}

    def send_to_channel(self, channel, topic, content):
        self.calls.append(("send", channel, topic, content))
        return 42

    def archive_channel(self, stream_id):
        self.calls.append(("archive", stream_id))
        return {}

    def stream_id(self, name):
        if name == "agents":
            return AGENTS_STREAM
        return next(row["stream_id"] for row in self.channels_list if row["name"] == name)

    def channel_topics(self, stream_id):
        if stream_id == AGENTS_STREAM:
            return list(self.board)
        self.calls.append(("topics", stream_id))
        return self.topic_names

    topic_names: list[str] = []


def wire(monkeypatch, tmp_path, calls, *, plane_files=False, superdirector="planner says hi"):
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    monkeypatch.setattr(zulip_listener, "RECORDS_ROOT", tmp_path / "records")
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
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
        "run_superdirector",
        lambda prompt, cwd: calls.append(("superdirector", prompt, cwd)) or superdirector,
    )
    monkeypatch.setattr(
        topics,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )
    guides = tmp_path / "guides"
    (guides / "workplan_superdirector").mkdir(parents=True)
    (guides / "workplan_superdirector" / "guide.md").write_text("GUIDE TEXT")
    monkeypatch.setattr(zulip_listener, "GUIDES", guides)


PROJECT = "demo-project"
PLAN_TEXT = "# The plan\n\nStep one, step two."


TASK_CHANGES = [
    zulip_listener.TaskChange(1, "created", "First", "# First\n\ndo this\n", "PD-5"),
]


def wire_response(monkeypatch, tmp_path, calls, *, cancelled=0, changes=None):
    """Stub everything `handle_superdirector_response` reaches beyond the
    filesystem and Zulip — the run itself already happened; the handler only
    acts on the files it left in the serving workspace."""
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(
        zulip_listener,
        "upsert_work",
        lambda project, channel, topic, title, description: (
            calls.append(("upsert", project, channel, topic, title, description))
            or (f'updated PD-4 "{title}"', "PD-4")
        ),
    )
    monkeypatch.setattr(
        zulip_listener,
        "cancel_sub_works",
        lambda project, channel, topic: calls.append(("cancel-subs",)) or cancelled,
    )
    monkeypatch.setattr(
        zulip_listener,
        "reconcile_task_files",
        lambda project, channel, topic, plan_dir: (
            calls.append(("reconcile", plan_dir))
            or (["created sub-work PD-5 \"First\""],
                TASK_CHANGES if changes is None else changes)
        ),
    )
    monkeypatch.setattr(
        zulip_listener,
        "transition_work",
        lambda project, channel, topic, group: calls.append(("transition", group)) or "PD-4",
    )
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: (
            calls.append(("post", kwargs.get("channel"), topic, text)) or "success"
        ),
    )


def superdirector_dir(tmp_path, number=1):
    """Each serving works in a fresh generation `<N>/`, not one stable dir."""
    return tmp_path / "topics" / CHANNEL / TOPIC / str(number) / "superdirector"


def project_dir(tmp_path, project=PROJECT):
    """The persistent project folder — `main/`, `direction/` and `devlog/`,
    reached from the serving workspace through symlinks."""
    return tmp_path / "projects" / project


def test_handle_topic_acks_then_runs_the_steps_in_order(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    # The trailing history read is the post-run re-check for human messages
    # that arrived during the run (none here, so the handler leaves).
    assert [call[0] for call in calls] == [
        "whoami", "write", "history", "init", "plane", "superdirector",
        # the handoff lookup, the reply, then the post-run re-check
        "history", "write", "history",
    ]
    # The ack is the first post, before any work: it makes the bot the last
    # poster so a later sweep skips the topic while this run is in flight.
    assert calls[1][1:3] == (TOPIC, zulip_listener.ACK_TEXT)
    # The chatlog lands in this generation's superdirector workspace.
    workspace = superdirector_dir(tmp_path)
    assert (workspace / "chatlog.md").read_text() == "[Developer] Build it\n"
    # The Plane mirror goes to `current/`, and an empty mirror leaves nothing.
    assert calls[4][1] == workspace / "current"
    assert not (workspace / "current").exists()
    # The run happens in the project folder; the workspace travels by path.
    assert calls[5][2] == project_dir(tmp_path)
    assert str(workspace) in calls[5][1]
    assert "currently registered mission" not in calls[5][1]
    assert calls[7][1:3] == (TOPIC, HANDOFF + "planner says hi")


def test_each_serving_cuts_a_new_generation(monkeypatch, tmp_path):
    """Before this, one stable directory was reused forever, so a continued
    conversation ran on top of the previous run's leftovers."""
    calls = []
    wire(monkeypatch, tmp_path, calls)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)
    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    assert superdirector_dir(tmp_path, 1).is_dir()
    assert superdirector_dir(tmp_path, 2).is_dir()
    # Each run is pointed at its own generation workspace.
    prompts = [call[1] for call in calls if call[0] == "superdirector"]
    assert str(superdirector_dir(tmp_path, 1)) in prompts[0]
    assert str(superdirector_dir(tmp_path, 2)) in prompts[1]


def test_handle_topic_mentions_plane_files_when_they_were_written(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls, plane_files=True)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    prompt = next(call[1] for call in calls if call[0] == "superdirector")
    current = superdirector_dir(tmp_path) / "current"
    assert f'The currently registered mission and tasks are placed in "{current}".' in prompt
    assert prompt.endswith("GUIDE TEXT")
    assert current.is_dir()


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

    assert (superdirector_dir(tmp_path) / "chatlog.md").read_text() == (
        "[Developer] Build it\n[Autolab (you)] ack\n"
    )


def test_handle_topic_always_answers_with_how_far_it_got(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)

    def explode(prompt, cwd):
        raise zulip_listener.ListenerError("claude_code timed out")

    monkeypatch.setattr(zulip_listener, "run_superdirector", explode)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    assert calls[-1][0] == "write"
    assert "failed during superdirector: claude_code timed out" in calls[-1][2]


def test_handle_topic_reports_a_channel_that_is_not_a_project(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    zulip_listener.handle_topic(Client(calls), "another-channel", TOPIC)
    # The ack still goes out; the failure is reported in the reply.
    assert [call[0] for call in calls if call[0] == "write"][0:2] == ["write", "write"]
    assert "failed during chatlog" in calls[-1][2]


def test_an_empty_topic_costs_no_agent_run(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    zulip_listener.handle_topic(Client(calls, history=[]), CHANNEL, TOPIC)
    assert not any(call[0] == "superdirector" for call in calls)
    assert calls[-1][2] == zulip_listener.EMPTY_REPLY


def test_a_plan_reconciles_the_split_and_builds_the_run_surfaces(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire_response(monkeypatch, tmp_path, calls)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "plan.md").write_text(PLAN_TEXT)
    (workspace / "task1.md").write_text("# First\ndo this")

    sections, resolve_after = zulip_listener.handle_superdirector_response(
        client, CHANNEL, TOPIC, PROJECT, workspace
    )

    assert [call[0] for call in calls] == [
        "upsert", "reconcile", "channels", "subscribers", "create-channel", "post",
    ]
    # Title and description both from the plan — the whole file, heading
    # included, because Plane holds the plan verbatim.
    assert calls[0][4:6] == ("The plan", PLAN_TEXT)
    assert calls[1][1] == workspace
    # The channel is named after the Work label, carries the parent channel's
    # subscribers, and remembers the binding a workrun- serving needs back.
    assert calls[4][1:] == (
        "work-pd-4",
        "[AUTO] project: demo-project; mission: pj-demo-project/workplan-one",
        [HUMAN_ID, BOT_ID],
        None,
    )
    # The task content is posted by the bot, so the topic waits quietly for a
    # human instead of being swept the moment it exists.
    assert calls[5][1:] == ("work-pd-4", "workrun-task1-pd-4", "# First\n\ndo this\n")
    assert sections == [
        'updated PD-4 "The plan"',
        'created sub-work PD-5 "First"',
        "work channel work-pd-4 is ready",
        "opened work-pd-4/workrun-task1-pd-4",
    ]
    assert resolve_after is False


def test_the_work_channel_follows_the_project_channels_folder(monkeypatch, tmp_path):
    """Whatever folder the `pj-` channel sits in — including none. This is not
    the place to invent a folder structure."""
    calls = []
    client = Client(calls)
    client.channels_list = [{"name": CHANNEL, "stream_id": 7, "folder_id": 3}]
    wire_response(monkeypatch, tmp_path, calls)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "plan.md").write_text(PLAN_TEXT)

    zulip_listener.handle_superdirector_response(client, CHANNEL, TOPIC, PROJECT, workspace)

    assert next(call for call in calls if call[0] == "create-channel")[4] == 3


def test_a_replan_mirrors_each_change_onto_its_own_run_topic(monkeypatch, tmp_path):
    """One to one with the Plane reconcile: updated tasks are re-posted,
    cancelled ones are told and resolved, and an unchanged task is left
    silent so a re-plan of task 3 does not disturb tasks 1 and 2."""
    calls = []
    client = Client(calls)
    changes = [
        zulip_listener.TaskChange(1, "unchanged", "First", "# First\n\na\n", "PD-5"),
        zulip_listener.TaskChange(2, "updated", "Second", "# Second\n\nb\n", "PD-6"),
        zulip_listener.TaskChange(3, "created", "Third", "# Third\n\nc\n", "PD-7"),
        zulip_listener.TaskChange(4, "cancelled", "Fourth", "", "PD-8"),
    ]
    wire_response(monkeypatch, tmp_path, calls, changes=changes)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "plan.md").write_text(PLAN_TEXT)

    sections, _ = zulip_listener.handle_superdirector_response(
        client, CHANNEL, TOPIC, PROJECT, workspace
    )

    posts = [call for call in calls if call[0] in {"post", "send", "resolve"}]
    assert posts == [
        ("post", "work-pd-4", "workrun-task2-pd-4", "Updated by planner.\n\n# Second\n\nb\n"),
        ("post", "work-pd-4", "workrun-task3-pd-4", "# Third\n\nc\n"),
        ("send", "work-pd-4", "workrun-task4-pd-4", "Cancelled by planner."),
        ("resolve", 42, "workrun-task4-pd-4"),
    ]
    assert sections[-3:] == [
        "updated work-pd-4/workrun-task2-pd-4",
        "opened work-pd-4/workrun-task3-pd-4",
        "cancelled and resolved work-pd-4/workrun-task4-pd-4",
    ]


def test_a_task_changed_after_completion_only_gets_a_note(monkeypatch, tmp_path):
    """Whether to redo it is the mission conversation's call, not this
    handler's; the resolved topic is left as it is."""
    calls = []
    client = Client(calls)
    client.channels_list = [
        {"name": CHANNEL, "stream_id": 7, "folder_id": None},
        {"name": "work-pd-4", "stream_id": 8, "folder_id": None},
    ]
    client.topic_names = ["\u2714 workrun-task1-pd-4"]
    changes = [
        zulip_listener.TaskChange(1, "changed-after-done", "First", "# First\n\na\n", "PD-5"),
    ]
    wire_response(monkeypatch, tmp_path, calls, changes=changes)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "plan.md").write_text(PLAN_TEXT)

    sections, _ = zulip_listener.handle_superdirector_response(
        client, CHANNEL, TOPIC, PROJECT, workspace
    )

    # Posted under the resolved name: a resolved topic is a renamed topic, so
    # the bare name would open a second one beside it.
    sent = next(call for call in calls if call[0] == "send")
    assert sent[1:3] == ("work-pd-4", "\u2714 workrun-task1-pd-4")
    assert sent[3] == zulip_listener.CHANGED_AFTER_DONE
    assert not any(call[0] == "resolve" for call in calls)
    assert sections[-1] == "noted a post-completion change in work-pd-4/workrun-task1-pd-4"


def test_the_workspace_keeps_its_evidence_after_registration(monkeypatch, tmp_path):
    """The generation number is the guard against double-acting — nothing in
    the serving workspace is deleted."""
    calls = []
    wire_response(monkeypatch, tmp_path, calls)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "plan.md").write_text(PLAN_TEXT)
    (workspace / "task1.md").write_text("# First\na")
    (workspace / "task2.md").write_text("# Second\nb")

    zulip_listener.handle_superdirector_response(
        Client(calls), CHANNEL, TOPIC, PROJECT, workspace
    )

    remaining = sorted(path.name for path in workspace.iterdir())
    assert remaining == ["plan.md", "task1.md", "task2.md"]


def test_a_failed_reconcile_is_reported_not_swallowed(monkeypatch, tmp_path):
    calls = []
    wire_response(monkeypatch, tmp_path, calls)

    def explode(project, channel, topic, plan_dir):
        raise zulip_listener.ListenerError("plane is down")

    monkeypatch.setattr(zulip_listener, "reconcile_task_files", explode)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "plan.md").write_text(PLAN_TEXT)

    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.handle_superdirector_response(
            Client(calls), CHANNEL, TOPIC, PROJECT, workspace
        )


def test_a_run_that_wrote_nothing_changes_nothing(monkeypatch, tmp_path):
    """A question-only run: the reply is the whole outcome."""
    calls = []
    wire_response(monkeypatch, tmp_path, calls)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)

    sections, resolve_after = zulip_listener.handle_superdirector_response(
        Client(calls), CHANNEL, TOPIC, PROJECT, workspace
    )

    assert calls == []
    assert sections == []
    assert resolve_after is False


def test_replanning_reuses_the_work_channel(monkeypatch, tmp_path):
    """`create_channel` is subscribe-based and idempotent, which is what makes
    a second planning round safe — the channel is joined, not duplicated."""
    calls = []
    client = Client(calls)
    wire_response(monkeypatch, tmp_path, calls)
    for number in (1, 4):
        workspace = superdirector_dir(tmp_path, number)
        workspace.mkdir(parents=True)
        (workspace / "plan.md").write_text(PLAN_TEXT)
        zulip_listener.handle_superdirector_response(
            client, CHANNEL, TOPIC, PROJECT, workspace
        )

    names = [call[1] for call in calls if call[0] == "create-channel"]
    assert names == ["work-pd-4", "work-pd-4"]
    # The Sub-Work keys no longer carry the generation, so nothing has to be
    # kept clear of a cancelled generation's keys any more.
    assert [call[1] for call in calls if call[0] == "reconcile"] == [
        superdirector_dir(tmp_path, 1), superdirector_dir(tmp_path, 4)
    ]


def test_start_flag_moves_the_work_to_in_progress(monkeypatch, tmp_path):
    calls = []
    wire_response(monkeypatch, tmp_path, calls)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "start.flag").touch()

    sections, resolve_after = zulip_listener.handle_superdirector_response(
        Client(calls), CHANNEL, TOPIC, PROJECT, workspace
    )

    assert calls == [("transition", "started")]
    assert sections == ["mission PD-4 is now In Progress"]
    assert resolve_after is False


def test_cancel_flag_cancels_everything_and_archives_the_work_channel(monkeypatch, tmp_path):
    """Mission cancel is the only remaining cancel-everything path, and the
    only thing that retires a work- channel. Nothing is re-created after it,
    so the archived channel's retained name cannot collide."""
    calls = []
    client = Client(calls)
    client.channels_list = [
        {"name": CHANNEL, "stream_id": 7, "folder_id": None},
        {"name": "work-pd-4", "stream_id": 8, "folder_id": None},
    ]
    wire_response(monkeypatch, tmp_path, calls, cancelled=3)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "cancel.flag").touch()

    sections, resolve_after = zulip_listener.handle_superdirector_response(
        client, CHANNEL, TOPIC, PROJECT, workspace
    )

    assert [call[0] for call in calls] == [
        "cancel-subs", "transition", "channels", "archive"
    ]
    assert calls[-1] == ("archive", 8)
    assert sections == [
        "mission PD-4 is cancelled along with 3 sub-work(s); resolving this topic",
        "archived work-pd-4",
    ]
    assert resolve_after is True


def test_cancelling_a_mission_that_never_got_a_channel_is_quiet(monkeypatch, tmp_path):
    calls = []
    wire_response(monkeypatch, tmp_path, calls)
    workspace = superdirector_dir(tmp_path)
    workspace.mkdir(parents=True)
    (workspace / "cancel.flag").touch()

    sections, _ = zulip_listener.handle_superdirector_response(
        Client(calls), CHANNEL, TOPIC, PROJECT, workspace
    )

    assert sections[-1] == "no work-pd-4 channel to archive"
    assert not any(call[0] == "archive" for call in calls)


def test_a_mission_serving_opens_one_run_topic_per_task(monkeypatch, tmp_path):
    """End to end through the skeleton: planning a mission leaves a work-
    channel holding one topic per task, each carrying its task content."""
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)
    changes = [
        zulip_listener.TaskChange(1, "created", "First", "# First\n\na\n", "PD-5"),
        zulip_listener.TaskChange(2, "created", "Second", "# Second\n\nb\n", "PD-6"),
    ]
    wire_response(monkeypatch, tmp_path, calls, changes=changes)
    monkeypatch.setattr(
        zulip_listener,
        "run_superdirector",
        lambda prompt, cwd: (
            (superdirector_dir(tmp_path) / "plan.md").write_text(PLAN_TEXT) and "" or "planned"
        ),
    )

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    created = next(call for call in calls if call[0] == "create-channel")
    assert created[1] == "work-pd-4"
    assert [call[2:] for call in calls if call[0] == "post"] == [
        ("workrun-task1-pd-4", "# First\n\na\n"),
        ("workrun-task2-pd-4", "# Second\n\nb\n"),
    ]
    reply = [call for call in calls if call[0] == "write"][-1][2]
    assert "opened work-pd-4/workrun-task1-pd-4" in reply
    assert "opened work-pd-4/workrun-task2-pd-4" in reply


def test_handle_topic_resolves_the_topic_after_the_final_reply(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)
    wire_response(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        zulip_listener,
        "run_superdirector",
        lambda prompt, cwd: (
            (superdirector_dir(tmp_path) / "cancel.flag").touch() or "cancelling"
        ),
    )

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    # The ✔ rename comes after the final reply so the whole thread moves.
    assert [call[0] for call in calls][-3:] == ["write", "history", "resolve"]
    assert calls[-1] == ("resolve", 1, TOPIC)


def test_handle_topic_reports_a_response_handling_failure(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    wire_response(monkeypatch, tmp_path, calls)

    def explode(project, channel, topic, group):
        raise zulip_listener.ListenerError("plane is down")

    monkeypatch.setattr(zulip_listener, "transition_work", explode)
    monkeypatch.setattr(
        zulip_listener,
        "run_superdirector",
        lambda prompt, cwd: (
            (superdirector_dir(tmp_path) / "start.flag").touch() or "starting"
        ),
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
            # per round: chatlog read, handoff lookup, re-check — twice.
            self.scripts = [
                [first], [first], [first, mid_run],
                [first, mid_run], [first, mid_run], [first, mid_run],
            ]

        def topic_history(self, channel, topic, num_before):
            calls.append(("history", channel, topic, num_before))
            return self.scripts.pop(0)

    zulip_listener.handle_topic(ScriptedClient(), CHANNEL, TOPIC)

    assert [call[0] for call in calls].count("superdirector") == 2
    acks = [call for call in calls if call[0] == "write" and call[2] == zulip_listener.ACK_TEXT]
    assert len(acks) == 2
    # The second round is its own generation, with the fuller chatlog.
    assert (superdirector_dir(tmp_path, 2) / "chatlog.md").read_text().endswith("one more thing\n")


def test_superdirector_prompt_points_at_the_workspace(monkeypatch, tmp_path):
    guide_dir = tmp_path / "workplan_superdirector"
    guide_dir.mkdir(parents=True)
    (guide_dir / "guide.md").write_text("GUIDE TEXT\n")
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)
    workspace = tmp_path / "ws"

    prompt = zulip_listener.superdirector_prompt("Autolab", workspace, plane_files=False)
    assert f'("chatlog.md") is placed in "{workspace}"' in prompt
    assert "You are 'Autolab' in the chatlog." in prompt
    assert f'"plan.md", "task[N].md", the flags — into "{workspace}"' in prompt
    assert "Your working directory is the project itself." in prompt
    assert "currently registered" not in prompt
    assert prompt.endswith("GUIDE TEXT")

    prompt = zulip_listener.superdirector_prompt("Autolab", workspace, plane_files=True)
    assert (
        "The currently registered mission and tasks are placed in "
        f'"{workspace / "current"}".'
    ) in prompt


def test_guide_refuses_to_start_without_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)
    with pytest.raises(GuideError):
        zulip_listener.guide("workplan_superdirector", "guide.md")


# --- serving one task on a workrun- topic ---------------------------------------
#
# A workrun- topic is bound to one Sub-Work by its own name and lives in that
# mission's work- channel. It is a conversation, not a button: every human
# post re-serves it, and `report.md` — the agreement signal the guide asks
# for — is what closes it.

WORK_CHANNEL = "work-pd-4"
WORKRUN_TOPIC = "workrun-task2-pd-4"
BINDING = "[AUTO] project: demo-project; mission: pj-demo-project/workplan-one"

TARGET = zulip_listener.RunTarget(
    zulip_listener.Work("demo-project", "Add the README", "Write it.", "p1", "i1"),
    "PD-6", 2, "PD-4", "Fix title screen",
)


class RunClient(Client):
    """A client whose realm already holds the mission's work- channel."""

    channels_list = [
        {"name": CHANNEL, "stream_id": 7, "folder_id": None},
        {"name": WORK_CHANNEL, "stream_id": 8, "folder_id": None, "description": BINDING},
    ]


def calls_of(calls, kind):
    return [call for call in calls if call[0] == kind]


def last_reply(calls):
    """The final reply — after it, serve_topic re-checks history, so the last
    call is a read, not a write."""
    return calls_of(calls, "write")[-1][2]


#: What `serve_topic` prefixes every reply with: the last other speaker, so
#: their next turn happens. In these suites that is always the Developer.
HANDOFF = "@**Developer**\n\n"


def wire_run(monkeypatch, tmp_path, calls, *, target=TARGET, report=None,
             output="work done", pushed=True, ledger=None):
    monkeypatch.setattr(
        zulip_listener, "AGENTCHAT_LEDGER", ledger or (tmp_path / "ledger.jsonl")
    )
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    monkeypatch.setattr(zulip_listener, "RECORDS_ROOT", tmp_path / "records")
    monkeypatch.setattr(
        zulip_listener, "init_project", lambda project: calls.append(("init", project)) or "ok"
    )
    monkeypatch.setattr(
        zulip_listener,
        "run_target",
        lambda project, channel, topic, serial: (
            calls.append(("target", project, channel, topic, serial)) or target
        ),
    )
    monkeypatch.setattr(
        topics,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )

    def supercoder(prompt, cwd, on_event=None, home=None):
        calls.append(("supercoder", prompt, cwd, home))
        workspace = Path(re.search(r'is placed in "([^"]+)"', prompt).group(1))
        if report is not None:
            (workspace / "report.md").write_text(report)
        return output

    monkeypatch.setattr(zulip_listener, "workrun_supercoder", supercoder)
    monkeypatch.setattr(
        zulip_listener,
        "report_work",
        lambda project_id, issue_id, text, ok: (
            calls.append(("report", project_id, issue_id, text, ok)) or ("PD-6", bool(text), ok)
        ),
    )
    monkeypatch.setattr(zulip_listener, "load_gitea_config", lambda: "gitea-config")
    monkeypatch.setattr(
        zulip_listener,
        "commit_all_and_push",
        lambda config, workspace, message: (
            calls.append(("push", workspace, message)) or pushed
        ),
    )
    guides = tmp_path / "guides"
    (guides / "workrun_supercoder").mkdir(parents=True)
    (guides / "workrun_supercoder" / "guide.md").write_text("RUN GUIDE")
    monkeypatch.setattr(zulip_listener, "GUIDES", guides)


def supercoder_dir(tmp_path, number=1):
    return tmp_path / "topics" / WORK_CHANNEL / WORKRUN_TOPIC / str(number) / "supercoder"


@pytest.mark.parametrize(
    "channel,topic",
    [
        ("general", "workrun-1"),               # the old any-channel button
        (CHANNEL, "workrun-task1-pd-4"),        # right name, wrong channel
        (WORK_CHANNEL, "workrun-something"),    # right channel, no serial
    ],
)
def test_a_run_topic_that_is_not_bound_to_a_task_is_explained(monkeypatch, tmp_path,
                                                              channel, topic):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)
    zulip_listener.handle_workrun(RunClient(calls), channel, topic)

    assert not any(call[0] in {"target", "supercoder"} for call in calls)
    assert last_reply(calls) == HANDOFF + zulip_listener.WRONG_PLACE_REPLY


def test_the_previous_task_gate_answers_before_any_cost(monkeypatch, tmp_path):
    calls = []
    blocked = zulip_listener.RunTarget(TARGET.work, "PD-6", 2, "PD-4", "Fix title screen",
                                       blocked_by="PD-5")
    wire_run(monkeypatch, tmp_path, calls, target=blocked)

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert not any(call[0] in {"init", "supercoder"} for call in calls)
    assert last_reply(calls) == HANDOFF + f"{zulip_listener.PREVIOUS_WORK_REPLY} (PD-5)"


def test_a_serving_runs_the_supercoder_in_the_project_with_its_workspace(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert [call[0] for call in calls if call[0] not in {"whoami", "history", "channels"}] == [
        "write", "target", "init", "supercoder", "write",
    ]
    # The serial comes from the topic name, the project and mission key from
    # the channel description.
    assert calls_of(calls, "target")[0][1:] == ("demo-project", CHANNEL, "workplan-one", 2)
    prompt, cwd, home = next(
        (call[1], call[2], call[3]) for call in calls if call[0] == "supercoder"
    )
    # The run posts as this task. Whatever it asks another agent is recorded
    # against this topic, so the answer brings the task back.
    assert home == (WORK_CHANNEL, WORKRUN_TOPIC)
    workspace = supercoder_dir(tmp_path)
    assert cwd == tmp_path / "projects" / "demo-project"
    assert str(workspace) in prompt
    assert prompt.endswith("RUN GUIDE")
    # The task travels in the prompt, read from Plane — the task[N].md the
    # superdirector wrote lives in another generation's directory.
    assert "# Add the README\n\nWrite it." in prompt
    assert (workspace / "chatlog.md").read_text() == "[Developer] Build it\n"
    # No report: the conversation is simply not finished. Nothing closes.
    assert not any(call[0] in {"report", "push"} for call in calls)
    assert last_reply(calls) == HANDOFF + "work done"


def test_each_serving_of_a_run_topic_cuts_a_new_generation(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)
    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)
    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert supercoder_dir(tmp_path, 1).is_dir()
    assert supercoder_dir(tmp_path, 2).is_dir()


def test_a_report_completes_the_task_records_it_and_resolves_the_topic(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, report="all good\n")

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert calls_of(calls, "report")[0][1:] == ("p1", "i1", "all good\n", True)
    # The devlog record is deterministic handler code, never the agent's git.
    devlog = tmp_path / "projects" / "demo-project" / "devlog"
    task_dir = devlog / "pd-4-fix-title-screen" / "task-2"
    assert (task_dir / "work.md").read_text() == "# Add the README\n\nWrite it.\n"
    assert (task_dir / "report.md").read_text() == "all good\n"
    assert calls_of(calls, "push")[0][1:] == (devlog, "[AUTO] task 2 report for PD-4")
    outcome = last_reply(calls)
    assert "task PD-6: commented yes, Done yes; resolving this topic" in outcome
    assert "recorded pd-4-fix-title-screen/task-2 in devlog and pushed" in outcome
    assert any(call[0] == "resolve" for call in calls)


def test_the_mission_devlog_directory_is_minted_once_and_then_found(monkeypatch, tmp_path):
    """A later re-plan may rewrite the Work title; a record that moved would
    stop being a record, so the directory is looked up by its label prefix."""
    devlog = tmp_path / "projects" / "demo-project" / "devlog"
    (devlog / "pd-4-fix-title-screen").mkdir(parents=True)
    calls = []
    renamed = zulip_listener.RunTarget(TARGET.work, "PD-6", 2, "PD-4",
                                       "Rewrite the whole title sequence")
    wire_run(monkeypatch, tmp_path, calls, target=renamed, report="done")

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert sorted(path.name for path in devlog.iterdir()) == ["pd-4-fix-title-screen"]


@pytest.mark.parametrize(
    "title,stem",
    [
        ("Fix title screen", "fix-title-screen"),
        ("  A & B: ship it!  ", "a-b-ship-it"),
        ("日本語だけ", "mission"),
    ],
)
def test_title_slug_stays_one_readable_path_component(title, stem):
    assert zulip_listener.title_slug(title) == stem


def test_a_run_that_said_nothing_still_closes_on_its_report(monkeypatch, tmp_path):
    """The harness stopped failing a run that wrote files and no farewell;
    `report.md`, not the answer text, is what says the task is done."""
    calls = []
    wire_run(monkeypatch, tmp_path, calls, report="all good",
             output=zulip_listener.NO_CLOSING_MESSAGE)

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert calls_of(calls, "report")[0][1:] == ("p1", "i1", "all good", True)
    outcome = last_reply(calls)
    assert zulip_listener.NO_CLOSING_MESSAGE in outcome
    assert "failed" not in outcome


def test_a_failed_supercoder_run_is_reported_into_the_topic(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)

    def explode(prompt, cwd, on_event=None, home=None):
        raise zulip_listener.ListenerError("claude_code timed out")

    monkeypatch.setattr(zulip_listener, "workrun_supercoder", explode)
    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert "failed during supercoder: claude_code timed out" in last_reply(calls)
    assert not any(call[0] == "resolve" for call in calls)


def test_a_work_channel_without_a_binding_is_reported(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)

    class Bare(RunClient):
        channels_list = [{"name": WORK_CHANNEL, "stream_id": 8, "description": "made by hand"}]

    zulip_listener.handle_workrun(Bare(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert "failed during reading the binding" in last_reply(calls)
    assert not any(call[0] == "supercoder" for call in calls)


# --- the board reaches both runs (agent_standardize p6) ----------------------
#
# The superdirector needs it to write a delegation into a task; the supercoder
# needs it to perform one. Neither learns any agent's name from autolab's own
# code — the file is the whole channel that knowledge travels through.


def test_the_planning_run_gets_the_board_in_its_workspace(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    client = Client(calls, board={"intro-agforge-agstudio1": FORGE_INTRO})

    zulip_listener.handle_topic(client, CHANNEL, TOPIC)

    text = (superdirector_dir(tmp_path) / "tools" / "agents.md").read_text()
    assert FORGE_INTRO in text
    prompt = next(call[1] for call in calls if call[0] == "superdirector")
    assert str(superdirector_dir(tmp_path) / "tools" / "agents.md") in prompt


def test_the_task_run_gets_the_board_in_its_workspace(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)
    client = RunClient(calls, board={"intro-agforge-agstudio1": FORGE_INTRO})

    zulip_listener.handle_workrun(client, WORK_CHANNEL, WORKRUN_TOPIC)

    text = (supercoder_dir(tmp_path) / "tools" / "agents.md").read_text()
    assert FORGE_INTRO in text
    prompt = next(call[1] for call in calls if call[0] == "supercoder")
    assert str(supercoder_dir(tmp_path) / "tools" / "agents.md") in prompt


def test_an_empty_board_still_lets_a_run_happen(monkeypatch, tmp_path):
    """Nobody has introduced themselves is a fact, not a failed serving."""
    calls = []
    wire_run(monkeypatch, tmp_path, calls)

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    assert "No agent has introduced itself" in (
        supercoder_dir(tmp_path) / "tools" / "agents.md"
    ).read_text()
    assert any(call[0] == "supercoder" for call in calls)


def test_the_board_is_re_harvested_for_every_serving(monkeypatch, tmp_path):
    """An agent that changed its entrance this morning is reachable this
    afternoon with no deploy, which only holds if nothing is cached."""
    calls = []
    wire_run(monkeypatch, tmp_path, calls)

    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)
    zulip_listener.handle_workrun(
        RunClient(calls, board={"intro-new": "hello"}), WORK_CHANNEL, WORKRUN_TOPIC
    )

    assert "No agent has introduced itself" in (
        supercoder_dir(tmp_path, 1) / "tools" / "agents.md"
    ).read_text()
    assert "hello" in (supercoder_dir(tmp_path, 2) / "tools" / "agents.md").read_text()


def test_dispatch_routes_run_topics_anywhere_and_mission_topics_only_in_projects(monkeypatch):
    routed = []
    monkeypatch.setattr(
        zulip_listener, "handle_workrun",
        lambda client, channel, topic: routed.append(("run", channel, topic)),
    )
    monkeypatch.setattr(
        zulip_listener, "handle_topic",
        lambda client, channel, topic: routed.append(("mission", channel, topic)),
    )

    monkeypatch.setattr(
        zulip_listener, "handle_bmining",
        lambda client, channel, topic: routed.append(("bmining", channel, topic)),
    )

    zulip_listener.dispatch(None, "general", "workrun-1")
    zulip_listener.dispatch(None, CHANNEL, "workrun-2")
    zulip_listener.dispatch(None, CHANNEL, TOPIC)
    zulip_listener.dispatch(None, "general", "workplan-stray")  # silently ignored
    zulip_listener.dispatch(None, CHANNEL, "bmining-idea")
    zulip_listener.dispatch(None, "general", "bmining-stray")  # silently ignored

    assert routed == [
        ("run", "general", "workrun-1"),
        ("run", CHANNEL, "workrun-2"),
        ("mission", CHANNEL, TOPIC),
        ("bmining", CHANNEL, "bmining-idea"),
    ]


def test_the_listener_never_widens_its_own_subscriptions():
    """Subscription is the project creator's routing decision, not the listener's.

    The listener used to put every active user in every `pj-*` channel every
    60s. Two autolab instances then heard every project, and `workplan-`/`workrun-`
    topics have no addressing rule to tell them apart.
    """
    assert not hasattr(zulip_listener, "subscribe_project_channels")
    assert not hasattr(zulip_listener, "subscription_loop")


@pytest.mark.parametrize("channel", ["general", "pj-x", "pj-Bad_Name"])
def test_project_from_channel_rejects_non_project_channels(channel):
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.project_from_channel(channel)


# --- bmining topics ---------------------------------------------------------


BMINING_TOPIC = "bmining-idea"


def wire_bmining(monkeypatch, tmp_path, calls, *, reply="director says hi",
                 committed=False, director=None):
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(zulip_listener, "RECORDS_ROOT", tmp_path / "records")
    monkeypatch.setattr(
        zulip_listener, "init_project", lambda project: calls.append(("init", project)) or "success"
    )
    monkeypatch.setattr(
        zulip_listener,
        "run_director",
        director or (lambda prompt, cwd: calls.append(("director", prompt, cwd)) or reply),
    )
    monkeypatch.setattr(zulip_listener, "load_gitea_config", lambda: "gitea-config")
    monkeypatch.setattr(
        zulip_listener,
        "commit_all_and_push",
        lambda config, workspace, message: (
            calls.append(("push", config, workspace, message)) or committed
        ),
    )
    monkeypatch.setattr(
        topics,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )
    guides = tmp_path / "guides"
    (guides / "bmining_director").mkdir(parents=True)
    (guides / "bmining_director" / "guide.md").write_text("BMINING GUIDE")
    monkeypatch.setattr(zulip_listener, "GUIDES", guides)


def bmining_paths(tmp_path):
    direction = tmp_path / "projects" / PROJECT / "direction"
    return direction, direction / ".local" / "work"


def test_handle_bmining_places_chatlog_runs_director_and_replies(monkeypatch, tmp_path):
    calls, seen = [], {}
    direction, work = bmining_paths(tmp_path)

    def director(prompt, cwd):
        seen["chatlog"] = (work / "chatlog.md").read_text()
        calls.append(("director", prompt, cwd))
        return "director says hi"

    wire_bmining(monkeypatch, tmp_path, calls, director=director)
    zulip_listener.handle_bmining(Client(calls), CHANNEL, BMINING_TOPIC)

    kinds = [call[0] for call in calls if call[0] != "history"]
    assert kinds == ["whoami", "write", "init", "director", "push", "write"]
    assert calls[1][2] == zulip_listener.ACK_TEXT
    assert "[Developer] Build it" in seen["chatlog"]
    directed = next(call for call in calls if call[0] == "director")
    assert directed[2] == direction
    assert "BMINING GUIDE" in directed[1]
    assert '".local/work/chatlog.md"' in directed[1]
    assert last_reply(calls) == HANDOFF + "director says hi"


def test_bmining_work_directory_is_removed_after_the_reply(monkeypatch, tmp_path):
    calls = []
    _, work = bmining_paths(tmp_path)
    work.mkdir(parents=True)
    (work / "leftover.txt").write_text("stale")

    wire_bmining(monkeypatch, tmp_path, calls)
    zulip_listener.handle_bmining(Client(calls), CHANNEL, BMINING_TOPIC)

    assert not work.exists()


def test_bmining_replaces_a_leftover_chatlog(monkeypatch, tmp_path):
    calls, seen = [], {}
    _, work = bmining_paths(tmp_path)
    work.mkdir(parents=True)
    (work / "chatlog.md").write_text("stale conversation")

    def director(prompt, cwd):
        seen["chatlog"] = (work / "chatlog.md").read_text()
        return "reply"

    wire_bmining(monkeypatch, tmp_path, calls, director=director)
    zulip_listener.handle_bmining(Client(calls), CHANNEL, BMINING_TOPIC)

    assert "stale conversation" not in seen["chatlog"]
    assert "[Developer] Build it" in seen["chatlog"]


def test_bmining_reports_the_push_when_the_clone_was_dirty(monkeypatch, tmp_path):
    calls = []
    direction, _ = bmining_paths(tmp_path)
    wire_bmining(monkeypatch, tmp_path, calls, committed=True)

    zulip_listener.handle_bmining(Client(calls), CHANNEL, BMINING_TOPIC)

    pushed = next(call for call in calls if call[0] == "push")
    assert pushed[2] == direction
    assert BMINING_TOPIC in pushed[3]
    assert "recorded notes committed and pushed" in last_reply(calls)


def test_bmining_stays_quiet_about_a_clean_clone(monkeypatch, tmp_path):
    calls = []
    wire_bmining(monkeypatch, tmp_path, calls, committed=False)

    zulip_listener.handle_bmining(Client(calls), CHANNEL, BMINING_TOPIC)

    assert "committed" not in last_reply(calls)


def test_a_failed_director_run_still_cleans_up_and_answers(monkeypatch, tmp_path):
    calls = []
    _, work = bmining_paths(tmp_path)

    def director(prompt, cwd):
        raise zulip_listener.ListenerError("boom")

    wire_bmining(monkeypatch, tmp_path, calls, director=director)
    zulip_listener.handle_bmining(Client(calls), CHANNEL, BMINING_TOPIC)

    assert not work.exists()
    assert "failed during director: boom" in last_reply(calls)
    assert all(call[0] != "push" for call in calls)


def test_bmining_is_swept(monkeypatch):
    assert "bmining-" == zulip_listener.BMINING_TOPIC_PREFIX


# --- live progress on workrun- topics --------------------------------------------


def test_progress_line_shapes():
    assert zulip_listener.progress_line(
        {"type": "text", "text": "  working\non it  "}
    ) == "💬 working on it"
    assert zulip_listener.progress_line({"type": "thinking", "thinking": "..."}) is None
    assert zulip_listener.progress_line(
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
    ) == "🔧 Bash: git status"
    # agcode's `run` tool spells its argument `command` too; a tool with no
    # showable argument still names itself.
    assert zulip_listener.progress_line(
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}}
    ) == "🔧 TodoWrite"


def test_run_progress_throttles_then_posts(monkeypatch):
    posts = []
    monkeypatch.setattr(
        zulip_listener, "topic_write",
        lambda topic, text, **kwargs: posts.append((topic, text)),
    )
    progress = zulip_listener.RunProgress(None, "general", "workrun-1", interval_s=1000)

    progress({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]}})
    assert posts == []  # inside the interval: accumulated, not posted

    progress.last_post -= 2000
    progress({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "done reading"}]}})
    assert posts == [("workrun-1", "🔧 Read: a.py\n💬 done reading")]

    progress({"type": "user", "message": {"content": []}})  # not an assistant event
    progress.flush()
    assert len(posts) == 1  # nothing pending: flush posts nothing


def test_a_serving_posts_the_progress_tail_before_the_outcome(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)

    def streaming_run(prompt, cwd, on_event=None, home=None):
        on_event({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "uv run pytest"}}]}})
        return "work done"

    monkeypatch.setattr(zulip_listener, "workrun_supercoder", streaming_run)
    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)

    writes = calls_of(calls, "write")
    assert writes[0][2] == zulip_listener.ACK_TEXT
    assert writes[1][2] == "🔧 Bash: uv run pytest"  # the flushed tail
    assert "work done" in writes[2][2]  # then the outcome


# --- a run that ends without a closing message --------------------------------


def test_run_wrappers_report_a_missing_closing_message(monkeypatch, tmp_path):
    """The harness stopped failing such runs (their work is their files, not
    their farewell), so each wrapper says so rather than returning nothing:
    a serving whose sections are all empty posts nothing at all, and a topic
    that got only an ack drops out of the sweep until a human posts again."""
    monkeypatch.setattr(zulip_listener, "RECORDS_ROOT", tmp_path / "records")
    monkeypatch.setattr(
        zulip_listener, "run_role",
        lambda role, prompt, **kwargs: ("   \n", {"outcome": "done"}, 0),
    )
    monkeypatch.setattr(zulip_listener, "bmining_prompt", lambda bot_name: "PROMPT")

    assert zulip_listener.workrun_supercoder("p", tmp_path) == zulip_listener.NO_CLOSING_MESSAGE
    assert zulip_listener.run_superdirector("p", tmp_path) == zulip_listener.NO_CLOSING_MESSAGE
    assert zulip_listener.run_director("p", tmp_path) == zulip_listener.NO_CLOSING_MESSAGE


def test_run_progress_logs_a_failed_post_and_keeps_going(monkeypatch):
    """A display that breaks must not kill the run — but a topic that went
    quiet because posting failed has to be tellable from one that went quiet
    because the run stalled."""
    logged = []
    monkeypatch.setattr(zulip_listener, "log", logged.append)

    def refuse(topic, text, **kwargs):
        raise zulip_listener.ZulipError("HTTP 500")

    monkeypatch.setattr(zulip_listener, "topic_write", refuse)
    progress = zulip_listener.RunProgress(None, "general", "workrun-1", interval_s=0)

    progress({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "still working"}]}})

    assert len(logged) == 1
    assert "could not post progress to 'general'/'workrun-1'" in logged[0]
    assert "dropping 1 line(s)" in logged[0]
    # Dropped, not requeued: a Zulip outage must not grow `pending` for the
    # rest of a twenty-minute run.
    assert progress.pending == []


def test_topic_filter_sweeps_the_whole_own_channel_and_prefixes_elsewhere(monkeypatch):
    monkeypatch.setattr(zulip_listener, "instance_name", lambda: "autolab-here1")

    assert zulip_listener.topic_filter("autolab-here1", "how-do-i-ask")
    assert zulip_listener.topic_filter(CHANNEL, "workplan-thing")
    assert zulip_listener.topic_filter("work-pa-12", "workrun-task1-pa-12")
    assert not zulip_listener.topic_filter("general", "just-chatting")


def test_the_own_channel_only_redirects_and_never_executes(monkeypatch):
    monkeypatch.setattr(zulip_listener, "instance_name", lambda: "autolab-here1")
    for name in ("handle_workrun", "handle_topic", "handle_bmining"):
        monkeypatch.setattr(
            zulip_listener, name,
            lambda *a, **k: pytest.fail("the entrance must not execute anything"),
        )
    sent = []

    class Client:
        def send_to_channel(self, channel, topic, text):
            sent.append((channel, topic, text))

    for topic in ("how-do-i-ask", "workplan-here", "workrun-here"):
        zulip_listener.dispatch(Client(), "autolab-here1", topic)

    assert [(c, t) for c, t, _ in sent] == [
        ("autolab-here1", "how-do-i-ask"),
        ("autolab-here1", "workplan-here"),
        ("autolab-here1", "workrun-here"),
    ]


def test_the_entrance_reply_names_the_instance_and_the_workplan_contract(monkeypatch):
    monkeypatch.setattr(zulip_listener, "instance_name", lambda: "autolab-here1")
    reply = zulip_listener.entrance_reply()
    assert "autolab-here1" in reply
    assert "workplan-" in reply
    assert "pj-" in reply


# --- the callback: a delegation that outlives its run ----------------------


FORGE_CHANNEL = "agforge-agstudio1"
FORGE_TOPIC = "assetplan-enemy-sprite"


class DelegatingClient(RunClient):
    """A `RunClient` that also holds the remote conversation."""

    def __init__(self, calls, remote_history=None):
        super().__init__(calls)
        self.remote_history = remote_history or [
            history_message(sender_id=13, name="Forge", content="Work registered as F2-9.")
        ]

    def topic_history(self, channel, topic, num_before):
        if channel == FORGE_CHANNEL:
            self.calls.append(("history", channel, topic, num_before))
            return list(self.remote_history)
        return super().topic_history(channel, topic, num_before)


def record_delegation(ledger, home_topic=WORKRUN_TOPIC):
    participation.record(
        ledger,
        remote=participation.Conversation(FORGE_CHANNEL, FORGE_TOPIC),
        home=participation.Conversation(WORK_CHANNEL, home_topic),
        message_id=77,
    )


def test_a_mention_serves_the_task_the_request_was_made_for(monkeypatch, tmp_path):
    """p7's whole shape for autolab: the run that delegated is long over, and
    forge's answer is what starts the next one."""
    calls = []
    ledger = tmp_path / "ledger.jsonl"
    wire_run(monkeypatch, tmp_path, calls, ledger=ledger)
    record_delegation(ledger)

    zulip_listener.handle_mention(DelegatingClient(calls), FORGE_CHANNEL, FORGE_TOPIC)

    prompt, _, home = next(
        (call[1], call[2], call[3]) for call in calls if call[0] == "supercoder"
    )
    # The task is the subject: same workspace, same chatlog, same Plane target.
    assert home == (WORK_CHANNEL, WORKRUN_TOPIC)
    assert calls_of(calls, "target")[0][1:] == ("demo-project", CHANNEL, "workplan-one", 2)
    workspace = supercoder_dir(tmp_path)
    assert (workspace / "chatlog.md").read_text() == "[Developer] Build it\n"
    # forge's conversation is a file beside it, and the prompt says where.
    thread = workspace / "threads" / FORGE_CHANNEL / f"{FORGE_TOPIC}.md"
    assert "Work registered as F2-9." in thread.read_text()
    assert f'"{thread}"' in prompt
    # Everything posted went back to forge's topic; the task's own topic got
    # only what RunProgress puts there.
    assert {call[3].get("channel") for call in calls_of(calls, "write")} == {FORGE_CHANNEL}
    assert last_reply(calls) == "@**Forge**\n\nwork done"


def test_a_mention_no_task_delegated_to_costs_no_run(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)
    zulip_listener.handle_mention(RunClient(calls), FORGE_CHANNEL, FORGE_TOPIC)
    assert calls == []


def test_a_participation_that_is_not_a_task_is_not_guessed_at(monkeypatch, tmp_path):
    """Only `workrun-` topics delegate today. A ledger line pointing anywhere
    else is logged and dropped rather than routed by guesswork."""
    calls = []
    ledger = tmp_path / "ledger.jsonl"
    wire_run(monkeypatch, tmp_path, calls, ledger=ledger)
    record_delegation(ledger, home_topic="bmining-idea")
    zulip_listener.handle_mention(RunClient(calls), FORGE_CHANNEL, FORGE_TOPIC)
    assert calls == []


def test_a_task_with_no_delegation_gets_no_threads_sentence(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls)
    zulip_listener.handle_workrun(RunClient(calls), WORK_CHANNEL, WORKRUN_TOPIC)
    prompt = next(call[1] for call in calls if call[0] == "supercoder")
    assert "threads" not in prompt
