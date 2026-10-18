"""agautolab.wfnotify against the real workflow editor CLI and a fake Zulip
(workflow_editor p5 step 4): the folder/channel/topic hierarchy, delivery
during a human wait, restart without duplicates, a response lost after the
post, a crash with a send in flight, a long outage while the work goes on,
a channel name taken by someone else, a renamed topic, and separation from
the legacy listeners' routing."""
from __future__ import annotations

import json
import sqlite3
import time

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
    the answer never came back. `search_fails` makes history reads fail;
    `unreadable` holds stream ids the bot can no longer read; `on_post` runs
    after a message is stored (fault injection)."""

    def __init__(self):
        self.folders: list[dict] = []
        self.streams: list[dict] = []
        self.messages: list[dict] = []
        self.mode = ""
        self.posts = 0
        self.search_fails = False
        self.unreadable: set[int] = set()
        self.on_post = None
        self.pages = 0

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
                              "subject": params["topic"], "content": params["content"], "sender_id": 11, "timestamp": time.time()})
        if self.on_post:
            self.on_post(self.messages[-1])
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

    def messages_page(self, narrow, *, anchor="newest", num_before=0, num_after=0, include_anchor=True):
        self._check()
        self.pages += 1
        if self.search_fails:
            raise ZulipError("search timed out")
        want = {n["operator"]: n["operand"] for n in narrow}
        if want.get("channel") in self.unreadable:
            raise ZulipRejected("Invalid channel ID")
        rows = [m for m in self.messages if m["stream_id"] == want.get("channel") and m["subject"] == want.get("topic")
                and ("sender" not in want or m["sender_id"] == want["sender"])]
        rows.sort(key=lambda m: m["id"])
        if anchor != "newest":
            rows = [m for m in rows if m["id"] < int(anchor) or (include_anchor and m["id"] == int(anchor))]
        page = rows[-num_before:] if num_before else []
        return {"messages": [dict(m) for m in page], "found_oldest": len(page) == len(rows), "found_newest": anchor == "newest"}

    def topic(self, name="run-001"):
        return [m for m in self.messages if m["subject"] == name]


@pytest.fixture()
def zulip(area):  # noqa: F811
    (area["area"] / "zulip.json").write_text(json.dumps({
        "enabled": True, "zulipUrl": "https://zulip.example", "viewBase": None,
        "since": "2026-01-01T00:00:00.000Z", "person": "Test Person"}))
    return FakeZulip()


def notifier(a, fake, tmp_path, pid=None) -> Notifier:
    return Notifier([a["wfe"]], fake, fake, inflight=tmp_path / "inflight.json", log=print, pid=pid)


def notify_db(a):
    return sqlite3.connect(a["area"] / ".local" / "notify" / "notify.sqlite")


def retry_now(a):
    """The backoff has passed: failed deliveries are due again."""
    with notify_db(a) as db:
        db.execute("UPDATE deliveries SET next_try = '2000-01-01T00:00:00.000Z' WHERE state = 'failed'")


def markers(fake, topic="run-001"):
    return [m["content"].splitlines()[-1] for m in fake.topic(topic)]


def posted_events(fake, topic="run-001"):
    """Every event token in the topic's markers, in order, repeats kept."""
    return [t for line in markers(fake, topic) for t in line.split(" ")[2:]]


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
    assert question.splitlines()[-1] == "ref docs/run-001 #5:question"
    # The person waits; nothing runs; a restarted transport sends nothing twice.
    before = zulip.posts
    for _ in range(2):
        notifier(area, zulip, tmp_path).pass_once()
    assert zulip.posts == before
    # Separation: no mention; no listener owns these topics. autolab sweeps
    # nothing; the other agents' sweep prefixes (their own venvs) are the
    # ones their instance modules declare today.
    legacy = ("routinerun-", "front-", "watch-")
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
    lost = [d for d in wfe(area, "notify", "deliveries") if d["state"] == "failed"]
    assert lost and lost[0]["uncertain"] == 1
    # The retry looks for the marker first and finds the message.
    retry_now(area)
    n.pass_once()
    n.pass_once()
    assert zulip.posts == 2, "the started message was recorded as sent, not posted again; the question followed"
    assert [m["content"].splitlines()[-1] for m in zulip.topic()] == ["ref docs/run-001 #2:started", "ref docs/run-001 #5:question"]


