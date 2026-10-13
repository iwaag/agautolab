"""agautolab.wfintake against the real workflow editor CLI and a fake Zulip
(workflow_editor p6 step 4): only routed channels and authorized people
start work; repeated passes and a restarted transport store nothing twice;
posts made while it was down are read through the cursor; an outage stays
visible and loses nothing; an instruction to a running node changes no node."""
from __future__ import annotations

import json
import subprocess

import pytest
from agag.zulip import ZulipError

from agautolab.wfintake import Intake
from test_wfexec import area, node_available, wfe  # noqa: F401 - the shared fixture

pytestmark = pytest.mark.skipif(not node_available, reason="node or the workflow editor package is not available")

STREAM, OTHER_STREAM = 301, 302
PERSON, STRANGER, BOT = 8, 9, 11
WORKFLOW = """schema: ag.workflow.v2
documentation: {policy: manual}
id: make
name: Make
intent: |
  Make what the person asks.
execution: {agent: maker}
intake: {zulip: requests}
repositories:
  root: {path: ., access: editable}
nodes:
  build:
    type: do
    description: Build it.
"""


class FakeZulip:
    """The reads wfintake makes. `down` fails every call."""

    def __init__(self):
        self.messages: list[dict] = []
        self.down = False
        self.users_calls = 0

    def post(self, stream: int, topic: str, sender: int, content: str) -> int:
        mid = 9000 + len(self.messages)
        names = {PERSON: "Pat Person", STRANGER: "Stranger", BOT: "autolab-bot"}
        self.messages.append({"id": mid, "stream_id": stream, "subject": topic, "sender_id": sender, "sender_full_name": names[sender],
                              "sender_email": f"u{sender}@x", "content": content, "timestamp": 1_790_000_000 + len(self.messages)})
        return mid

    def users(self):
        if self.down:
            raise ZulipError("connection refused")
        self.users_calls += 1
        return [{"user_id": PERSON, "is_bot": False}, {"user_id": STRANGER, "is_bot": False}, {"user_id": BOT, "is_bot": True}]

    def messages_page(self, narrow, *, anchor="newest", num_before=0, num_after=0, include_anchor=True):
        if self.down:
            raise ZulipError("connection refused")
        want = {n["operator"]: n["operand"] for n in narrow}
        rows = sorted((m for m in self.messages if m["stream_id"] == want["channel"] and ("topic" not in want or m["subject"] == want["topic"])), key=lambda m: m["id"])
        if anchor == "oldest":
            page = rows[:num_after]
            return {"messages": [dict(m) for m in page], "found_oldest": True, "found_newest": len(page) == len(rows)}
        after = [m for m in rows if m["id"] > int(anchor) or (include_anchor and m["id"] == int(anchor))]
        page = after[:num_after]
        return {"messages": [dict(m) for m in page], "found_oldest": False, "found_newest": len(page) == len(after)}


