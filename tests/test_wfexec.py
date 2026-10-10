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

EXPERIMENT = Path(__file__).resolve().parents[2] / "experiments" / "workflow_editor"
NODE = "node"

WORKFLOW = """schema: ag.workflow.v1
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

node_available = subprocess.run([NODE, "--version"], capture_output=True).returncode == 0 and (EXPERIMENT / "node_modules").exists()
pytestmark = pytest.mark.skipif(not node_available, reason="node or the workflow editor checkout is not available")


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
    launcher.write_text(f'#!/bin/sh\nWFE_REGISTRY="{registry}" exec {NODE} "{EXPERIMENT}/cli/wfe.ts" "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.setenv("WFE_BIN", str(launcher))
    project = area / "pj-demo"
    subprocess.run([str(launcher), "create", str(project), "--name", "Demo", "--no-gitea"], check=True, capture_output=True)
    (project / "devdocs" / "workflows" / "ship.yaml").write_text(WORKFLOW)
    remote = area / "remote.git"
    for args in (["add", "-A"], ["commit", "-q", "-m", "workflow"], ["init", "-q", "--bare", "--initial-branch=main", str(remote)],
                 ["remote", "add", "origin", str(remote)], ["push", "-q", "-u", "origin", "main"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    stub = area / "stub.py"
    stub.write_text(STUB)
    return {"area": area, "wfe": str(launcher), "project": project, "stub": stub, "remote": remote}


def executor(a) -> Executor:
    s = Settings(wfe=[a["wfe"]], agent_command=[sys.executable, str(a["stub"])])
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
