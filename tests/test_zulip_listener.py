from pathlib import Path

import pytest

from agag import topics
from agag.topics import GuideError

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
        topics,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text, kwargs)) or "success",
    )
    guides = tmp_path / "guides"
    (guides / "mission_front").mkdir(parents=True)
    (guides / "mission_front" / "guide.md").write_text("GUIDE TEXT")
    monkeypatch.setattr(zulip_listener, "GUIDES", guides)


PROJECT = "demo-project"
PLAN_TEXT = "# The plan\n\nStep one, step two."


def wire_response(
    monkeypatch,
    tmp_path,
    calls,
    *,
    superdirector="planning done",
    plan=PLAN_TEXT,
    tasks=("# First\ndo this",),
    cancelled=0,
):
    """Stub everything `handle_front_response` reaches outside the filesystem.

    The superdirector stub writes what a real run would leave behind in the
    project folder — `plan.md` and the task split — because the cleanup and
    the parent-work description are read off those files.
    """
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
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

    def superdirector_run(project_dir):
        calls.append(("superdirector", project_dir))
        if plan is not None:
            (project_dir / "plan.md").write_text(plan)
        for number, text in enumerate(tasks, start=1):
            (project_dir / f"task{number}.md").write_text(text)
        return superdirector

    monkeypatch.setattr(zulip_listener, "run_superdirector", superdirector_run)
    monkeypatch.setattr(
        zulip_listener,
        "register_task_files",
        lambda project, channel, topic, plan_dir, rev: (
            calls.append(("register", plan_dir, rev)) or [f"created sub-work rev {rev}"]
        ),
    )
    monkeypatch.setattr(
        zulip_listener,
        "transition_work",
        lambda project, channel, topic, group: calls.append(("transition", group)) or "PD-4",
    )


def front_dir(tmp_path, number=1):
    """Each serving works in a fresh generation `<N>/`, not one stable dir."""
    return tmp_path / "topics" / CHANNEL / TOPIC / str(number) / "front"


def project_dir(tmp_path, project=PROJECT):
    """The persistent project folder — `main/`, `direction/`, `devlog/` and,
    for the length of one planning round, the planning files."""
    return tmp_path / "projects" / project


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
    # The chatlog lands in this generation's front workspace.
    assert (front_dir(tmp_path) / "chatlog.md").read_text() == "[Developer] Build it\n"
    assert calls[4][1] == front_dir(tmp_path)
    assert calls[5][2] == front_dir(tmp_path)
    assert "mission and tasks" not in calls[5][1]
    assert calls[6][1:3] == (TOPIC, "front says hi")


def test_each_serving_cuts_a_new_generation(monkeypatch, tmp_path):
    """Before this, one stable `front/` was reused forever, so a continued
    conversation ran on top of the previous run's leftovers."""
    calls = []
    wire(monkeypatch, tmp_path, calls)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)
    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    assert front_dir(tmp_path, 1).is_dir()
    assert front_dir(tmp_path, 2).is_dir()
    assert [call[2] for call in calls if call[0] == "front"] == [
        front_dir(tmp_path, 1), front_dir(tmp_path, 2),
    ]


def test_handle_topic_mentions_plane_files_when_they_were_written(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls, plane_files=True)

    zulip_listener.handle_topic(Client(calls), CHANNEL, TOPIC)

    prompt = next(call[1] for call in calls if call[0] == "front")
    assert "The current mission and tasks are also placed in the working directory." in prompt
    assert prompt.endswith("GUIDE TEXT")


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
    assert "failed during chatlog" in calls[-1][2]


def test_an_empty_topic_costs_no_agent_run(monkeypatch, tmp_path):
    calls = []
    wire(monkeypatch, tmp_path, calls)
    zulip_listener.handle_topic(Client(calls, history=[]), CHANNEL, TOPIC)
    assert not any(call[0] == "front" for call in calls)
    assert calls[-1][2] == zulip_listener.EMPTY_REPLY