def test_restart_between_post_and_acknowledgment(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0003")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    # The process claimed, began and posted, then died before recording it.
    d = wfe(area, "notify", "claim", "--sender", n.owner, "--pid", str(n.pid))["deliveries"][0]
    wfe(area, "notify", "begin", str(d["delivery"]), "--claim", d["claim"], *n._dest_args(d["destination"]))
    zulip.call("POST", "messages", {"type": "stream", "to": d["destination"]["streamId"], "topic": d["destination"]["topic"], "content": d["content"]})
    restarted = notifier(area, zulip, tmp_path)
    restarted.pass_once()
    assert markers(zulip).count(d["marker"]) == 1, "the frozen delivery was found and acknowledged, not posted again"
    rows = {x["id"]: x for x in wfe(area, "notify", "deliveries")}
    assert rows[d["delivery"]]["state"] == "sent" and rows[d["delivery"]]["message_id"] == zulip.messages[0]["id"]
    assert posted_events(zulip) == ["#2:started", "#5:question"]


def test_legacy_inflight_file_is_retired_into_the_store(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0007")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    # An old transport posted #2 and died; only its inflight.json knows.
    run = "demo/docs/run-001"
    stream = next(s["stream_id"] for s in zulip.streams if s["name"] == "wf-demo.docs")
    zulip.call("POST", "messages", {"type": "stream", "to": stream, "topic": "run-001", "content": "**Started** — (old transport)\nref docs/run-001 #2:started"})
    (tmp_path / "inflight.json").write_text(json.dumps({"ids": [f"{run}#2:started"], "marker": "ref docs/run-001 #2:started"}))
    n.pass_once()
    assert not (tmp_path / "inflight.json").exists()
    assert list(tmp_path.glob("inflight.json.retired-*")), "the evidence is kept, retired"
    assert posted_events(zulip) == ["#2:started", "#5:question"], "the in-flight event was found, not sent twice"
    legacy = [d for d in wfe(area, "notify", "deliveries") if d["kind"] == "legacy"]
    assert legacy and legacy[0]["state"] == "resolved"


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
    for _ in range(12):
        retry_now(area)
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


# ---- p5/ex1: frozen deliveries, claims and reconciliation --------------------------------


def answer_and_lose_the_next_post(area, zulip, n):
    """q1 is answered; the `answered` notice reaches Zulip but its answer is
    lost. Returns the lost delivery as the store records it."""
    for _ in range(3):
        n.pass_once()
    assert posted_events(zulip) == ["#2:started", "#5:question"]
    wfe(area, "run", "answer", "docs/run-001", "q1", "--answer", "Agreed.", "--by", "Test Person")
    zulip.mode = "lost"
    n.pass_once()
    lost = [d for d in wfe(area, "notify", "deliveries") if d["state"] == "failed"]
    assert len(lost) == 1 and lost[0]["uncertain"] == 1
    return lost[0]


def test_growing_batch_is_not_posted_twice(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    """p5 review finding: a routine event is posted, its answer lost; more
    routine events arrive before the retry. The retry is the same frozen
    message, found by its marker; the later events follow it."""
    ex = request_and_plan(area, monkeypatch, "pytest-notify-0101")
    n = notifier(area, zulip, tmp_path)
    lost = answer_and_lose_the_next_post(area, zulip, n)
    first = zulip.messages[-1]
    assert ex.tick() == "launched"
    until(ex, "finished")  # the run moves on: #15:reported, #17:completed …
    for _ in range(6):
        retry_now(area)
        n.pass_once()
    events = posted_events(zulip)
    assert events.count("#7:answered") == 1, events
    assert len(events) == len(set(events)), f"nothing twice: {markers(zulip)}"
    assert events.index("#7:answered") < events.index("#15:reported") < events.index("#17:completed"), "later events follow, in order"
    rows = {d["id"]: d for d in wfe(area, "notify", "deliveries")}
    assert rows[lost["id"]]["state"] == "sent" and rows[lost["id"]]["message_id"] == first["id"], "the existing post was acknowledged"
    assert rows[lost["id"]]["content"] == first["content"], "membership and text stayed as frozen"
    assert zulip.posts == len(zulip.messages)


def test_restart_after_claim_before_post(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0102")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    # (a) Claimed, then the process died: nothing was begun.
    d = wfe(area, "notify", "claim", "--sender", n.owner, "--pid", str(n.pid))["deliveries"][0]
    notifier(area, zulip, tmp_path).pass_once()
    assert markers(zulip)[0] == d["marker"]
    rows = {x["id"]: x for x in wfe(area, "notify", "deliveries")}
    assert rows[d["delivery"]]["state"] == "sent" and rows[d["delivery"]]["attempts"] == 1


def test_restart_after_begin_before_post(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0103")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    # (b) Begun, then the process died before the POST left: uncertain.
    d = wfe(area, "notify", "claim", "--sender", n.owner, "--pid", str(n.pid))["deliveries"][0]
    wfe(area, "notify", "begin", str(d["delivery"]), "--claim", d["claim"], *n._dest_args(d["destination"]))
    restarted = notifier(area, zulip, tmp_path)
    restarted.pass_once()
    rows = {x["id"]: x for x in wfe(area, "notify", "deliveries")}
    first = rows[d["delivery"]]
    assert first["state"] == "sent" and first["attempts"] == 2, "the same delivery, begun again"
    assert first["checked"].startswith("absent from #"), f"absence was established before the retry: {first['checked']}"
    assert zulip.topic()[0]["content"] == d["content"], "the exact frozen payload"
    assert posted_events(zulip) == ["#2:started", "#5:question"]


def test_acknowledgment_without_the_database_lock(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    """The POST succeeds; recording it cannot get the write lock (held past
    the busy timeout). The transport does not post again; the next pass
    finds the message."""
    request_and_plan(area, monkeypatch, "pytest-notify-0104")
    n = notifier(area, zulip, tmp_path)
    provision_only(area, n)
    holder = {}

    def lock_the_store(message):
        db = sqlite3.connect(area["area"] / ".local" / "notify" / "notify.sqlite", isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        holder["db"] = db
        zulip.on_post = None

    zulip.on_post = lock_the_store
    result = n.pass_once()
    assert result["failed"] == 1 and zulip.posts == 1, "posted once; recording failed; nothing posted again"
    holder["db"].execute("ROLLBACK")
    holder["db"].close()
    state = {x["id"]: x for x in wfe(area, "notify", "deliveries")}
    assert list(state.values())[0]["state"] == "sending", "still begun: it may have been posted"
    for _ in range(2):
        n.pass_once()
    assert posted_events(zulip) == ["#2:started", "#5:question"], "found by its marker, then the next message"
    assert zulip.posts == 2


def test_two_notifiers_only_the_owner_sends(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    import subprocess
    import sys
    request_and_plan(area, monkeypatch, "pytest-notify-0105")
    owner_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        a = notifier(area, zulip, tmp_path, pid=owner_process.pid)
        provision_only(area, a)
        # A claims and begins, and is then stuck (its request may be in flight).
        d = wfe(area, "notify", "claim", "--sender", a.owner, "--pid", str(a.pid))["deliveries"][0]
        wfe(area, "notify", "begin", str(d["delivery"]), "--claim", d["claim"], *a._dest_args(d["destination"]))
        b = notifier(area, zulip, tmp_path)
        result = b.pass_once()
        assert "refused" in result and f"pid {owner_process.pid}" in result["refused"]
        assert zulip.posts == 0, "only the owner sends"
    finally:
        owner_process.kill()
        owner_process.wait()
    # A is gone: B takes over; A's begun delivery is reconciled first.
    b.pass_once()
    assert posted_events(zulip) == ["#2:started", "#5:question"]
    rows = {x["id"]: x for x in wfe(area, "notify", "deliveries")}
    assert rows[d["delivery"]]["attempts"] == 2 and rows[d["delivery"]]["checked"].startswith("absent from")
    stale = subprocess.run([area["wfe"], "notify", "sent", str(d["delivery"]), "--claim", d["claim"], "--message", "1",
                            "--stream", "1", "--channel", "x", "--topic", "y", "--json"], capture_output=True, text=True)
    assert stale.returncode != 0 and "already recorded" in stale.stdout + stale.stderr, "A's late result is refused"


# ---- p5/ex1 step 3: reconciliation across history, places and failures ------------------


def test_marker_older_than_the_last_hundred_messages(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0201")
    n = notifier(area, zulip, tmp_path)
    lost = answer_and_lose_the_next_post(area, zulip, n)
    stream = zulip.messages[-1]["stream_id"]
    # The topic grows by 450 more of the bot's own messages (and a person's) before the retry.
    for i in range(450):
        zulip.messages.append({"id": 6000 + i, "type": "stream", "stream_id": stream, "display_recipient": "wf-demo.docs", "subject": "run-001",
                               "content": f"other\nref docs/run-999 #{i}:x", "sender_id": 11 if i % 3 else 8, "timestamp": time.time()})
    zulip.pages = 0
    retry_now(area)
    n.pass_once()
    assert posted_events(zulip).count("#7:answered") == 1
    assert zulip.pages >= 2, "the search paged back past the newest window"
    assert {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]["state"] == "sent"


def test_search_that_cannot_reach_far_enough_stays_uncertain(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    import agautolab.wfnotify as wfnotify
    request_and_plan(area, monkeypatch, "pytest-notify-0202")
    n = notifier(area, zulip, tmp_path)
    lost = answer_and_lose_the_next_post(area, zulip, n)
    stream = zulip.messages[-1]["stream_id"]
    for i in range(30):
        zulip.messages.append({"id": 6000 + i, "type": "stream", "stream_id": stream, "display_recipient": "wf-demo.docs", "subject": "run-001",
                               "content": f"ref docs/run-999 #{i}:x", "sender_id": 11, "timestamp": time.time()})
    monkeypatch.setattr(wfnotify, "SEARCH_PAGE", 10)
    monkeypatch.setattr(wfnotify, "SEARCH_PAGES_MAX", 2)
    retry_now(area)
    before = zulip.posts
    n.pass_once()
    assert zulip.posts == before, "no repost when absence is not established"
    row = {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]
    assert row["state"] == "failed" and row["uncertain"] == 1 and "history not covered" in row["last_error"]
    status = wfe(area, "notify", "status")
    assert any(f"delivery {lost['id']} may have been posted" in p for p in status["problems"]), status["problems"]


def test_moved_topic_is_searched_where_it_went(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0203")
    n = notifier(area, zulip, tmp_path)
    lost = answer_and_lose_the_next_post(area, zulip, n)
    for m in zulip.messages:
        m["subject"] = "run-001 (renamed)"  # a person renamed the topic, the lost post included
    retry_now(area)
    n.pass_once()
    assert posted_events(zulip, "run-001 (renamed)").count("#7:answered") == 1
    assert zulip.topic("run-001") == []
    assert {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]["state"] == "sent"
    with notify_db(area) as db:
        moves = db.execute("SELECT topic, reason FROM relocations WHERE run_key = 'demo/docs/run-001'").fetchall()
    assert moves == [("run-001", "left for #%d › run-001 (renamed)" % zulip.messages[0]["stream_id"])], "the old place is kept as history"


def test_search_failure_is_not_absence(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0204")
    n = notifier(area, zulip, tmp_path)
    lost = answer_and_lose_the_next_post(area, zulip, n)
    zulip.search_fails = True
    for _ in range(3):
        retry_now(area)
        n.pass_once()
    assert posted_events(zulip).count("#7:answered") == 1
    row = {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]
    assert row["uncertain"] == 1 and "cannot establish whether it was posted" in row["last_error"]
    zulip.search_fails = False
    retry_now(area)
    n.pass_once()
    assert posted_events(zulip).count("#7:answered") == 1
    assert {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]["state"] == "sent"


def test_lost_access_and_reset_do_not_turn_uncertainty_into_a_fresh_send(area, zulip, tmp_path, monkeypatch):  # noqa: F811
    request_and_plan(area, monkeypatch, "pytest-notify-0205")
    n = notifier(area, zulip, tmp_path)
    lost = answer_and_lose_the_next_post(area, zulip, n)
    old_stream = zulip.messages[-1]["stream_id"]
    zulip.unreadable.add(old_stream)  # the bot lost access to the channel
    retry_now(area)
    n.pass_once()
    row = {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]
    assert row["uncertain"] == 1 and "Invalid channel ID" in row["last_error"]
    # A person resets the channel; ours is provisioned again under a new id.
    zulip.streams[[s["stream_id"] for s in zulip.streams].index(old_stream)]["name"] = "wf-demo.docs-old"
    notes = wfe(area, "notify", "reset", "channel:demo/docs")["notes"]
    assert any(f"delivery {lost['id']}" in x and "still looked for" in x for x in notes), notes
    for _ in range(3):
        retry_now(area)
        n.pass_once()
    assert zulip.topic("run-001") and all(m["stream_id"] == old_stream for m in zulip.topic("run-001")), "nothing posted to the new channel while the old place cannot be searched"
    status = wfe(area, "notify", "status")
    assert any(f"delivery {lost['id']} may have been posted" in p for p in status["problems"])
    # The person checked the old channel and found nothing: the same payload goes out, once.
    wfe(area, "notify", "resolve", str(lost["id"]), "--not-posted", "--by", "Test Person")
    n.pass_once()
    new_stream = next(s["stream_id"] for s in zulip.streams if s["name"] == "wf-demo.docs")
    resent = [m for m in zulip.messages if m["stream_id"] == new_stream]
    assert [m["content"] for m in resent][:1] == [lost["content"]]
    assert {d["id"]: d for d in wfe(area, "notify", "deliveries")}[lost["id"]]["checked"] == "resolved by Test Person: not posted"


# ---- legacy state, made by the pre-p5/ex1 editor and transport ---------------------------

OLD_EDITOR, OLD_TRANSPORT = "32498e9", "daeaada"


@pytest.fixture()
def old_release(area, tmp_path):  # noqa: F811
    """The released pre-p5/ex1 editor CLI (from agdevworld's history, with the
    package's node_modules) and transport (from agautolab's history)."""
    import importlib.util
    import subprocess
    import sys
    from test_wfexec import PACKAGE, NODE
    agdevworld = PACKAGE.parent
    root = tmp_path / "old"
    root.mkdir()
    archive = subprocess.run(f"git -C '{agdevworld}' archive {OLD_EDITOR} workflow_editor | tar -x -C '{root}'", shell=True, capture_output=True, text=True)
    source = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[1]), "show", f"{OLD_TRANSPORT}:src/agautolab/wfnotify.py"], capture_output=True, text=True)
    if archive.returncode or source.returncode:
        pytest.skip("the pre-p5/ex1 releases are not in this checkout's history")
    (root / "workflow_editor" / "node_modules").symlink_to(PACKAGE / "node_modules")
    launcher = area["area"] / "bin" / "wfe-old"
    launcher.write_text(f'#!/bin/sh\nWFE_REGISTRY="{area["area"] / "registry.json"}" exec {NODE} "{root / "workflow_editor"}/cli/wfe.ts" "$@"\n')
    launcher.chmod(0o755)
    spec = importlib.util.spec_from_loader("wfnotify_old", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source.stdout, "wfnotify_old.py", "exec"), module.__dict__)  # noqa: S102 - the repository's own history
    sys.modules.setdefault("wfnotify_old", module)
    return {"wfe": str(launcher), "module": module}


from pathlib import Path  # noqa: E402


@pytest.mark.skip(reason="workflow_editor p9: runs live in their own working copies, which the pre-p5/ex1 release this test drives cannot see; the legacy migration it rehearses was carried out on 2026-10-11")
def test_legacy_state_is_migrated_and_reconciled(area, zulip, tmp_path, monkeypatch, old_release):  # noqa: F811
    ex = request_and_plan(area, monkeypatch, "pytest-notify-0301")
    old = old_release["module"].Notifier([old_release["wfe"]], zulip, zulip, inflight=tmp_path / "inflight.json", log=print)
    for _ in range(3):
        old.pass_once()
    assert posted_events(zulip) == ["#2:started", "#5:question"]
    wfe(area, "run", "answer", "docs/run-001", "q1", "--answer", "Agreed.", "--by", "Test Person")
    zulip.mode = "lost"
    old.pass_once()  # #6 posted, answer lost
    assert ex.tick() == "launched"
    until(ex, "finished")
    with notify_db(area) as db:
        db.execute("UPDATE outbox SET next_try = '2000-01-01T00:00:00.000Z' WHERE state = 'failed'")
    zulip.mode = "down"
    old.pass_once()  # the grown batch #6 #13 is attempted: refused connection, marked uncertain
    zulip.mode = ""
    with notify_db(area) as db:
        legacy = db.execute("SELECT id, state, uncertain, message_id FROM outbox ORDER BY ord").fetchall()
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert version == 0
    uncertain = [r[0].split("#")[1] for r in legacy if r[2]]
    assert uncertain == ["6:answered", "13:reported"], legacy
    # The old transport failed before it posted the grown batch, so its
    # in-flight file still names the lost #6 only; #13 was never attempted
    # but is marked uncertain all the same.
    assert [i.split("#")[1] for i in json.loads((tmp_path / "inflight.json").read_text())["ids"]] == ["6:answered"]
    sent_before = {r[0]: r[3] for r in legacy if r[1] == "sent"}
    # The new releases take over the same area.
    n = notifier(area, zulip, tmp_path)
    for _ in range(3):
        n.pass_once()
    events = posted_events(zulip)
    assert events.count("#7:answered") == 1, f"found as posted, not sent again: {markers(zulip)}"
    assert events.count("#15:reported") == 1 and events.count("#17:completed") == 1, "never-posted and pending events went out once"
    assert events.index("#7:answered") < events.index("#15:reported") < events.index("#17:completed")
    with notify_db(area) as db:
        after = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, state, message_id FROM outbox")}
        version = db.execute("PRAGMA user_version").fetchone()[0]
        notes = [r[0] for r in db.execute("SELECT note FROM notes")]
    assert version == 3  # p6 adds the request conversations
    assert all(after[i] == ("sent", m) for i, m in sent_before.items()), "sent evidence survives"
    assert all(state == "sent" for state, _ in after.values())
    assert any("uncertain sends" in x for x in notes)
    assert list(tmp_path.glob("inflight.json.retired-*"))
    legacy_rows = [d for d in wfe(area, "notify", "deliveries") if d["kind"] == "legacy"]
    assert len(legacy_rows) == 1 and legacy_rows[0]["state"] == "resolved" and "1 event(s) found posted; 1 not posted" in legacy_rows[0]["checked"]
