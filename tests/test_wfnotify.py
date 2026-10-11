"""agautolab.wfnotify against the real workflow editor CLI and a fake Zulip
(workflow_editor p5 step 4): the folder/channel/topic hierarchy, delivery
during a human wait, restart without duplicates, a response lost after the
post, a crash with a send in flight, a long outage while the work goes on,
a channel name taken by someone else, a renamed topic, and separation from
the legacy listeners' routing."""
from __future__ import annotations

import json

import pytest
from agag.agent import topic_filter
from agag.listen import mentions_bot
from agag.zulip import ZulipError, ZulipRejected, ZulipTimeout

from agautolab.instance import SPEC
from agautolab.wfnotify import Notifier
from test_wfexec import area, executor, node_available, until, wfe  # noqa: F401 - the shared fixture

pytestmark = pytest.mark.skipif(not node_available, reason="node or the workflow editor package is not available")


class FakeZulip:
    """The calls wfnotify makes, kept in memory. `mode`: "" works; "down"
    fails every call; "lost" posts the next message and then raises as if
    the answer never came back."""

    def __init__(self):
        self.folders: list[dict] = []
        self.streams: list[dict] = []
        self.messages: list[dict] = []
        self.mode = ""
        self.posts = 0

    def _check(self):
        if self.mode == "down":
            raise ZulipError("connection refused")

    def whoami(self):
        return {"user_id": 11, "full_name": "autolab-agstudio1"}

    def realm_owners(self):
        self._check()
        return [8]

    def channel_folders(self):
        self._check()
        return [dict(f) for f in self.folders]

    def create_channel_folder(self, name, description=""):
        self._check()
        fid = len(self.folders) + 1
        self.folders.append({"id": fid, "name": name, "description": description})
        return fid

    def channels(self, *, include_archived=False):
        self._check()
        return [dict(s) for s in self.streams if include_archived or not s.get("is_archived")]

    def create_channel(self, name, description, principals, announce=False, folder_id=None):
        self._check()
        self.streams.append({"stream_id": 100 + len(self.streams), "name": name, "description": description, "folder_id": folder_id, "subscribers": list(principals)})
        return {}

    def set_channel_folder(self, stream_id, folder_id):
        next(s for s in self.streams if s["stream_id"] == stream_id)["folder_id"] = folder_id

    def call(self, method, path, params=None, timeout=30):
        self._check()
        assert (method, path) == ("POST", "messages")
        stream = next((s for s in self.streams if s["stream_id"] == int(params["to"])), None)
        if stream is None:
            raise ZulipRejected("Invalid channel ID")
        self.posts += 1
        mid = 5000 + len(self.messages)
        self.messages.append({"id": mid, "type": "stream", "stream_id": stream["stream_id"], "display_recipient": stream["name"],
                              "subject": params["topic"], "content": params["content"], "sender_id": 11})
        if self.mode == "lost":
            self.mode = ""
            raise ZulipTimeout("read timed out")
        return {"id": mid}

    def message(self, message_id, *, strict=False):
        self._check()
        return next((dict(m) for m in self.messages if m["id"] == message_id), None)

    def topic_history(self, channel, topic, num_before=50):
        self._check()
        return [dict(m) for m in self.messages if m["display_recipient"] == channel and m["subject"] == topic][-num_before:]

    def topic(self, name="run-001"):
        return [m for m in self.messages if m["subject"] == name]


@pytest.fixture()
def zulip(area):  # noqa: F811
    (area["area"] / "zulip.json").write_text(json.dumps({
        "enabled": True, "zulipUrl": "https://zulip.example", "viewBase": None,
        "since": "2026-01-01T00:00:00.000Z", "person": "Test Person"}))
    return FakeZulip()


def notifier(a, fake, tmp_path) -> Notifier:
    return Notifier([a["wfe"]], fake, fake, inflight=tmp_path / "inflight.json", log=print)


def provision_only(a, n: Notifier) -> None:
    for _ in range(3):
        for task in wfe(a, "notify", "due")["provision"]:
            n.provision(task)


def request_and_plan(a, monkeypatch, receipt="pytest-notify-0001"):
    monkeypatch.setenv("STUB_MODE", "")
    words = a["area"] / "words.md"
    words.write_text("Please build one thing.")
    wfe(a, "request", "docs", "--braindump", str(words), "--author", "Test Person", "--receipt", receipt, "--workspace", "demo")
    ex = executor(a, "doc_stub")
    assert ex.tick() == "launched"
    until(ex, "finished")
    return ex


def test_hierarchy_human_wait_restart_and_separation(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch)
    n = notifier(area, zulip, tmp_path)
    for _ in range(3):
        n.pass_once()
    assert [f["name"] for f in zulip.folders] == ["wf-demo"], "one folder per project"
    names = {s["name"]: s for s in zulip.streams}
    assert set(names) >= {"wf-demo.docs", "wf-demo.ship"}, "one channel per workflow, filed in the project's folder"
    assert all(s["folder_id"] == 1 and 8 in s["subscribers"] and 11 in s["subscribers"] for s in names.values())
    topic = zulip.topic()
    assert [m["content"].splitlines()[0] for m in topic][0].startswith("**Started** — autolab took up the request")
    question = topic[1]["content"]
    assert question.startswith("**Question for Test Person** — agree to the plan (q1)")
    assert "Plan: plan.md (published at" in question, "the checkpoint was published before the question went out"
    assert "Answer: in agdevworld's run view on the execution host" in question
    assert question.splitlines()[-1] == "ref docs/run-001 #4:question"
    # The person waits; nothing runs; a restarted transport sends nothing twice.
    before = zulip.posts
    for _ in range(2):
        notifier(area, zulip, tmp_path).pass_once()
    assert zulip.posts == before
    # Separation: no mention; no legacy listener owns these topics. autolab's
    # routing is imported; the other agents' sweep prefixes (their own
    # venvs) are the ones their instance modules declare today.
    legacy = ("workplan-", "workrun-", "bmining-", "routinerun-", "front-", "watch-", "assetplan-", "assetrun-")
    for m in zulip.messages:
        assert not m["subject"].startswith(legacy) and not m["display_recipient"].startswith(("pj-", "work-"))
        assert "@**" not in m["content"]
        assert not mentions_bot(m["content"], "autolab-agstudio1")
        assert not topic_filter(SPEC)(m["display_recipient"], m["subject"])
    status = wfe(area, "notify", "status", "docs/run-001", "--workspace", "demo")
    assert status["topic"]["topic"] == "run-001" and status["topic"]["url"].startswith("https://zulip.example/#narrow/channel/")
    assert status["pending"] == 0 and status["failed"] == 0