def test_new_mission_plans_in_the_project_folder_and_registers_the_plan(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls, cancelled=2)
    front = front_dir(tmp_path)
    front.mkdir(parents=True)
    (front / "new_mission.md").write_text("# Build it\n\nThe body.")

    sections, resolve_after = zulip_listener.handle_front_response(
        CHANNEL, TOPIC, PROJECT, front, 1
    )

    # The superdirector runs first now: the parent Work's description is what
    # it decided, which does not exist until it has run.
    assert [call[0] for call in calls] == [
        "superdirector", "upsert", "cancel-subs", "register",
    ]
    assert calls[0][1] == project_dir(tmp_path)
    # Title from the mission, description from the plan — the whole file,
    # heading included, because Step 6 reads it back as `plan.md`.
    assert calls[1][4:6] == ("Build it", PLAN_TEXT)
    assert calls[3][1] == project_dir(tmp_path)
    # The Sub-Work generation key is the generation number itself.
    assert calls[3][2] == 1
    assert sections == [
        "planning done",
        'updated PD-4 "Build it"',
        "cancelled 2 existing sub-work(s)",
        "created sub-work rev 1",
    ]
    assert resolve_after is False


def test_the_planning_files_are_cleared_once_plane_has_them(monkeypatch, tmp_path):
    """The project folder is persistent — no generation number separates one
    round from the next — so a registered round leaves nothing behind."""
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls, tasks=("# First\na", "# Second\nb"))
    front = front_dir(tmp_path)
    front.mkdir(parents=True)
    (front / "new_mission.md").write_text("# Build it\n\nThe body.")
    (project_dir(tmp_path) / "main").mkdir(parents=True)

    zulip_listener.handle_front_response(CHANNEL, TOPIC, PROJECT, front, 1)

    remaining = sorted(path.name for path in project_dir(tmp_path).iterdir())
    assert remaining == ["main"]
    # The front's own generation keeps its evidence.
    assert (front / "new_mission.md").exists()


def test_a_failed_registration_leaves_the_plan_for_a_retry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls)

    def explode(project, channel, topic, plan_dir, rev):
        raise zulip_listener.ListenerError("plane is down")

    monkeypatch.setattr(zulip_listener, "register_task_files", explode)
    front = front_dir(tmp_path)
    front.mkdir(parents=True)
    (front / "new_mission.md").write_text("# Build it\n\nThe body.")

    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.handle_front_response(CHANNEL, TOPIC, PROJECT, front, 1)

    # Paying sonnet twice for the same plan is the thing being avoided.
    assert sorted(path.name for path in project_dir(tmp_path).iterdir()) == [
        "new_mission.md", "plan.md", "task1.md",
    ]


def test_a_superdirector_that_wrote_no_plan_is_a_failure(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls, plan=None, tasks=())
    front = front_dir(tmp_path)
    front.mkdir(parents=True)
    (front / "new_mission.md").write_text("# Build it\n\nThe body.")

    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.handle_front_response(CHANNEL, TOPIC, PROJECT, front, 1)
    # Nothing reached Plane, so nothing may be reported as if it had.
    assert [call[0] for call in calls] == ["superdirector"]


def test_the_sub_work_key_follows_the_generation(monkeypatch, tmp_path):
    """A later generation's split must not collide with a cancelled one's
    keys, which live in Plane forever."""
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls)
    for number in (1, 4):
        front = front_dir(tmp_path, number)
        front.mkdir(parents=True)
        (front / "new_mission.md").write_text("# Build it\n\nThe body.")
        zulip_listener.handle_front_response(CHANNEL, TOPIC, PROJECT, front, number)
    assert [call[2] for call in calls if call[0] == "register"] == [1, 4]


def test_start_flag_moves_the_work_to_in_progress(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls)
    front = front_dir(tmp_path)
    front.mkdir(parents=True)
    (front / "start.flag").touch()

    sections, resolve_after = zulip_listener.handle_front_response(
        CHANNEL, TOPIC, PROJECT, front, 1
    )

    assert calls == [("transition", "started")]
    assert sections == ["mission PD-4 is now In Progress"]
    assert resolve_after is False


