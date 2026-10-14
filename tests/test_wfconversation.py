"""The whole Zulip conversation of a run with the real editor CLI, the real
intake and notifier transports, the executor loop with a stub agent, and one
fake Zulip that both reads and posts (workflow_editor p6 step 7): a request
and its answer come from Zulip; the question goes to the request topic and
progress to the run topic; a Zulip outage and a lost post answer keep every
guarantee in both destinations — nothing lost, nothing posted twice."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest
from agag.zulip import ZulipError, ZulipRejected, ZulipTimeout

from agautolab.wfexec import Executor, Settings
from agautolab.wfintake import Intake
from agautolab.wfnotify import Notifier
from test_wfexec import area, node_available, until, wfe  # noqa: F401 - the shared fixture

pytestmark = pytest.mark.skipif(not node_available, reason="node or the workflow editor package is not available")

STREAM, PERSON, BOT = 401, 8, 11
WORKFLOW = """schema: ag.workflow.v2
documentation: {policy: manual}
id: make
name: Make
intent: |
  Agree on a scope, then make it.
execution: {agent: maker}
intake: {zulip: requests}
repositories:
  root: {path: ., access: editable}
nodes:
  scope:
    type: talk
    description: Agree on the scope with the person.
  build:
    type: do
    description: Build what was agreed.
edges:
  - {from: scope, to: build}
