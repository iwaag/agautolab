"""agautolab.wfexec against the real workflow editor CLI, with a stub agent
in place of the harness (workflow_editor p4 stage 3): a request goes through
a human wait (the process ends), an answer, a resumption and completion; a
stop is carried out and confirmed; a restarted executor neither relaunches
nor loses an attempt whose process is still alive, and calls a lost one
unknown."""
from __future__ import annotations

import json
import os
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
    assert (area["project"] / "made.txt").exists()
    published = subprocess.run(["git", "show", "main:made.txt"], cwd=area["remote"], capture_output=True, text=True)
    assert published.stdout == "made by the stub\n", "the checkpoint was published"


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
    log = subprocess.run(["git", "log", "--format=%s", "main"], cwd=area["remote"], capture_output=True, text=True).stdout
    assert "docs/run-001" in log
    published = subprocess.run(["git", "show", "main:devdocs/runs/docs/run-001/report.md"], cwd=area["remote"], capture_output=True, text=True)
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
    monkeypatch.setattr(ex, "agent_argv", lambda cwd, profile=None: (asked.append(profile), real(cwd, profile))[1])
    assert ex.tick() == "launched"
    until(ex, "finished")
    assert asked == ["stub"], "launched with the workflow's profile"
    prompt = out.read_text()
    contract = GUIDE.read_text()
    assert prompt.startswith(contract.rstrip()), "the common contract comes first"
    assert "# Project guide: agent/g.md\n\n# guide-agent\nProject fact: decks are blue." in prompt
    assert prompt.index("# Project guide: agent/g.md") < prompt.index("# This attempt")
    assert "you perform this run as guide-agent; the execution host autolab launched you" in prompt
    r = wfe(area, "run", "show", "ask/run-001")["record"]
    assert r["executor"] == {"name": "guide-agent", "backend": None, "host": "autolab"}
    begin = next(e for e in r["history"] if e["op"] == "attempt.begin")
    assert begin["settings"] == {
        "agent": "guide-agent", "profile": "stub", "backend": "trial command",
        "contract": {"name": "wfrun/guide.md", "sha256": hashlib.sha256(contract.encode()).hexdigest()},
        "guides": [{"path": "agent/g.md", "sha256": hashlib.sha256(b"# guide-agent\nProject fact: decks are blue.\n").hexdigest()}],
    }
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
    refused = run("start", RUN, "build", check=False)
    assert refused.returncode == 1 and "is a node boundary" in refused.stdout + refused.stderr, refused.stdout + refused.stderr
    pending = json.loads(run("instructions", RUN).stdout)["pending"]
    run("ack", RUN, *pending, "--response", "incorporate", "--note", "build follows it")
    run("start", RUN, "build")
    open("made.txt", "w").write("made, bigger\\n")
    run("complete", RUN, "build", "--outcome", "made.txt", "--artifact", "made.txt")
''')


def test_instruction_during_a_node_is_taken_up_at_the_boundary(area, monkeypatch):
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
    ops = [e["op"] for e in r["history"]]
    assert ops.index("instruction.ack") < max(i for i, e in enumerate(r["history"]) if e["op"] == "node.start" and e.get("node") == "build")
