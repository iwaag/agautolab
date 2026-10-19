"""agautolab.wfexec against the real workflow editor CLI, with a stub agent
in place of the harness (workflow_editor p4 stage 3): a request goes through
a human wait (the process ends), an answer, a resumption and completion; a
stop is carried out and confirmed; a restarted executor neither relaunches
nor loses an attempt whose process is still alive, and calls a lost one
unknown."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agautolab.wfexec import Executor, Settings

# The workflow editor package whose CLI is under test: WFE_PACKAGE, or the
# source in agdevworld (workflow_editor/, a system component since
# workflow_editor p4/ex1). An installed release (<area>/service/current) works too.
PACKAGE = Path(os.environ.get("WFE_PACKAGE") or Path(__file__).resolve().parents[2] / "agdevworld" / "workflow_editor")
NODE = "node"

WORKFLOW = """schema: ag.workflow.v2
documentation: {policy: manual}
id: ship
name: Ship
intent: |
  Agree on a scope, then build it.
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

# The same work under the default documentation policy (workflow_editor p5):
# the run plans, reports each node and reports the whole.
DOC_WORKFLOW = """schema: ag.workflow.v2
documentation: {policy: default}
id: docs
name: Docs
intent: |
  Build one thing, with a plan and reports.
repositories:
  root: {path: ., access: editable}
nodes:
  build:
    type: do
    description: Build it.
"""

# A stub that follows the documentation steps; STUB_MODE=die-in-report
# crashes the first time it is writing a report.
DOC_STUB = textwrap.dedent('''
    import json, os, subprocess, sys
    wfe = os.environ["WFE_BIN"]
    RUN = "docs/run-001"
    def run(*a):
        p = subprocess.run([wfe, "run", *a, "--json"], capture_output=True, text=True)
        if p.returncode: print("refused:", a, p.stderr, file=sys.stderr); sys.exit(3)
        return p
    rec = json.loads(subprocess.run([wfe, "run", "show", RUN, "--json"], capture_output=True, text=True).stdout)["record"]
    folder = "devdocs/runs/docs/run-001/"
    st = lambda n: rec["nodes"][n]["state"]
    if st("_plan") == "pending":
        run("start", RUN, "_plan")
        open(folder + "plan.md", "w").write("# Plan\\n\\nBuild one thing; check that it exists.\\n")
        run("ask", RUN, "_plan", "--question", "Agree to plan.md?", "--to", "Test Person")
        sys.exit(0)
    if st("_plan") == "waiting":
        run("take-up", RUN, "q1")
        run("complete", RUN, "_plan", "--outcome", "Plan agreed.")
        run("start", RUN, "build")
        open("made.txt", "w").write("made\\n")
        run("complete", RUN, "build", "--outcome", "made.txt", "--artifact", "made.txt")
        run("start", RUN, "_report.build")
        if os.environ.get("STUB_MODE") == "die-in-report":
            sys.exit(1)
    open(folder + "report-build.md", "w").write("# build\\n\\nmade.txt exists.\\n")
    run("complete", RUN, "_report.build", "--outcome", "Built made.txt. Verified: the file exists. Next: final report.")
    run("start", RUN, "_final-report")
    open(folder + "report.md", "w").write("# Report\\n\\nmade.txt was built; see report-build.md.\\n")
    run("complete", RUN, "_final-report", "--outcome", "Delivered made.txt; awaiting the person's review.")
''')

# The stub agent: reads the run, does the next thing a real agent would.
STUB = textwrap.dedent('''
    import json, os, signal, subprocess, sys, time
    wfe = os.environ["WFE_BIN"]
    def run(*a):
        p = subprocess.run([wfe, "run", *a, "--json"], capture_output=True, text=True)
        if p.returncode: print("refused:", p.stderr, file=sys.stderr)
        return p
    show = json.loads(subprocess.run([wfe, "run", "show", "run-001", "--json"], capture_output=True, text=True).stdout)
    nodes = show["record"]["nodes"]
    mode = os.environ.get("STUB_MODE", "")
    if nodes["scope"]["state"] == "pending":
        run("start", "run-001", "scope")
        if mode == "sleep":
            signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))
            time.sleep(120)
        run("ask", "run-001", "scope", "--question", "Small or large?", "--to", "Test Person")
        if mode == "leftover":
            # The session ends (its final result), but something it left in
            # the background keeps the process alive (p8 ex1).
            print(json.dumps({"type": "result", "subtype": "success", "is_error": False}), flush=True)
            subprocess.Popen(["sleep", "300"])
            time.sleep(300)
    elif nodes["scope"]["state"] == "waiting":
        run("take-up", "run-001", "q1")
        run("complete", "run-001", "scope", "--outcome", "small, as answered")
        run("start", "run-001", "build")
        open("made.txt", "w").write("made by the stub\\n")
        run("complete", "run-001", "build", "--outcome", "made.txt", "--artifact", "made.txt")
''')

node_available = subprocess.run([NODE, "--version"], capture_output=True).returncode == 0 and (PACKAGE / "node_modules").exists()
pytestmark = pytest.mark.skipif(not node_available, reason="node or the workflow editor package is not available")