def test_cancel_flag_cancels_everything_and_requests_resolution(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    wire_response(monkeypatch, tmp_path, calls, cancelled=3)
    front = front_dir(tmp_path)
    front.mkdir(parents=True)
    (front / "cancel.flag").touch()

    sections, resolve_after = zulip_listener.handle_front_response(
        CHANNEL, TOPIC, PROJECT, front, 1
    )

    assert calls == [("cancel-subs",), ("transition", "cancelled")]
    assert sections == ["mission PD-4 is cancelled along with 3 sub-work(s); resolving this topic"]
    assert resolve_after is True


def test_handle_topic_resolves_the_topic_after_the_final_reply(monkeypatch, tmp_path):
    calls = []
    client = Client(calls)
    wire(monkeypatch, tmp_path, calls)
    wire_response(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        zulip_listener,
        "run_front",
        lambda prompt, cwd: (cwd / "cancel.flag").touch() or "cancelling",
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
        "run_front",
        lambda prompt, cwd: (cwd / "start.flag").touch() or "starting",
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
    # The second round is its own generation, with the fuller chatlog.
    assert (front_dir(tmp_path, 2) / "chatlog.md").read_text().endswith("one more thing\n")


def test_front_prompt_carries_autolabs_own_extra_line(monkeypatch, tmp_path):
    guide_dir = tmp_path / "mission_front"
    guide_dir.mkdir(parents=True)
    (guide_dir / "guide.md").write_text("GUIDE TEXT\n")
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)

    assert zulip_listener.front_prompt("Autolab", plane_files=False) == (
        "The chatlog is placed in the working directory. "
        "You are 'Autolab' in the chatlog.\n\nGUIDE TEXT"
    )
    assert zulip_listener.front_prompt("Autolab", plane_files=True) == (
        "The chatlog is placed in the working directory. "
        "You are 'Autolab' in the chatlog.\n"
        "The current mission and tasks are also placed in the working directory."
        "\n\nGUIDE TEXT"
    )


def test_guide_refuses_to_start_without_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(zulip_listener, "GUIDES", tmp_path)
    with pytest.raises(GuideError):
        zulip_listener.guide("mission_front", "guide.md")


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

    def work_run(workspace, asset_url=None):
        calls.append(("run", workspace, asset_url))
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


CHOSEN = zulip_listener.Work("demo-project", "Add the README", "Write it.", "p1", "i1")
ASSET_WORK = zulip_listener.Work(
    "demo-project", "Sprite sheet", "A 32x32 hero.", "p1", "i9", is_asset=True
)
ASSET_CHANNEL = "pj-demo-project"
ASSET_TOPIC = "create-asset_i9"


def test_handle_run_executes_comments_completes_and_cleans_up(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN, report="all good", success=True)

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    workspace = tmp_path / "projects" / "demo-project" / "main"
    assert [call[0] for call in calls] == ["write", "run", "report", "write"]
    assert calls[0][2] == zulip_listener.ACK_TEXT
    assert calls[1][1:] == (workspace, None)
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

    def capture(workspace, asset_url=None):
        seen["work.md"] = (workspace / ".local" / "work" / "work.md").read_text()
        return real_run(workspace, asset_url)

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

    def explode(workspace, asset_url=None):
        calls.append(("run", workspace, asset_url))
        raise zulip_listener.ListenerError("claude_code timed out")

    monkeypatch.setattr(zulip_listener, "run_work", explode)
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    assert "failed during work run: claude_code timed out" in calls[-1][2]
    assert not (tmp_path / "projects" / "demo-project" / "main" / ".local" / "work").exists()


# --- the asset state machine -----------------------------------------------
#
# The autolab Work stays `unstarted` through states 1-2, so `next_work()`
# keeps choosing it and everything behind it waits for the asset. Accepted
# for this episode.


def wire_asset(monkeypatch, tmp_path, calls, *, state, key=None, url="http://minio/fresh"):
    monkeypatch.setattr(
        zulip_listener, "asset_order",
        lambda project_id, channel, issue_id: (
            calls.append(("order-state", project_id, channel, issue_id)) or (state, None)
        ),
    )
    monkeypatch.setattr(
        zulip_listener, "find_asset_key",
        lambda client, channel, topic: calls.append(("find-key", channel, topic)) or key,
    )
    monkeypatch.setattr(
        zulip_listener, "resign", lambda k: calls.append(("resign", k)) or url
    )


def test_an_unordered_asset_is_ordered_and_the_serving_ends(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=ASSET_WORK)
    wire_asset(monkeypatch, tmp_path, calls, state="absent")
    direction = tmp_path / "projects" / "demo-project" / "direction"
    direction.mkdir(parents=True)
    (direction / "aesthetics.md").write_text("2D retro digital game art style\n")

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    # No coding run, and Plane is not written: the Work stays unstarted.
    assert [call[0] for call in calls] == ["write", "order-state", "write", "write"]
    order = calls[2]
    assert order[1] == ASSET_TOPIC
    # Self-contained: agforge's front reads the whole topic chatlog and its
    # generator plans from that, so the order carries everything.
    assert order[2] == (
        "# Sprite sheet\n\nA 32x32 hero.\n\n"
        "Art direction for this project:\n2D retro digital game art style"
    )
    assert f"asset ordered in {ASSET_CHANNEL}/{ASSET_TOPIC}" in calls[-1][2]


def test_an_order_without_aesthetics_still_stands_alone(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=ASSET_WORK)
    wire_asset(monkeypatch, tmp_path, calls, state="absent")
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)
    assert calls[2][2] == "# Sprite sheet\n\nA 32x32 hero."