@pytest.fixture()
def routed(area):  # noqa: F811
    project = area["project"]
    (project / "devdocs" / "workflows" / "make.yaml").write_text(WORKFLOW)
    for args in (["add", "-A"], ["commit", "-q", "-m", "make workflow"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (area["area"] / "zulip.json").write_text(json.dumps({
        "enabled": True, "zulipUrl": "https://zulip.example", "viewBase": None, "since": "2026-01-01T00:00:00.000Z", "person": "Pat",
        "intake": {"enabled": True, "since": "2026-01-01T00:00:00.000Z", "projects": {"demo": {}}, "requesters": [{"id": PERSON, "name": "Pat"}]},
    }))
    wfe(area, "notify", "due")  # the notifier records its targets
    wfe(area, "notify", "target", "channel:demo/make", "--zulip-id", str(STREAM))
    return area


def runs(a) -> list[str]:
    return [r["ref"] for r in wfe(a, "run", "list")["runs"]]


def test_only_routed_channels_and_requesters_start_work_once(routed):
    fake = FakeZulip()
    fake.post(STREAM, "request-thing", BOT, "a bot speaks first")
    fake.post(STREAM, "request-thing", STRANGER, "do my thing instead")
    origin = fake.post(STREAM, "request-thing", PERSON, "Please make a **thing**.")
    fake.post(STREAM, "run-001", PERSON, "is it done?")
    fake.post(STREAM, "chatter", PERSON, "hello")
    fake.post(OTHER_STREAM, "request-elsewhere", PERSON, "not a routed channel")
    intake = Intake([routed["wfe"]], fake, log=print)
    first = intake.pass_once()
    assert first["received"] == 5, "the routed channel only"
    assert runs(routed) == ["make/run-001"]
    rec = wfe(routed, "run", "show", "make/run-001")["record"]
    assert rec["input"]["source"]["message"] == origin
    assert rec["executor"] == {"name": "maker", "backend": None, "host": "autolab"}
    assert (routed["project"] / ".local/wfe/runs/make/run-001/tree" / "devdocs" / "runs" / "make" / "run-001" / "braindump.md").read_text() == "Please make a **thing**."
    # Again, and from a restarted transport: nothing new is stored or applied.
    assert intake.pass_once()["received"] == 0
    assert Intake([routed["wfe"]], fake, log=print).pass_once()["received"] == 0
    assert runs(routed) == ["make/run-001"]
    status = wfe(routed, "intake", "status")
    assert status["counts"] == {"done": 5}
    assert status["channels"] == [{"streamId": STREAM, "project": "demo", "workflow": "make", "cursor": max(m["id"] for m in fake.messages if m["stream_id"] == STREAM)}]
    assert status["transport"]["ok"] == 1
    jobs = wfe(routed, "exec", "status", "make/run-001")["jobs"]
    assert [j["kind"] for j in jobs] == ["start"], "queued once"


def test_posts_made_while_down_are_read_through_the_cursor(routed):
    fake = FakeZulip()
    intake = Intake([routed["wfe"]], fake, log=print)
    intake.pass_once()
    # Downtime: more than a page of other traffic, then a request.
    for i in range(130):
        fake.post(STREAM, "request-noise", BOT, f"noise {i}")
    origin = fake.post(STREAM, "request-later", PERSON, "Make it later.")
    restarted = Intake([routed["wfe"]], fake, log=print)
    assert restarted.pass_once()["received"] == 131
    assert runs(routed) == ["make/run-001"]
    assert wfe(routed, "run", "show", "make/run-001")["record"]["input"]["source"]["message"] == origin


def test_an_outage_is_visible_and_loses_nothing(routed):
    fake = FakeZulip()
    fake.post(STREAM, "request-thing", PERSON, "Please make a thing.")
    fake.down = True
    intake = Intake([routed["wfe"]], fake, log=print)
    result = intake.pass_once()
    assert "connection refused" in result["error"]
    status = wfe(routed, "intake", "status")
    assert status["transport"]["ok"] == 0
    assert any("last pass failed" in p for p in status["problems"])
    assert status["channels"][0]["cursor"] == 0, "nothing was stored, the cursor did not move"
    assert runs(routed) == []
    fake.down = False
    assert intake.pass_once()["received"] == 1
    assert runs(routed) == ["make/run-001"]
    assert wfe(routed, "intake", "status")["transport"]["ok"] == 1


def test_an_instruction_to_a_running_node_changes_no_node(routed):
    fake = FakeZulip()
    fake.post(STREAM, "request-thing", PERSON, "Please make a thing.")
    intake = Intake([routed["wfe"]], fake, log=print)
    intake.pass_once()
    wfe(routed, "run", "start", "make/run-001", "build", "--by", "maker")
    fake.post(STREAM, "request-thing", PERSON, "Make it bigger, please.")
    intake.pass_once()
    rec = wfe(routed, "run", "show", "make/run-001")["record"]
    assert rec["nodes"]["build"]["state"] == "running"
    assert rec["instructions"]["i1"]["text"] == "Make it bigger, please."
    assert rec["instructions"]["i1"]["from"] == "Pat"
    assert rec["instructions"]["i1"]["during"] == ["build"]
    assert rec["instructions"]["i1"]["ack"] is None
    assert rec["history"][-1]["via"] == "zulip"