@pytest.fixture()
def area(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "gitconfig").write_text("[user]\n\tname = Test Person\n\temail = t@example.invalid\n[protocol \"file\"]\n\tallow = always\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("WFE_LOCK_DIR", str(tmp_path / "locks"))
    area = tmp_path / "area"
    area.mkdir()
    registry = area / "registry.json"
    launcher = area / "bin" / "wfe"
    launcher.parent.mkdir()
    launcher.write_text(f'#!/bin/sh\nWFE_REGISTRY="{registry}" exec {NODE} "{PACKAGE}/cli/wfe.ts" "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.setenv("WFE_BIN", str(launcher))
    project = area / "pj-demo"
    subprocess.run([str(launcher), "create", str(project), "--name", "Demo", "--no-gitea"], check=True, capture_output=True)
    (project / "devdocs" / "workflows" / "ship.yaml").write_text(WORKFLOW)
    (project / "devdocs" / "workflows" / "docs.yaml").write_text(DOC_WORKFLOW)
    remote = area / "remote.git"
    for args in (["add", "-A"], ["commit", "-q", "-m", "workflow"], ["init", "-q", "--bare", "--initial-branch=main", str(remote)],
                 ["remote", "add", "origin", str(remote)], ["push", "-q", "-u", "origin", "main"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    stub = area / "stub.py"
    stub.write_text(STUB)
    (area / "doc_stub.py").write_text(DOC_STUB)
    return {"area": area, "wfe": str(launcher), "project": project, "stub": stub, "doc_stub": area / "doc_stub.py", "remote": remote}


def executor(a, stub: str = "stub") -> Executor:
    s = Settings(wfe=[a["wfe"]], agent_command=[sys.executable, str(a[stub])])
    return Executor(s, log=lambda m: print(m))


def wfe(a, *args: str) -> dict:
    p = subprocess.run([a["wfe"], *args, "--json"], cwd=a["project"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout
    return json.loads(p.stdout)


def record(a) -> dict:
    return wfe(a, "run", "show", "run-001")["record"]


def until(ex: Executor, want: str, limit: float = 30) -> None:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if ex.tick() == want:
            return
        time.sleep(0.2)
    raise AssertionError(f"no {want} within {limit} s")


def test_request_wait_answer_resume_complete(area):
    req = area["area"] / "words.md"
    req.write_text("Please build a small thing.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-0001", "--workspace", "demo")
    ex = executor(area)
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = record(area)
    assert r["nodes"]["scope"]["state"] == "waiting"
    assert r["control"]["attempt"] is None, "no process runs through the human wait"
    assert r["control"]["last"]["outcome"] == "exited"
    assert ex.tick() == "idle", "nothing to do until the person answers"
    wfe(area, "run", "answer", "run-001", "q1", "--answer", "Small.", "--by", "Test Person", "--receipt", "pytest-answer-0001")
    wfe(area, "run", "answer", "run-001", "q1", "--answer", "Small.", "--by", "Test Person", "--receipt", "pytest-answer-0001")
    assert len(record(area)["questions"]["q1"]["answers"]) == 1, "a retransmitted answer is recorded once"
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = record(area)
    assert r["execution"]["state"] == "completed"
    assert r["control"]["attempts"] == 2
    assert [e["op"] for e in r["history"]].count("attempt.begin") == 2
    # The run worked in its own copy and saved to its own branch (workflow_editor p9).
    assert (area["project"] / ".local/wfe/runs/ship/run-001/tree/made.txt").exists()
    assert not (area["project"] / "made.txt").exists(), "the project workspace is the person's"
    published = subprocess.run(["git", "show", "wfe/demo/ship/run-001:made.txt"], cwd=area["remote"], capture_output=True, text=True)
    assert published.stdout == "made by the stub\n", "the checkpoint was published on the run branch"
    shared = subprocess.run(["git", "show", "main:made.txt"], cwd=area["remote"], capture_output=True, text=True)
    assert shared.returncode != 0, "and not on the shared branch"


def test_stop_is_carried_out_and_confirmed(area, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "sleep")
    req = area["area"] / "words.md"
    req.write_text("Build, slowly.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-0002", "--workspace", "demo")
    ex = executor(area)
    assert ex.tick() == "launched"
    time.sleep(1.5)
    wfe(area, "run", "stop", "run-001", "--reason", "wrong direction", "--by", "Test Person")
    assert record(area)["control"]["stop"] is not None, "requested, not yet confirmed"
    until(ex, "finished")
    r = record(area)
    assert r["control"]["hold"]["kind"] == "stopped"
    assert r["control"]["attempt"] is None
    assert ex.tick() == "idle", "a stopped run is not resumed by itself"


def test_a_session_that_ended_is_not_kept_open_by_what_it_left_in_the_background(area, monkeypatch):
    """project_migration p8 ex1 (front converse/run-029): the harness wrote
    its final result and lived on for a backgrounded command. The executor
    stops the leftovers after a grace and ends the attempt as the session
    ended: a normal exit, the slot free, the run waiting on its question."""
    import agautolab.wfexec as wx
    monkeypatch.setenv("STUB_MODE", "leftover")
    monkeypatch.setattr(wx, "SESSION_END_GRACE_SECONDS", 1)
    req = area["area"] / "words.md"
    req.write_text("Build it.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-0099", "--workspace", "demo")
    ex = executor(area)
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = record(area)
    assert r["control"]["last"]["outcome"] == "exited", r["control"]
    assert r["control"]["hold"] is None
    assert r["nodes"]["scope"]["state"] == "waiting"
    assert ex.current is None, "the slot is free"


def test_restarted_executor_keeps_a_live_attempt_and_calls_a_lost_one_unknown(area, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "sleep")
    req = area["area"] / "words.md"
    req.write_text("Build.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-0003", "--workspace", "demo")
    first = executor(area)
    assert first.tick() == "launched"
    pid = first.current.proc.pid
    # The executor dies; its agent process lives on in its own session.
    for _ in range(100):  # the agent records its start, then works (sleeps)
        if record(area)["nodes"]["scope"]["state"] == "running":
            break
        time.sleep(0.1)
    second = executor(area)
    assert second.tick() == "idle", "the live attempt keeps the slot: no duplicate launch"
    os.killpg(pid, 9)
    first.current.proc.wait()
    second.last_reconcile = 0
    second.tick()
    r = record(area)
    assert r["control"]["hold"]["kind"] == "unknown"
    assert r["nodes"]["scope"]["state"] == "running", "not completed or failed by recovery"
    assert second.tick() == "idle", "nothing reruns by itself"


def test_restarted_executor_adopts_its_live_attempt_and_carries_out_a_stop(area, monkeypatch):
    """project_migration p7: the executor takes back the process it left
    running (wfe exec adopt), so a stop requested after its restart is
    carried out here, and the end is read from the launch gate's exit file."""
    monkeypatch.setenv("STUB_MODE", "sleep")
    req = area["area"] / "words.md"
    req.write_text("Build.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-0103", "--workspace", "demo")
    first = executor(area)
    assert first.tick() == "launched"
    attempt = first.current.attempt
    for _ in range(100):
        if record(area)["nodes"]["scope"]["state"] == "running":
            break
        time.sleep(0.1)
    # The first executor is gone: its child is reaped as a dead parent's would be.
    import threading
    threading.Thread(target=first.current.proc.wait, daemon=True).start()
    second = executor(area)
    second.adopt()
    assert second.current is not None and second.current.attempt == attempt, "the live attempt is taken back"
    wfe(area, "run", "stop", "run-001", "--reason", "enough", "--by", "Test Person")
    until(second, "finished", limit=60)
    first.current.proc.wait()
    r = record(area)
    assert r["control"]["hold"]["kind"] == "stopped"
    assert r["control"]["last"]["outcome"] == "stopped"
    assert (Path(first.current.log).parent / f"{attempt}.jsonl.exit").read_text().strip() == "exit 143"


def test_default_policy_plans_reports_and_resumes_a_report_without_redoing_work(area, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "die-in-report")
    req = area["area"] / "words.md"
    req.write_text("Please build one thing.")
    wfe(area, "request", "docs", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-docs-01", "--workspace", "demo")
    ex = executor(area, "doc_stub")
    show = lambda: wfe(area, "run", "show", "docs/run-001")["record"]
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = show()
    assert r["nodes"]["_plan"]["state"] == "waiting", "the plan is consulted on before any work"
    assert r["questions"]["q1"]["document"]["path"].endswith("/plan.md")
    assert r["nodes"]["build"]["state"] == "pending"
    wfe(area, "run", "answer", "docs/run-001", "q1", "--answer", "Agreed.", "--by", "Test Person")
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = show()
    assert r["nodes"]["build"]["state"] == "completed", "the work is done"
    assert r["nodes"]["_report.build"]["state"] == "running", "the report was being written"
    assert r["control"]["hold"]["kind"] == "interrupted"
    assert r["execution"]["state"] != "completed"
    monkeypatch.setenv("STUB_MODE", "")
    wfe(area, "run", "resume", "docs/run-001", "--reason", "finish the report", "--by", "Test Person")
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = show()
    assert r["execution"]["state"] == "completed"
    assert r["nodes"]["_final-report"]["document"]["path"].endswith("/report.md")
    assert r["nodes"]["_plan"]["agreement"] == {"question": "q1"}
    completes = [e for e in r["history"] if e["op"] == "node.complete" and e["node"] == "build"]
    assert len(completes) == 1, "the completed work was not repeated"
    assert r["decisions"] == [], "the final report is not acceptance"
    log = subprocess.run(["git", "log", "--format=%s", "wfe/demo/docs/run-001"], cwd=area["remote"], capture_output=True, text=True).stdout
    assert "docs/run-001" in log
    published = subprocess.run(["git", "show", "wfe/demo/docs/run-001:devdocs/runs/docs/run-001/report.md"], cwd=area["remote"], capture_output=True, text=True)
    assert published.returncode == 0 and "made.txt was built" in published.stdout


def test_prolonged_store_lock_fails_ticks_without_doubling_attempts(area):
    """workflow_editor p5/ex1 step 4: exec.sqlite's write lock is held past
    the busy timeout while the executor would claim, and again while the
    attempt ends. The ticks fail (bounded, named); after the lock goes the
    executor claims once, launches once and records the one end."""
    import sqlite3
    req = area["area"] / "words.md"
    req.write_text("Please build a small thing.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-lock-0001", "--workspace", "demo")
    store = area["area"] / ".local" / "exec" / "exec.sqlite"
    ex = executor(area)

    def held(action):
        db = sqlite3.connect(store, isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with pytest.raises(RuntimeError) as failure:
                action()
            return time.monotonic() - started, str(failure.value)
        finally:
            db.execute("ROLLBACK")
            db.close()

    took, message = held(ex.tick)
    assert 4.5 <= took < 30, f"bounded by the busy timeout ({took:.1f} s)"
    assert "exec store:" in message and "SQLite code 5" in message, message
    assert wfe(area, "exec", "status", "run-001")["attempts"] == []
    assert ex.tick() == "launched"
    attempt = wfe(area, "exec", "status", "run-001")["attempts"][0]["id"]
    deadline = time.monotonic() + 30
    while ex.current.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    took, message = held(ex.tick)  # the agent has exited; recording its end waits for the lock
    assert "exec store:" in message, message
    until(ex, "finished")
    status = wfe(area, "exec", "status", "run-001")
    assert [a["id"] for a in status["attempts"]] == [attempt], "one attempt, not a second"
    assert status["attempts"][0]["state"] == "ended" and status["attempts"][0]["exit_code"] == 0
    assert [e["op"] for e in record(area)["history"]].count("attempt.end") == 1, "one end recorded"
    assert ex.tick() == "idle"


# workflow_editor p6: a workflow names its agent, profile and guides; the
# host launches it with that profile and composes the common contract, the
# project's guides and the brief, and the run records what it launched with.
SETTINGS_WORKFLOW = """schema: ag.workflow.v2
documentation: {policy: manual}
id: ask
name: Ask
intent: |
  Ask one thing.
execution:
  agent: guide-agent
  profile: stub
  guides: [agent/g.md]
repositories:
  root: {path: ., access: editable}
nodes:
  ask:
    type: talk
    description: Ask the person.
"""
PROMPT_STUB = textwrap.dedent('''
    import os, subprocess, sys
    wfe = os.environ["WFE_BIN"]
    open(os.environ["PROMPT_OUT"], "w").write(sys.stdin.read())
    for a in (["start", "ask/run-001", "ask"], ["ask", "ask/run-001", "ask", "--question", "Ok?", "--to", "Test Person"]):
        subprocess.run([wfe, "run", *a, "--json"], check=True, capture_output=True)
''')


def test_workflow_settings_choose_profile_guides_and_identity(area, monkeypatch):
    import hashlib
    from agautolab.wfexec import GUIDE
    project = area["project"]
    (project / "agent").mkdir()
    (project / "agent" / "g.md").write_text("# guide-agent\nProject fact: decks are blue.\n")
    (project / "devdocs" / "workflows" / "ask.yaml").write_text(SETTINGS_WORKFLOW)
    for args in (["add", "-A"], ["commit", "-q", "-m", "ask workflow"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (area["area"] / "prompt_stub.py").write_text(PROMPT_STUB)
    out = area["area"] / "prompt.txt"
    monkeypatch.setenv("PROMPT_OUT", str(out))
    req = area["area"] / "words.md"
    req.write_text("Ask me.")
    wfe(area, "request", "ask", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-settings", "--workspace", "demo")
    ex = Executor(Settings(wfe=[area["wfe"]], agent_command=[sys.executable, str(area["area"] / "prompt_stub.py")]), log=print)
    asked: list = []
    real = ex.agent_argv
    monkeypatch.setattr(ex, "agent_argv", lambda cwd, profile=None, session=None: (asked.append(profile), real(cwd, profile, session))[1])
    assert ex.tick() == "launched"
    until(ex, "finished")
    assert asked == ["stub"], "launched with the workflow's profile"
    prompt = out.read_text()
    contract = GUIDE.read_text()
    assert prompt.startswith(contract.rstrip()), "the common contract comes first"
    # workflow_editor p13: the claim's scoped context follows the contract, once.
    head = "## Workflow default — guide agent/g.md (agent/g.md)\n\n# guide-agent\nProject fact: decks are blue."
    assert head in prompt and prompt.count("Project fact: decks are blue.") == 1
    assert len(contract.rstrip()) < prompt.index("# Context (run ") < prompt.index(head) < prompt.index("# This attempt")
    assert "you perform this run as guide-agent; the execution host autolab launched you" in prompt
    r = wfe(area, "run", "show", "ask/run-001")["record"]
    assert r["executor"] == {"name": "guide-agent", "backend": None, "host": "autolab", "node": "local"}  # node: project_migration p7
    begin = next(e for e in r["history"] if e["op"] == "attempt.begin")
    context = begin["settings"].pop("context")
    assert context["snapshot"].startswith("ctx-") and context["fingerprint"].startswith("sha256:")
    g = next(x for x in context["sources"] if x["key"] == "guide/agent/g.md")
    assert g == {"key": "guide/agent/g.md", "scope": "workflow", "category": "default", "delivery": "body", "sha256": hashlib.sha256(b"# guide-agent\nProject fact: decks are blue.\n").hexdigest()}
    delivered = [e for e in r["history"] if e["op"] == "attempt.context"]
    assert [(e["event"], e["snapshot"]) for e in delivered] == [("delivered", context["snapshot"])], "the host records that the prompt reached the process"
    assert begin["settings"] == {
        "agent": "guide-agent", "profile": "stub", "backend": "trial command",
        "contract": {"name": "wfrun/guide.md", "sha256": hashlib.sha256(contract.encode()).hexdigest()},
        # workflow_editor p7/ex1: the run's node-catalog snapshot (the area's seeds)
        "catalog": {"state": "supplied", "sha256": r["context"]["catalog"]["sha256"], "templates": 2},
        # agent_session p1: no session policy declared = fresh; a trial
        # command offers no sessions, so the attempt has no session id.
        "session": {"policy": "fresh", "mode": "new", "id": None},
    }
    assert "The person recognizes these activities as distinct steps." in prompt, "the run's catalog reaches the agent"
    assert r["nodes"]["ask"]["state"] == "waiting"


# workflow_editor p6 step 5: an instruction arriving while a node runs does
# not interrupt it; the next start is refused until the attempt acknowledges
# it, and the attempt then goes on and ends normally (not interrupted).
BOUNDARY_STUB = textwrap.dedent('''
    import json, os, subprocess, sys, time
    wfe = os.environ["WFE_BIN"]
    RUN = "ship/run-001"
    def run(*a, check=True):
        p = subprocess.run([wfe, "run", *a, "--json"], capture_output=True, text=True)
        if check and p.returncode: print("refused:", a, p.stdout, p.stderr, file=sys.stderr); sys.exit(3)
        return p
    run("start", RUN, "scope")
    open(os.environ["STUB_SIGNAL"] + ".running", "w").write("scope")
    while not os.path.exists(os.environ["STUB_SIGNAL"]):
        time.sleep(0.1)
    run("complete", RUN, "scope", "--outcome", "scope settled without a question")
    run("start", RUN, "build")  # p11: an unread instruction holds no transition
    pending = json.loads(run("instructions", RUN).stdout)["pending"]
    run("ack", RUN, *pending, "--response", "incorporate", "--note", "build follows it")
    open("made.txt", "w").write("made, bigger\\n")
    run("complete", RUN, "build", "--outcome", "made.txt", "--artifact", "made.txt")
''')


def test_instruction_during_a_node_is_read_without_holding_a_start(area, monkeypatch):
    (area["area"] / "boundary_stub.py").write_text(BOUNDARY_STUB)
    signal_file = area["area"] / "go"
    monkeypatch.setenv("STUB_SIGNAL", str(signal_file))
    req = area["area"] / "words.md"
    req.write_text("Please build a small thing.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-req-boundary", "--workspace", "demo")
    ex = Executor(Settings(wfe=[area["wfe"]], agent_command=[sys.executable, str(area["area"] / "boundary_stub.py")]), log=print)
    assert ex.tick() == "launched"
    end = time.monotonic() + 30
    while not (area["area"] / "go.running").exists():
        assert time.monotonic() < end, "the stub never started scope"
        time.sleep(0.1)
    wfe(area, "run", "instruct", "run-001", "--instruction", "Make it bigger.", "--from", "Test Person", "--by", "Test Person")
    r = record(area)
    assert r["nodes"]["scope"]["state"] == "running", "nothing interrupted the running node"
    assert r["instructions"]["i1"]["during"] == ["scope"]
    signal_file.write_text("go")
    until(ex, "finished")
    r = record(area)
    assert r["execution"]["state"] == "completed"
    assert r["control"]["last"]["outcome"] == "exited", "a healthy attempt is not marked interrupted"
    ack = r["instructions"]["i1"]["ack"]
    assert ack["response"] == "incorporate" and ack["attempt"] == r["control"]["last"]["id"]


# ---- sessions (agent_session p1) --------------------------------------------------
# A stub that behaves like a harness keeping explicit conversations: `new`
# refuses an id already stored, `resume` of an id it lacks fails before doing
# anything, the init line announces the id, and every prompt is appended to
# the conversation (its store file) and to PROMPTS (for assertions).
SESSION_WORKFLOW = WORKFLOW.replace("id: ship\n", "id: keep\nexecution: {session: run, guides: [agent/g.md]}\n")
FRESH_WORKFLOW = WORKFLOW.replace("id: ship\n", "id: anew\nexecution: {session: fresh}\n")
SESSION_STUB = textwrap.dedent('''
    import json, os, signal, subprocess, sys, time
    from pathlib import Path
    wfe = os.environ["WFE_BIN"]
    store = Path(os.environ["SESSION_STORE"])
    mode, sid = os.environ.get("WFE_SESSION_MODE"), os.environ.get("WFE_SESSION_ID")
    prompt = sys.stdin.read()
    if os.environ.get("STUB_MODE") == "die-before-store":
        time.sleep(60)
    if os.environ.get("STUB_MODE") == "other-session":  # the harness opens a conversation nobody chose
        print(json.dumps({"type": "system", "subtype": "init", "session_id": f"other-{sid}"}), flush=True)
        time.sleep(60)
    conv = store / sid
    if mode == "new" and conv.exists():
        print(f"Error: Session ID {sid} is already in use."); sys.exit(1)
    if mode == "resume" and not conv.exists():
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "errors": [f"No conversation found with session ID: {sid}"]})); sys.exit(1)
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    with conv.open("a") as f: f.write(json.dumps({"mode": mode, "prompt": prompt}) + "\\n")
    with open(os.environ["PROMPTS"], "a") as f: f.write(json.dumps({"sid": sid, "mode": mode, "prompt": prompt}) + "\\n")
    run_ref = os.environ["RUN_REF"]
    def run(*a):
        p = subprocess.run([wfe, "run", *a, "--json"], capture_output=True, text=True)
        if p.returncode: print("refused:", p.stderr, file=sys.stderr)
        return p
    nodes = json.loads(subprocess.run([wfe, "run", "show", run_ref, "--json"], capture_output=True, text=True).stdout)["record"]["nodes"]
    if nodes["scope"]["state"] == "pending":
        run("start", run_ref, "scope")
        if os.environ.get("STUB_MODE") == "sleep":
            time.sleep(60)
        run("ask", run_ref, "scope", "--question", "Small or large?", "--to", "Test Person")
    elif nodes["scope"]["state"] == "waiting":
        run("take-up", run_ref, "q1")
        run("complete", run_ref, "scope", "--outcome", "small")
        run("start", run_ref, "build")
        open("made.txt", "w").write("made\\n")
        run("complete", run_ref, "build", "--outcome", "made.txt", "--artifact", "made.txt")
''')


@pytest.fixture()
def sessions(area, monkeypatch):
    project = area["project"]
    (project / "agent").mkdir(exist_ok=True)
    (project / "agent" / "g.md").write_text("# guide\nFact one.\n")
    (project / "devdocs" / "workflows" / "keep.yaml").write_text(SESSION_WORKFLOW)
    (project / "devdocs" / "workflows" / "anew.yaml").write_text(FRESH_WORKFLOW)
    for args in (["add", "-A"], ["commit", "-q", "-m", "session workflows"], ["push", "-q"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    store = area["area"] / "sessions"
    store.mkdir()
    (area["area"] / "session_stub.py").write_text(SESSION_STUB)
    monkeypatch.setenv("SESSION_STORE", str(store))
    monkeypatch.setenv("PROMPTS", str(area["area"] / "prompts.jsonl"))
    return {**area, "store": store}


def session_executor(a, trial_sessions: bool = True) -> Executor:
    s = Settings(wfe=[a["wfe"]], agent_command=[sys.executable, str(a["area"] / "session_stub.py")],
                 trial_sessions=trial_sessions, trial_session_store=a["store"])
    return Executor(s, log=lambda m: print(m))


def prompts(a) -> list[dict]:
    p = a["area"] / "prompts.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def request(a, workflow: str, receipt: str, monkeypatch, run: str = "run-001") -> str:
    req = a["area"] / "words.md"
    req.write_text("Please build a small thing.")
    wfe(a, "request", workflow, "--braindump", str(req), "--author", "Test Person", "--receipt", receipt, "--workspace", "demo")
    monkeypatch.setenv("RUN_REF", f"{workflow}/{run}")
    return f"{workflow}/{run}"


def answer(a, ref: str, receipt: str) -> None:
    wfe(a, "run", "answer", ref, "q1", "--answer", "Small.", "--by", "Test Person", "--receipt", receipt)


def test_run_policy_resumes_one_explicit_session_across_a_wait(sessions, monkeypatch):
    from agautolab.wfexec import GUIDE
    ref = request(sessions, "keep", "pytest-sess-0001", monkeypatch)
    ex = session_executor(sessions)
    assert ex.tick() == "launched"
    until(ex, "finished")
    first = wfe(sessions, "run", "show", ref)["record"]
    sid = first["sessions"][0]["id"]
    assert first["sessions"][0]["attached"] == sid and first["sessions"][0]["state"] == "open"
    begin = next(e for e in first["history"] if e["op"] == "attempt.begin")
    assert begin["settings"]["session"] == {"policy": "run", "mode": "new", "id": sid}, "the id is recorded before launch"
    answer(sessions, ref, "pytest-sess-ans-1")
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = wfe(sessions, "run", "show", ref)["record"]
    assert r["execution"]["state"] == "completed"
    assert len(r["sessions"]) == 1 and len(r["sessions"][0]["attempts"]) == 2, "one session served both attempts"
    second = [e for e in r["history"] if e["op"] == "attempt.begin"][1]
    assert second["settings"]["session"] == {"policy": "run", "mode": "resume", "id": sid}
    p = prompts(sessions)
    assert [x["mode"] for x in p] == ["new", "resume"] and p[0]["sid"] == p[1]["sid"] == sid
    assert p[0]["prompt"].startswith(GUIDE.read_text().rstrip()), "a new session gets the whole bootstrap"
    assert p[1]["prompt"].startswith(f"# This attempt: {second['attempt']}"), "a resumed one the continuation"
    assert GUIDE.read_text().strip()[:200] not in p[1]["prompt"] and "Fact one." not in p[1]["prompt"], "contract and guides are not appended again"
    assert "earlier attempt id in this conversation is ended" in p[1]["prompt"]
    # p1 ex2: the answer itself, no history read, no stable run facts again.
    assert "    Small." in p[1]["prompt"] and "q1 answer 0 from Test Person" in p[1]["prompt"]
    assert "--history" not in p[1]["prompt"] and "Working copy:" not in p[1]["prompt"]
    # Another run of the same workflow has a session of its own.
    ref2 = request(sessions, "keep", "pytest-sess-0002", monkeypatch, run="run-002")
    assert ex.tick() == "launched"
    until(ex, "finished")
    other = wfe(sessions, "run", "show", ref2)["record"]["sessions"][0]["id"]
    assert other != sid and prompts(sessions)[-1]["mode"] == "new"


def test_prompt_kinds_new_continuation_and_reconstruction():
    """agent_session p1 ex2: a new session gets the whole bootstrap; an
    ordinary resume the editor's continuation; a resume the editor marked
    for reconstruction the whole bootstrap again, saying why."""
    ex = Executor(Settings(wfe=["wfe"]), log=lambda m: None)
    base = {"attempt": {"id": "a2"}, "brief": "Run: p / w/run-001\nThe person's request is braindump.md.", "continuation": "Run: p / w/run-001\nPending: Small."}
    new = ex.prompt({**base, "execution": {"session": {"policy": "run", "mode": "new", "id": "s"}}}, contract="CONTRACT")
    assert new.startswith("CONTRACT") and "The person's request is braindump.md." in new and "Pending: Small." not in new
    cont = ex.prompt({**base, "execution": {"session": {"policy": "run", "mode": "resume", "id": "s"}}}, contract="CONTRACT")
    assert cont.startswith("# This attempt: a2") and "Pending: Small." in cont and "CONTRACT" not in cont and "--history" not in cont
    rec = ex.prompt({**base, "continuation": None, "execution": {"session": {"policy": "run", "mode": "resume", "id": "s", "reconstruct": "the conversation was compacted in attempt a1"}}}, contract="CONTRACT")
    assert rec.startswith("# Attempt a2: the run's instructions again") and "the conversation was compacted in attempt a1" in rec
    assert "CONTRACT" in rec and "The person's request is braindump.md." in rec


def test_fresh_policy_starts_a_new_session_each_attempt(sessions, monkeypatch):
    ref = request(sessions, "anew", "pytest-sess-0003", monkeypatch)
    ex = session_executor(sessions)
    assert ex.tick() == "launched"
    until(ex, "finished")
    answer(sessions, ref, "pytest-sess-ans-3")
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = wfe(sessions, "run", "show", ref)["record"]
    assert r["execution"]["state"] == "completed"
    assert [s["state"] for s in r["sessions"]] == ["replaced", "open"]
    assert r["sessions"][0]["id"] != r["sessions"][1]["id"]
    assert "fresh policy" in r["sessions"][0]["ended"]["reason"]
    assert [x["mode"] for x in prompts(sessions)] == ["new", "new"]


def test_run_policy_is_refused_where_no_session_can_be_resumed(sessions, monkeypatch):
    ref = request(sessions, "keep", "pytest-sess-0004", monkeypatch)
    ex = session_executor(sessions, trial_sessions=False)
    assert ex.tick() == "idle", "nothing launched"
    r = wfe(sessions, "run", "show", ref)["record"]
    assert r["control"]["attempts"] == 0 and not r.get("sessions")
    jobs = wfe(sessions, "exec", "status", ref, "--workspace", "demo")["jobs"]
    # project_migration p7: the job waits for a worker that can keep the session.
    assert jobs[-1]["state"] == "queued" and "session policy is \"run\"" in jobs[-1]["note"]
    assert prompts(sessions) == [], "never silently fresh"


def test_missing_session_is_replaced_before_launch(sessions, monkeypatch):
    ref = request(sessions, "keep", "pytest-sess-0005", monkeypatch)
    ex = session_executor(sessions)
    assert ex.tick() == "launched"
    until(ex, "finished")
    sid = wfe(sessions, "run", "show", ref)["record"]["sessions"][0]["id"]
    (sessions["store"] / sid).unlink()  # the host lost the conversation
    answer(sessions, ref, "pytest-sess-ans-5")
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = wfe(sessions, "run", "show", ref)["record"]
    assert r["execution"]["state"] == "completed"
    old, new = r["sessions"]
    assert old["state"] == "replaced" and "missing on" in old["ended"]["reason"] and len(old["attempts"]) == 1
    assert new["state"] == "open" and new["id"] != sid and new["attached"] == new["id"]
    p = prompts(sessions)
    assert [x["mode"] for x in p] == ["new", "new"], "the replacement starts from the whole bootstrap"
    assert not any(x["mode"] == "resume" for x in p), "the missing session was never resumed blindly"
    # p1 ex2: the replacement reconstructs: the original request and the
    # pending answer are in its prompt, not only a pointer to the history.
    assert "braindump.md (author Test Person)" in p[1]["prompt"] and "    Small." in p[1]["prompt"]


def test_changed_guide_or_retirement_starts_a_new_session(sessions, monkeypatch):
    ref = request(sessions, "keep", "pytest-sess-0006", monkeypatch)
    ex = session_executor(sessions)
    assert ex.tick() == "launched"
    until(ex, "finished")
    copy = sessions["project"] / ".local/wfe/runs/keep/run-001/tree"
    (copy / "agent" / "g.md").write_text("# guide\nFact two.\n")  # the guide the run reads changed
    answer(sessions, ref, "pytest-sess-ans-6")
    assert ex.tick() == "launched"
    until(ex, "finished")
    r = wfe(sessions, "run", "show", ref)["record"]
    assert [s["state"] for s in r["sessions"]] == ["replaced", "open"]
    begin = [e for e in r["history"] if e["op"] == "attempt.begin"][1]["settings"]["session"]
    assert begin["mode"] == "new" and "context guide/agent/g.md changed" in begin["replaces"]["reason"]
    assert "Fact two." in prompts(sessions)[-1]["prompt"], "the new session is instructed with the new guide"
    # A person retires the current session between attempts.
    out = subprocess.run([sessions["wfe"], "run", "session", ref, "--retire", "--reason", "start clean", "--by", "Test Person"], cwd=sessions["project"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    r = wfe(sessions, "run", "show", ref)["record"]
    assert r["sessions"][-1]["state"] == "retired" and r["sessions"][-1]["ended"]["by"] == "Test Person"


def test_early_crash_and_restart_keep_one_writer_and_replace_an_unstored_session(sessions, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "die-before-store")
    ref = request(sessions, "keep", "pytest-sess-0007", monkeypatch)
    first = session_executor(sessions)
    assert first.tick() == "launched"
    pid = first.current.proc.pid
    sid = wfe(sessions, "run", "show", ref)["record"]["sessions"][0]["id"]
    second = session_executor(sessions)  # the executor restarted after launch
    assert second.tick() == "idle", "the live attempt keeps the slot: no second writer of the session"
    os.killpg(pid, 9)  # dies before the harness stored the conversation
    first.current.proc.wait()
    second.last_reconcile = 0
    second.tick()
    r = wfe(sessions, "run", "show", ref)["record"]
    assert r["control"]["hold"]["kind"] == "unknown"
    assert r["sessions"][0]["id"] == sid and "attached" not in r["sessions"][0], "intended, never confirmed"
    monkeypatch.setenv("STUB_MODE", "")
    wfe(sessions, "run", "resume", ref, "--reason", "looked: nothing happened", "--by", "Test Person")
    assert second.tick() == "launched"
    until(second, "finished")
    r = wfe(sessions, "run", "show", ref)["record"]
    assert [s["state"] for s in r["sessions"]] == ["replaced", "open"]
    assert "missing on" in r["sessions"][0]["ended"]["reason"]
    assert [x["mode"] for x in prompts(sessions)] == ["new"]


# ---- launch ownership (agent_session p1 ex1) -------------------------------------------
# The agent starts only behind the launch gate; `MARKER` gets a line the moment
# agent work begins, so "no agent work" is a missing file, not a guess.
MARKER_STUB = 'import os, runpy, sys\nopen(os.environ["MARKER"], "a").write("ran\\n")\nrunpy.run_path(sys.argv[1], run_name="__main__")\n'


@pytest.fixture()
def launch(area, monkeypatch):
    (area["area"] / "marker_stub.py").write_text(MARKER_STUB)
    marker = area["area"] / "marker"
    monkeypatch.setenv("MARKER", str(marker))
    req = area["area"] / "words.md"
    req.write_text("Please build a small thing.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-launch-0001", "--workspace", "demo")
    return {**area, "marker": marker}


def marker_executor(a) -> Executor:
    s = Settings(wfe=[a["wfe"]], agent_command=[sys.executable, str(a["area"] / "marker_stub.py"), str(a["stub"])])
    return Executor(s, log=lambda m: print(m))


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def attempt_rows(a) -> list[dict]:
    return wfe(a, "exec", "status", "run-001", "--workspace", "demo")["attempts"]


def age_launching_rows(a) -> None:
    """The restart came more than two minutes after the claim."""
    import sqlite3
    (db,) = list(a["area"].rglob("exec/exec.sqlite"))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE attempts SET began = '2026-01-01T00:00:00.000Z' WHERE state = 'launching'")


@pytest.mark.parametrize("where", ["started", "gate"])
def test_a_launch_failing_after_the_spawn_stops_the_process_before_the_attempt_ends(launch, monkeypatch, where):
    ex = marker_executor(launch)
    real, seen = ex.wfe, {}

    def wfe_(*args, **kw):
        if args[:2] == ("exec", "started"):
            seen["pid"] = int(args[4])
            if where == "started":
                raise RuntimeError("injected: the store refused `started`")
        if args[:2] == ("exec", "finished"):
            seen["alive at finish"] = group_alive(seen["pid"])
        return real(*args, **kw)

    ex.wfe = wfe_
    if where == "gate":
        def gate(fd):
            raise OSError("injected: the gate pipe failed")
        monkeypatch.setattr(Executor, "open_gate", staticmethod(gate))
    assert ex.tick() == "finished"
    assert seen["alive at finish"] is False, "the group was gone before the attempt was finalized"
    assert not launch["marker"].exists(), "no agent work started"
    assert ex.current is None
    r = record(launch)
    assert r["control"]["attempt"] is None and r["control"]["hold"]["kind"] == "interrupted"
    assert [a["state"] for a in attempt_rows(launch)] == ["ended"]


def test_an_unconfirmed_stop_keeps_the_process_and_the_slot(launch, monkeypatch):
    ex = marker_executor(launch)
    real = ex.wfe
    ex.wfe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")) if a[:2] == ("exec", "started") else real(*a, **k)
    monkeypatch.setattr("agautolab.wfexec.ABANDON_WAIT_SECONDS", 0.2)
    gone = Executor.group_gone
    monkeypatch.setattr(Executor, "group_gone", staticmethod(lambda r: False))
    assert ex.tick() == "stop-unconfirmed"
    assert ex.current is not None and ex.current.abandon
    age_launching_rows(launch)
    ex.last_reconcile = 0
    assert ex.tick() == "stop-unconfirmed", "reconcile --holding leaves the owned attempt alone"
    assert [a["state"] for a in attempt_rows(launch)] == ["launching"], "the slot stays held"
    monkeypatch.setattr(Executor, "group_gone", staticmethod(gone))
    assert ex.tick() == "finished"
    assert record(launch)["control"]["hold"]["kind"] == "interrupted"


def test_an_executor_dying_before_registration_leaves_no_writer(launch, tmp_path):
    ready = tmp_path / "ready"
    fake = tmp_path / "wfe-dies-at-started"
    fake.write_text(f'#!/bin/sh\nif [ "$1" = exec ] && [ "$2" = started ]; then echo "$5" > "{ready}.tmp" && mv "{ready}.tmp" "{ready}"; exec sleep 60; fi\nexec "{launch["wfe"]}" "$@"\n')
    fake.chmod(0o755)
    code = ("import sys; from agautolab.wfexec import Executor, Settings; "
            "Executor(Settings(wfe=[sys.argv[1]], agent_command=[sys.executable, sys.argv[2], sys.argv[3]])).tick()")
    first = subprocess.Popen([sys.executable, "-c", code, str(fake), str(launch["area"] / "marker_stub.py"), str(launch["stub"])], start_new_session=True)
    try:
        end = time.monotonic() + 60
        while not ready.exists():
            assert first.poll() is None and time.monotonic() < end, "the executor never reached `started`"
            time.sleep(0.05)
        pid = int(ready.read_text())
        assert group_alive(pid), "the agent was spawned, behind its gate"
        os.killpg(first.pid, signal.SIGKILL)  # the executor (and its pending `started`) dies
    finally:
        first.wait()
    end = time.monotonic() + 10
    while group_alive(pid):
        assert time.monotonic() < end, "the abandoned gate did not exit"
        time.sleep(0.05)
    assert not launch["marker"].exists(), "the unregistered process never started agent work"
    second = marker_executor(launch)
    assert second.tick() == "idle", "the launching row keeps the slot until it is stale"
    age_launching_rows(launch)
    second.last_reconcile = 0
    second.tick()
    assert record(launch)["control"]["hold"]["kind"] == "unknown"
    wfe(launch, "run", "resume", "run-001", "--reason", "looked: nothing ran", "--by", "Test Person")
    assert second.tick() == "launched"
    until(second, "finished")
    assert launch["marker"].read_text() == "ran\n", "exactly one writer ever worked"
    assert record(launch)["nodes"]["scope"]["state"] == "waiting"


# ---- session mismatch (agent_session p1 ex1) -------------------------------------------

def wfe_fails(a, *args: str) -> str:
    p = subprocess.run([a["wfe"], *args, "--json"], cwd=a["project"], capture_output=True, text=True)
    assert p.returncode != 0, p.stdout
    return p.stderr + p.stdout


def test_a_reported_session_mismatch_stops_the_attempt_and_nothing_continues_it(sessions, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "other-session")
    ref = request(sessions, "keep", "pytest-sess-0008", monkeypatch)
    ex = session_executor(sessions)
    assert ex.tick() == "launched"
    pid = ex.current.proc.pid
    until(ex, "finished")
    assert not group_alive(pid), "the attempt was stopped and its group confirmed gone"
    r = wfe(sessions, "run", "show", ref)["record"]
    (cur,) = r["sessions"]
    assert cur["state"] == "open" and cur["mismatch"] is True
    assert cur["attached"] == f"other-{cur['id']}", "both ids are kept"
    assert r["control"]["hold"]["kind"] == "interrupted"
    assert "session mismatch" in r["control"]["hold"]["reason"] and cur["id"] in r["control"]["hold"]["reason"]
    assert r["nodes"]["scope"]["state"] == "pending", "no work went on in the unverified conversation"
    for refused in (wfe_fails(sessions, "run", "resume", ref, "--reason", "go on", "--by", "Test Person"),
                    wfe_fails(sessions, "run", "recover", ref, "--incident", "i-test-1", "--attempt", r["control"]["last"]["id"], "--reason", "looked", "--by", "Observer")):
        assert "session-mismatch" in refused and "--retire" in refused, refused
    assert ex.tick() == "idle"


def test_retiring_a_mismatched_session_permits_a_new_one_without_a_recovery_budget(sessions, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "other-session")
    ref = request(sessions, "keep", "pytest-sess-0009", monkeypatch)
    ex = session_executor(sessions)
    ids = []
    for n in (1, 2, 3):
        assert ex.tick() == "launched"
        until(ex, "finished")
        r = wfe(sessions, "run", "show", ref)["record"]
        ids.append(r["sessions"][-1]["id"])
        assert r["sessions"][-1]["mismatch"] is True and r["control"]["hold"]["kind"] == "interrupted"
        wfe(sessions, "run", "session", ref, "--retire", "--reason", f"looked {n}: nothing ran in it", "--by", "Observer")
        r = wfe(sessions, "run", "show", ref)["record"]
        assert r["control"]["hold"]["kind"] == "interrupted", "retiring lifts no hold"
        recover = ("run", "recover", ref, "--incident", f"i-test-{n}", "--attempt", r["control"]["last"]["id"], "--reason", "the conversation was retired", "--by", "Observer")
        wfe(sessions, *recover)  # p11: recovery has no budget
    r = wfe(sessions, "run", "show", ref)["record"]
    assert len(set(ids)) == 3, "each attempt selected a new session"
    assert [s["state"] for s in r["sessions"]] == ["retired"] * 3
    assert [x["mode"] for x in prompts(sessions)] == [], "no attempt worked in any of them"
    assert ex.tick() == "launched", "the third recovery was not refused"
    until(ex, "finished")


def test_a_failed_attachment_record_is_retried(sessions, monkeypatch):
    ref = request(sessions, "keep", "pytest-sess-0010", monkeypatch)
    ex = session_executor(sessions)
    real, calls = ex.wfe, []

    def wfe_(*args, **kw):
        if args[:2] == ("exec", "session") and "--attached" in args or args[:2] == ("exec", "finished") and "--attached" in args:
            calls.append(args[1])
            if len(calls) == 1:
                raise RuntimeError("injected: the record was busy")
        return real(*args, **kw)

    ex.wfe = wfe_
    assert ex.tick() == "launched"
    until(ex, "finished")
    assert len(calls) >= 2, "the failed record was tried again"
    r = wfe(sessions, "run", "show", ref)["record"]
    (cur,) = r["sessions"]
    assert cur["attached"] == cur["id"] and not cur.get("mismatch")
    assert r["nodes"]["scope"]["state"] == "waiting" and r["control"]["hold"] is None


# project_migration p8: a workflow claimed in the worker's conversation lane.
CHAT_WORKFLOW = WORKFLOW.replace("id: ship\nname: Ship\n", "id: chat\nname: Chat\nexecution:\n  lane: conversation\n")
# An agent that only works until it is stopped.
SLEEPER = "import signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *a: sys.exit(143))\ntime.sleep(120)\n"


def test_lanes_each_claim_their_own_job_and_are_adopted_by_lane(area):
    """A long default-lane attempt and a conversation-lane attempt run at
    once, each claimed, stopped and ended by its own lane's executor; the
    heartbeat and reconcile of the default lane hold both; a restarted
    executor hands each live attempt back to its lane."""
    (area["area"] / "workers.json").write_text(json.dumps({"default": "local", "workers": {"local": {"local": True, "lanes": {"conversation": 1}}}}))
    (area["project"] / "devdocs" / "workflows" / "chat.yaml").write_text(CHAT_WORKFLOW)
    for args in (["add", "-A"], ["commit", "-q", "-m", "chat"]):
        subprocess.run(["git", *args], cwd=area["project"], check=True, capture_output=True)
    (area["area"] / "sleeper.py").write_text(SLEEPER)
    req = area["area"] / "words.md"
    req.write_text("Work.")
    wfe(area, "request", "ship", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-lane-0001", "--workspace", "demo")
    wfe(area, "request", "chat", "--braindump", str(req), "--author", "Test Person", "--receipt", "pytest-lane-0002", "--workspace", "demo")
    settings = Settings(wfe=[area["wfe"]], agent_command=[sys.executable, str(area["area"] / "sleeper.py")], lanes=["conversation"])
    first = Executor(settings, log=lambda m: print(m))
    conv = first.lanes["conversation"]
    assert conv.lane == "conversation" and conv.lanes == {}
    assert first.tick() == "launched"
    assert first.tick() == "executing", "the default slot is held"
    assert conv.tick() == "launched", "the conversation lane claims while the default slot is held"
    assert first.current.attempt != conv.current.attempt
    assert wfe(area, "run", "show", "ship/run-001")["record"]["control"]["attempt"]["id"] == first.current.attempt
    assert wfe(area, "run", "show", "chat/run-001")["record"]["control"]["attempt"]["id"] == conv.current.attempt
    first.last_reconcile = 0
    assert first.tick() == "executing", "reconcile holds every lane's attempt"
    assert conv.tick() == "executing"
    local = wfe(area, "exec", "workers")["workers"][0]
    assert local["reported"] == f"executing {first.current.attempt}; conversation: executing {conv.current.attempt}"
    assert [(x["attempt"], x["lane"]) for x in local["slots"]] == [(first.current.attempt, "default"), (conv.current.attempt, "conversation")]

    # The executor is restarted: each live attempt goes back to its lane.
    import threading
    for ex in (first, conv):
        threading.Thread(target=ex.current.proc.wait, daemon=True).start()
    second = Executor(settings, log=lambda m: print(m))
    second.adopt()
    assert second.current is not None and second.current.attempt == first.current.attempt
    assert second.lanes["conversation"].current is not None and second.lanes["conversation"].current.attempt == conv.current.attempt

    # Each lane carries out its own stop and records its own end.
    wfe(area, "run", "stop", "chat/run-001", "--reason", "enough", "--by", "Test Person")
    until(second.lanes["conversation"], "finished", limit=60)
    assert second.current is not None, "the default lane's attempt is untouched"
    assert wfe(area, "run", "show", "chat/run-001")["record"]["control"]["hold"]["kind"] == "stopped"
    assert wfe(area, "run", "show", "ship/run-001")["record"]["control"]["attempt"] is not None
    wfe(area, "run", "stop", "ship/run-001", "--reason", "enough", "--by", "Test Person")
    until(second, "finished", limit=60)
    assert wfe(area, "run", "show", "ship/run-001")["record"]["control"]["hold"]["kind"] == "stopped"


def test_lanes_setting_defaults_to_the_one_slot(tmp_path):
    f = tmp_path / "wfexec.toml"
    f.write_text('wfe = ["wfe"]\n')
    assert Settings.load(f).lanes == []
    assert Executor(Settings.load(f)).lanes == {}
    f.write_text('wfe = ["wfe"]\nlanes = ["conversation", "default"]\n')
    assert Settings.load(f).lanes == ["conversation"]