def test_lost_response_is_reconciled_not_duplicated(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0002")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    zulip.mode = "lost"  # the first message is posted, its answer lost
    n.pass_once()
    assert zulip.posts == 1, "after the lost answer nothing passes the uncertain message"
    outbox = wfe(area, "notify", "outbox")
    lost = [r for r in outbox if r["state"] == "failed"]
    assert lost and lost[0]["uncertain"] == 1
    # The retry looks for the marker first and finds the message.
    wfe_db = area["area"] / ".local" / "notify" / "notify.sqlite"
    import sqlite3
    with sqlite3.connect(wfe_db) as db:
        db.execute("UPDATE outbox SET next_try = '2000-01-01T00:00:00.000Z'")
    n.pass_once()
    n.pass_once()
    assert zulip.posts == 2, "the started message was recorded as sent, not posted again; the question followed"
    assert [m["content"].splitlines()[-1] for m in zulip.topic()] == ["ref docs/run-001 #2:started", "ref docs/run-001 #4:question"]


def test_crash_with_a_send_in_flight(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0003")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    due = wfe(area, "notify", "due")["deliveries"][0]
    # The process posted, then died before recording it.
    zulip.call("POST", "messages", {"type": "stream", "to": due["streamId"], "topic": due["topic"], "content": due["content"]})
    (tmp_path / "inflight.json").write_text(json.dumps({"ids": due["ids"], "marker": due["marker"]}))
    n.pass_once()
    assert [m["content"].splitlines()[-1] for m in zulip.topic()].count(due["marker"]) == 1
    assert not (tmp_path / "inflight.json").exists()


def test_long_outage_keeps_work_and_delivers_in_order(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    ex = request_and_plan(area, monkeypatch, "pytest-notify-0004")
    n = notifier(area, zulip, tmp_path)
    n.pass_once()
    zulip.mode = "down"
    for _ in range(3):
        n.pass_once()  # nothing gets through; nothing breaks
    wfe(area, "run", "answer", "docs/run-001", "q1", "--answer", "Agreed.", "--by", "Test Person")
    assert ex.tick() == "launched"
    until(ex, "finished")
    rec = wfe(area, "run", "show", "docs/run-001")["record"]
    assert rec["execution"]["state"] == "completed", "a Zulip outage does not hold up or fail the work"
    zulip.mode = ""
    import sqlite3
    for _ in range(12):
        with sqlite3.connect(area["area"] / ".local" / "notify" / "notify.sqlite") as db:
            db.execute("UPDATE outbox SET next_try = '2000-01-01T00:00:00.000Z' WHERE state = 'failed'")
        n.pass_once()
    markers = [m["content"].splitlines()[-1] for m in zulip.topic()]
    types = [t for line in markers for t in line.split(" #")[1:]]
    seqs = [int(t.split(":")[0]) for t in types if t[0].isdigit()]
    assert seqs == sorted(seqs), f"per-run order kept: {markers}"
    assert len(types) == len(set(types)), "nothing twice"
    assert any(t.endswith(":completed") for t in types)
    final = next(m["content"] for m in zulip.topic() if ":completed" in m["content"].splitlines()[-1])
    assert "Not accepted yet" in final
    assert wfe(area, "notify", "status", "docs/run-001", "--workspace", "demo")["pending"] == 0


def test_taken_name_is_a_problem_not_an_adoption(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    zulip.streams.append({"stream_id": 900, "name": "wf-demo.docs", "description": "somebody's own channel", "folder_id": None, "subscribers": [8]})
    request_and_plan(area, monkeypatch, "pytest-notify-0005")
    n = notifier(area, zulip, tmp_path)
    for _ in range(3):
        n.pass_once()
    assert zulip.topic() == [], "nothing posted into a channel this integration does not own"
    status = wfe(area, "notify", "status")
    target = next(t for t in status["targets"] if t["key"] == "channel:demo/docs")
    assert target["state"] == "problem" and "taken by channel #900" in target["problem"]
    # The repair: the person renames their channel; reset; the next pass provisions ours.
    zulip.streams[0]["name"] = "their-channel"
    wfe(area, "notify", "reset", "channel:demo/docs")
    for _ in range(3):
        n.pass_once()
    assert len(zulip.topic()) == 2


def test_a_renamed_topic_is_followed(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    ex = request_and_plan(area, monkeypatch, "pytest-notify-0006")
    n = notifier(area, zulip, tmp_path)
    for _ in range(3):
        n.pass_once()
    for m in zulip.messages:
        m["subject"] = "run-001 heavy warrior"  # renamed by a person
    wfe(area, "run", "answer", "docs/run-001", "q1", "--answer", "Agreed.", "--by", "Test Person")
    n.pass_once()
    assert zulip.messages[-1]["subject"] == "run-001 heavy warrior"
    assert zulip.topic("run-001") == []