"""
STUB = textwrap.dedent('''
    import json, os, subprocess, sys
    wfe = os.environ["WFE_BIN"]
    RUN = "make/run-001"
    def run(*a):
        p = subprocess.run([wfe, "run", *a, "--json"], capture_output=True, text=True)
        if p.returncode: print("refused:", a, p.stdout, p.stderr, file=sys.stderr); sys.exit(3)
        return p
    rec = json.loads(run("show", RUN).stdout)["record"]
    if rec["nodes"]["scope"]["state"] == "pending":
        run("start", RUN, "scope")
        run("ask", RUN, "scope", "--question", "Small or large?", "--to", "Pat")
    else:
        run("take-up", RUN, "q1")
        run("complete", RUN, "scope", "--outcome", "as answered")
        run("start", RUN, "build")
        open("made.txt", "w").write("made\\n")
        run("complete", RUN, "build", "--outcome", "made.txt", "--artifact", "made.txt")
''')


class FakeZulip:
    """One realm: the bot (11) and a person (8); reads and posts. `mode`:
    "down" fails every call; "lost" posts the next message, then raises."""

    def __init__(self):
        self.messages: list[dict] = []
        self.mode = ""
        self.lose_next_in: str | None = None  # the next post to this topic is stored, its answer lost

    def _check(self):
        if self.mode == "down":
            raise ZulipError("connection refused")

    def _store(self, sender: int, topic: str, content: str) -> dict:
        m = {"id": 70000 + len(self.messages), "type": "stream", "stream_id": STREAM, "display_recipient": "wf-demo.make", "subject": topic,
             "sender_id": sender, "sender_full_name": "Pat Person" if sender == PERSON else "autolab", "sender_email": f"u{sender}@x",
             "content": content, "timestamp": int(time.time())}
        self.messages.append(m)
        return m

    def person_posts(self, topic: str, content: str) -> int:
        return self._store(PERSON, topic, content)["id"]

    # -- the reads and writes the transports make --
    def whoami(self):
        return {"user_id": BOT, "full_name": "autolab"}

    # Provisioning of channels other than the one this test routes (since
    # p8/ex2 every project also has the basic workflow freework): created
    # here with ids of their own, never the routed STREAM.
    def channels(self, include_archived=False):
        self._check()
        return [dict(c) for c in getattr(self, "made", [])]

    def create_channel(self, name, description="", principals=None, folder_id=None):
        self._check()
        self.made = getattr(self, "made", []) + [{"name": name, "stream_id": 900 + len(getattr(self, "made", [])), "description": description, "folder_id": folder_id}]

    def realm_owners(self):
        return [PERSON]

    def users(self):
        self._check()
        return [{"user_id": PERSON, "is_bot": False}, {"user_id": BOT, "is_bot": True}]

    def call(self, method, path, params=None, timeout=30):
        self._check()
        assert (method, path) == ("POST", "messages")
        if int(params["to"]) != STREAM:
            raise ZulipRejected("Invalid channel ID")
        m = self._store(BOT, params["topic"], params["content"])
        if self.lose_next_in == params["topic"]:
            self.lose_next_in = None
            raise ZulipTimeout("read timed out")
        return {"id": m["id"]}

    def message(self, message_id, *, strict=False):
        self._check()
        return next((dict(m) for m in self.messages if m["id"] == message_id), None)

    def messages_page(self, narrow, *, anchor="newest", num_before=0, num_after=0, include_anchor=True):
        self._check()
        want = {n["operator"]: n["operand"] for n in narrow}
        rows = sorted((m for m in self.messages if m["stream_id"] == want["channel"] and ("topic" not in want or m["subject"] == want["topic"])
                       and ("sender" not in want or m["sender_id"] == want["sender"])), key=lambda m: m["id"])
        if anchor == "oldest":
            page = rows[:num_after]
            return {"messages": [dict(m) for m in page], "found_oldest": True, "found_newest": len(page) == len(rows)}
        if anchor == "newest":
            page = rows[-num_before:] if num_before else []
            return {"messages": [dict(m) for m in page], "found_oldest": len(page) == len(rows), "found_newest": True}
        before = [m for m in rows if m["id"] < int(anchor) or (include_anchor and m["id"] == int(anchor))]
        after = [m for m in rows if m["id"] > int(anchor) or (include_anchor and m["id"] == int(anchor))]
        if num_before:
            page = before[-num_before:]
            return {"messages": [dict(m) for m in page], "found_oldest": len(page) == len(before), "found_newest": False}
        page = after[:num_after]
        return {"messages": [dict(m) for m in page], "found_oldest": False, "found_newest": len(page) == len(after)}

    def topic(self, name: str) -> list[dict]:
        return [m for m in self.messages if m["subject"] == name and m["sender_id"] == BOT]


@pytest.fixture()
def conversation(area, tmp_path):  # noqa: F811
    project = area["project"]
    (project / "devdocs" / "workflows" / "make.yaml").write_text(WORKFLOW)
    for args in (["add", "-A"], ["commit", "-q", "-m", "make workflow"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (area["area"] / "zulip.json").write_text(json.dumps({
        "enabled": True, "zulipUrl": "https://zulip.example", "viewBase": None, "since": "2026-01-01T00:00:00.000Z", "person": "Pat",
        "intake": {"enabled": True, "since": "2026-01-01T00:00:00.000Z", "projects": {"demo": {}}, "requesters": [{"id": PERSON, "name": "Pat"}]},
    }))
    wfe(area, "notify", "due")
    wfe(area, "notify", "target", "folder:demo", "--zulip-id", "1")
    wfe(area, "notify", "target", "channel:demo/make", "--zulip-id", str(STREAM))
    for n, other in enumerate(("ship", "docs")):  # the fixture's other workflows: provisioned elsewhere
        wfe(area, "notify", "target", f"channel:demo/{other}", "--zulip-id", str(STREAM + 10 + n))
    (area["area"] / "conv_stub.py").write_text(STUB)
    fake = FakeZulip()
    return {
        **area, "fake": fake,
        "intake": Intake([area["wfe"]], fake, log=print),
        "notifier": Notifier([area["wfe"]], fake, fake, inflight=tmp_path / "inflight.json", log=print),
        "executor": Executor(Settings(wfe=[area["wfe"]], agent_command=[sys.executable, str(area["area"] / "conv_stub.py")]), log=print),
    }


def markers(fake: FakeZulip, topic: str) -> list[str]:
    return [m["content"].splitlines()[-1] for m in fake.topic(topic)]


def due_now(a) -> None:
    import sqlite3
    with sqlite3.connect(a["area"] / ".local" / "notify" / "notify.sqlite") as db:
        db.execute("UPDATE deliveries SET next_try = '2000-01-01T00:00:00.000Z' WHERE state = 'failed'")
        db.execute("UPDATE outbox SET created = '2000-01-01T00:00:00.000Z'")


def test_a_run_conducted_in_zulip_through_an_outage_and_a_lost_answer(conversation):
    c = conversation
    fake, intake, notifier, ex = c["fake"], c["intake"], c["notifier"], c["executor"]
    origin = fake.person_posts("request-thing", "Please make a thing.")
    # Zulip is down when the transport first looks: nothing is lost.
    fake.mode = "down"
    assert "connection refused" in intake.pass_once()["error"]
    notifier.pass_once()
    assert wfe(c, "run", "list")["runs"] == []
    fake.mode = ""
    intake.pass_once()
    assert [r["ref"] for r in wfe(c, "run", "list")["runs"]] == ["make/run-001"]
    # The agent asks. The first post to the request topic loses its answer.
    assert ex.tick() == "launched"
    until(ex, "finished")
    fake.lose_next_in = "request-thing"
    notifier.pass_once()  # accepted posted (answer lost), started posted; the request topic waits
    due_now(c)
    for _ in range(2):
        notifier.pass_once()  # accepted found by its marker, not posted again; then the question
    request_markers = markers(fake, "request-thing")
    assert sum(m.endswith(":accepted@request") for m in request_markers) == 1, request_markers
    assert sum(":question@request" in m for m in request_markers) == 1, request_markers
    assert request_markers.index(next(m for m in request_markers if ":accepted@" in m)) < request_markers.index(next(m for m in request_markers if ":question@" in m)), "order kept"
    assert any(":started" in m for m in markers(fake, "run-001"))
    assert not any("question" in m for m in markers(fake, "run-001")), "questions are not in the run topic"
    question = next(m for m in fake.topic("request-thing") if ":question@request" in m["content"])
    assert "reply in this topic" in question["content"]
    # The person replies in Zulip after the question; it is their answer.
    reply = fake.person_posts("request-thing", "Small, please.")
    intake.pass_once()
    rec = wfe(c, "run", "show", "make/run-001")["record"]
    assert rec["questions"]["q1"]["answers"][0]["source"]["message"] == reply
    assert rec["questions"]["q1"]["answers"][0]["from"] == "Pat"
    # Resumed through the existing lifecycle, then completed.
    assert ex.tick() == "launched"
    until(ex, "finished")
    rec = wfe(c, "run", "show", "make/run-001")["record"]
    assert rec["execution"]["state"] == "completed"
    # Another outage while there is news: delivered once afterwards.
    fake.mode = "down"
    notifier.pass_once()
    fake.mode = ""
    due_now(c)
    for _ in range(3):
        notifier.pass_once()
    for topic in ("request-thing", "run-001"):
        ms = markers(fake, topic)
        tokens = [t for m in ms for t in m.split(" ")[2:]]
        assert len(tokens) == len(set(tokens)), f"nothing posted twice in {topic}: {ms}"
    assert any(":answered@request" in m for m in markers(fake, "request-thing")), "the answer's receipt is told in the request topic"
    done_request = next(m for m in fake.topic("request-thing") if "Execution completed" in m["content"])
    assert "a new request needs a new request topic" in done_request["content"]
    assert any("Execution completed" in m["content"] for m in fake.topic("run-001"))
    # After completion a post is kept, told and changes nothing.
    fake.person_posts("request-thing", "and another thing")
    intake.pass_once()
    notifier.pass_once()
    assert any("This request is complete" in m["content"] for m in fake.topic("request-thing"))
    assert [r["ref"] for r in wfe(c, "run", "list")["runs"]] == ["make/run-001"]
    assert all(not m["content"].startswith("@") and "@**" not in m["content"] for m in fake.messages if m["sender_id"] == BOT), "no mentions"
    assert origin < reply