def test_an_asset_in_progress_reports_and_fires_no_runcreate(monkeypatch, tmp_path):
    """The Omni Agent triggers `runcreate-` by hand this episode; autolab must
    never emit one."""
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=ASSET_WORK)
    wire_asset(monkeypatch, tmp_path, calls, state="working")

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    assert [call[0] for call in calls] == ["write", "order-state", "write"]
    assert f"asset in progress in {ASSET_CHANNEL}/{ASSET_TOPIC}" in calls[-1][2]
    assert not any("runcreate" in str(call) for call in calls)
    assert not any(call[0] in {"run", "report"} for call in calls)


def test_a_completed_asset_is_resigned_and_the_url_reaches_the_run(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=ASSET_WORK, report="done", success=True)
    wire_asset(monkeypatch, tmp_path, calls, state="done", key="files/2026-08-16/abc.zip")

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    assert [call[0] for call in calls] == [
        "write", "order-state", "find-key", "resign", "run", "report", "write",
    ]
    # Re-signed immediately before the run, so a 1200 s run cannot outlive
    # the 60-minute URL it was handed.
    assert calls[3][1] == "files/2026-08-16/abc.zip"
    assert calls[4][2] == "http://minio/fresh"
    assert "asset ready (files/2026-08-16/abc.zip)" in calls[-1][2]


def test_a_completed_asset_without_a_key_footer_is_a_failure(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=ASSET_WORK)
    wire_asset(monkeypatch, tmp_path, calls, state="done", key=None)

    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)

    assert "failed during asset check" in calls[-1][2]
    assert zulip_listener.S3_KEY_MARKER in calls[-1][2]
    assert not any(call[0] == "run" for call in calls)


def test_a_plain_work_never_touches_the_asset_route(monkeypatch, tmp_path):
    calls = []
    wire_run(monkeypatch, tmp_path, calls, chosen=CHOSEN)
    wire_asset(monkeypatch, tmp_path, calls, state="absent")
    zulip_listener.handle_run(Client(calls), "general", RUN_TOPIC)
    assert not any(call[0] in {"order-state", "find-key", "resign"} for call in calls)


def test_the_asset_note_is_appended_to_the_run_guide(monkeypatch, tmp_path):
    guides = tmp_path / "guides"
    (guides / "run_coding").mkdir(parents=True)
    (guides / "run_coding" / "guide.md").write_text("RUN GUIDE")
    monkeypatch.setattr(zulip_listener, "GUIDES", guides)

    assert zulip_listener.work_prompt(None) == "RUN GUIDE"
    prompt = zulip_listener.work_prompt("http://minio/x.zip")
    assert prompt.startswith("RUN GUIDE\n\nNote: the asset required by this work")
    assert "try to compromise" in prompt
    assert prompt.endswith("\nhttp://minio/x.zip")


def test_find_asset_key_takes_the_newest_footer(monkeypatch):
    class Topic:
        def topic_history(self, channel, topic, num_before):
            return [
                {"content": "result of x: http://old\n[S3KEY] files/old.zip"},
                {"content": "chatter with no footer"},
                {"content": "result of y: http://new\n[S3KEY] files/new.zip"},
            ]

    assert zulip_listener.find_asset_key(Topic(), ASSET_CHANNEL, ASSET_TOPIC) == (
        "files/new.zip"
    )


def test_find_asset_key_is_none_without_a_footer():
    class Topic:
        def topic_history(self, channel, topic, num_before):
            return [{"content": "no key here, just words about [S3KEY] in passing"}]

    assert zulip_listener.find_asset_key(Topic(), ASSET_CHANNEL, ASSET_TOPIC) is None


# --- answering agforge on a create- topic ----------------------------------
#
# The mention gate is the loop breaker: agforge reacts to any create- post
# that is not its own, so without the asymmetry the two bots would answer
# each other forever, one paid run per lap.


FORGE_ID = 13


def create_message(sender_id=FORGE_ID, name="Forge", content="@**Autolab** what size?", id=5):
    return {
        "id": id,
        "type": "stream",
        "sender_id": sender_id,
        "sender_full_name": name,
        "display_recipient": ASSET_CHANNEL,
        "subject": ASSET_TOPIC,
        "content": content,
    }


def wire_create(monkeypatch, tmp_path, calls, *, answer="64x64."):
    monkeypatch.setattr(zulip_listener, "TOPICS_ROOT", tmp_path / "topics")
    monkeypatch.setattr(zulip_listener, "RECORDS_ROOT", tmp_path / "records")
    monkeypatch.setattr(zulip_listener, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(
        zulip_listener,
        "topic_write",
        lambda topic, text, **kwargs: calls.append(("write", topic, text)) or "success",
    )
    monkeypatch.setattr(
        zulip_listener,
        "asset_answer_context",
        lambda project, work_id: (
            calls.append(("context", project, work_id)) or ("# The plan\n\nBody.", "# Sprite\n\nA hero.\n")
        ),
    )

    def answer_run(answer_dir, project_dir):
        calls.append(("answer-run", answer_dir, project_dir))
        if answer is not None:
            (answer_dir / "answer.md").write_text(answer)
        return "wrote it"

    monkeypatch.setattr(zulip_listener, "run_answer", answer_run)


class CreateClient(Client):
    def __init__(self, calls, history=None):
        super().__init__(calls, history=history or [create_message()])


def answer_dir(tmp_path, number=1):
    return tmp_path / "topics" / ASSET_CHANNEL / ASSET_TOPIC / str(number) / "answer"


def test_a_mentioned_question_is_answered_from_plane_and_posted(monkeypatch, tmp_path):
    calls = []
    wire_create(monkeypatch, tmp_path, calls)

    zulip_listener.handle_create(CreateClient(calls), ASSET_CHANNEL, ASSET_TOPIC)

    assert [call[0] for call in calls] == [
        "whoami", "history", "context", "answer-run", "write",
    ]
    # The work id is the topic name's tail.
    assert calls[2][1:] == ("demo-project", "i9")
    # Read from the answer generation, but run in the project folder.
    assert calls[3][1:] == (answer_dir(tmp_path), tmp_path / "projects" / "demo-project")
    assert (answer_dir(tmp_path) / "chatlog.md").read_text() == "[Forge] @**Autolab** what size?\n"
    assert (answer_dir(tmp_path) / "plan.md").read_text() == "# The plan\n\nBody."
    assert (answer_dir(tmp_path) / "task.md").read_text() == "# Sprite\n\nA hero.\n"
    # Posting it makes autolab the last non-forge poster, which is what lets
    # agforge's sweep pick the conversation back up.
    assert calls[-1][1:] == (ASSET_TOPIC, "64x64.")


def test_a_post_without_a_mention_costs_nothing(monkeypatch, tmp_path):
    """agforge posts plan registrations and deliveries with no mention. If
    autolab answered those, the two bots would ping-pong forever."""
    calls = []
    wire_create(monkeypatch, tmp_path, calls)
    client = CreateClient(
        calls, history=[create_message(content="registered PA-1 in Demo Project")]
    )

    zulip_listener.handle_create(client, ASSET_CHANNEL, ASSET_TOPIC)

    assert [call[0] for call in calls] == ["whoami", "history"]


@pytest.mark.parametrize("content", [
    "@**Autolab** what size?",
    "@_**Autolab** silent but still a mention",
    "@**Autolab|11** disambiguated",
])
def test_every_zulip_mention_form_opens_the_gate(content):
    assert zulip_listener.mentions_us(content, "Autolab", 11)


@pytest.mark.parametrize("content", [
    "registered PA-1",
    "@**Forge** not us",
    "Autolab, informally",
    "@**Autolab Two** a different bot",
])
def test_a_non_mention_keeps_the_gate_shut(content):
    assert zulip_listener.mentions_us(content, "Autolab", 11) is False


def test_our_own_last_post_is_never_answered(monkeypatch, tmp_path):
    calls = []
    history = [create_message(sender_id=BOT_ID, name="Autolab", content="@**Autolab** self")]
    wire_create(monkeypatch, tmp_path, calls)
    client = CreateClient(calls)
    client.history = history

    zulip_listener.handle_create(client, ASSET_CHANNEL, ASSET_TOPIC)
    assert [call[0] for call in calls] == ["whoami", "history"]


def test_a_non_asset_create_topic_is_ignored_before_any_lookup(monkeypatch, tmp_path):
    """agforge's own `create-<stamp>-<id>` topics are none of autolab's
    business, and the check costs not even a history read."""
    calls = []
    wire_create(monkeypatch, tmp_path, calls)
    zulip_listener.handle_create(CreateClient(calls), ASSET_CHANNEL, "create-20260816-1200-ab")
    assert calls == []


def test_an_answer_run_that_wrote_nothing_posts_nothing(monkeypatch, tmp_path):
    calls = []
    wire_create(monkeypatch, tmp_path, calls, answer=None)
    with pytest.raises(zulip_listener.ListenerError):
        zulip_listener.handle_create(CreateClient(calls), ASSET_CHANNEL, ASSET_TOPIC)
    assert not any(call[0] == "write" for call in calls)


def test_each_answer_cuts_a_new_generation(monkeypatch, tmp_path):
    calls = []
    wire_create(monkeypatch, tmp_path, calls)
    zulip_listener.handle_create(CreateClient(calls), ASSET_CHANNEL, ASSET_TOPIC)
    zulip_listener.handle_create(CreateClient(calls), ASSET_CHANNEL, ASSET_TOPIC)
    assert answer_dir(tmp_path, 1).is_dir()
    assert answer_dir(tmp_path, 2).is_dir()


def test_the_answer_prompt_points_at_the_three_files(monkeypatch, tmp_path):
    guides = tmp_path / "guides"
    (guides / "create_answer_superdirector").mkdir(parents=True)
    (guides / "create_answer_superdirector" / "guide.md").write_text("ANSWER GUIDE")
    monkeypatch.setattr(zulip_listener, "GUIDES", guides)

    prompt = zulip_listener.answer_prompt(tmp_path / "answer")
    for name in ("chatlog.md", "plan.md", "task.md", "answer.md"):
        assert name in prompt
    assert str(tmp_path / "answer") in prompt
    assert prompt.endswith("ANSWER GUIDE")


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
