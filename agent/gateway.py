#!/usr/bin/env python3
"""Mission gateway: submit missions to the autolab agent over HTTP, no SSH.

Stdlib-only single-file server. Routes:

  POST /window    {"text": str}  -> the conversational window (see below)
  GET  /guide     agent/GUIDE.md, the capability card, as plain text
  GET  /status    mission/driver/done/session summary, scoped to the
                  current run (sessions started before it are excluded;
                  "game" says whether the served build is from this run)
  GET  /log       ?tail=N  tail of the current (or last) drive log
  GET  /jobs      one summary row per .local/jobs/<job>/
  GET  /projects  project role-profile selections and available profiles
  GET  /jobs/<job>              status document + cost rollup + evidence timeline
  GET  /jobs/<job>/evidence/<iter>/<file>   raw evidence file passthrough
  POST /jobs/<job>/summarize/<iter>  ?force=1  start a one-shot summarizer
  GET  /jobs/<job>/summarize/<iter>  {status: absent|pending|done|error, summary?}
  GET  /game/...  static files from .local/agent/serve/
  GET  /healthz   liveness probe

POST /window is this node's single desire-accepting conversational entrance
(devpolicy/policy.md, Single Entrance). It is handed the same job state the
typed GETs expose plus agent/GUIDE.md, and answers from them — and it can
start work: a <<mission>>...<</mission>> block in its reply is executed by
the gateway (start_mission: write MISSION.md, spawn drive.sh). The window
is the only entrance; there is no separate mission route.

No route carries authentication: this node serves a single-user experimental
cluster. The guards that exist are cost/concurrency guards, not auth.
POST /jobs/.../summarize/... spends money, so it is bounded by a
one-at-a-time guard and by the per-iteration cache (one paid call per
iteration ever). POST /window carries the same one-at-a-time guard; its
selected profile may use OpenCode or Claude Code.

Monitoring reads never write and never take a job's `.lock`, so they are safe
against a live iteration; half-written JSON degrades to an `error` field on
the affected row instead of a 500.

One mission at a time: a start is refused (409) while drive.sh is alive.
State lives under .local/agent/ next to the rest of the agent layer:

  serve/                 static dir the finished game is installed into
  gateway/run-NNNN.log   drive.sh combined output per accepted mission
  gateway/run-NNNN.exit  drive.sh exit code, written when it finishes
  gateway/current        run id + pid + start time of the active (or last) drive
  done                   written by the agent when it considers the mission
                         over; drive.sh stops on its existence and never reads
                         it, /status carries its content verbatim
  window/run-NNNN.json   one record per window answer (devpolicy/agent_records.md)
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agag.agent_config import AgentConfigError, load_config  # noqa: E402
from agautolab.agent_settings import AGENTS_CONFIG, AGENTS_LOCAL_CONFIG  # noqa: E402
from agautolab.project_settings import (  # noqa: E402
    PROJECTS_ROOT,
    PROJECT_AGENT_ROLES,
    ProjectSettingsError,
    load_project_roles,
)
from agautolab.role_run import run_role  # noqa: E402
STATE = ROOT / ".local" / "agent"
GATEWAY = STATE / "gateway"
SERVE = STATE / "serve"
JOBS = ROOT / ".local" / "jobs"
MONITOR = Path(__file__).resolve().parent / "monitor"
GUIDE = Path(__file__).resolve().parent / "GUIDE.md"
WINDOW = STATE / "window"

# Versioned envelope, in the spirit of nctl's `nctl.drift.v1`: scope 3 points
# agdevworld at this same feed, and the kind is what keeps that cheap.
KIND = "autolab.monitor.v1"
PROJECTS_KIND = "autolab.projects.v1"

TERMINAL_STATUSES = {"converged", "stuck", "error"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ITER_NAME = re.compile(r"^iter-\d+$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def current_run():
    try:
        return json.loads((GATEWAY / "current").read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    # kill(0, sig) and kill(-1, sig) address process groups / every process, so
    # they would answer "alive" for a pid that never existed. Only real pids.
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def drive_running():
    cur = current_run()
    if not cur:
        return None
    # The exit file is written by the wrapper's last command, so its presence
    # is the authoritative "finished" signal — os.kill(pid, 0) alone still
    # succeeds while the detached child is an unreaped zombie.
    if (GATEWAY / f"run-{cur['run']:04d}.exit").exists():
        return None
    return cur if pid_alive(cur.get("pid")) else None


def start_mission(mission, max_sessions=12):
    """The one mission-starting seam: write MISSION.md, spawn drive.sh.

    Returns (http-shaped code, document): 202 accepted, 400 bad input,
    409 while a drive is alive. The window's mission block is the caller;
    the codes are its concurrency semantics, kept from the retired route.
    """
    if not (isinstance(mission, str) and mission.strip()):
        return 400, {"error": "mission must be a non-empty string"}
    if not (isinstance(max_sessions, int) and 1 <= max_sessions <= 50):
        return 400, {"error": "max_sessions must be 1..50"}
    running = drive_running()
    if running:
        return 409, {"error": "a mission is already running", "current": running}
    GATEWAY.mkdir(parents=True, exist_ok=True)
    run = 1
    while (GATEWAY / f"run-{run:04d}.log").exists():
        run += 1
    (STATE / "MISSION.md").write_text(mission)
    # A previous mission's end-of-mission note must not stop this one.
    (STATE / "done").unlink(missing_ok=True)
    log = open(GATEWAY / f"run-{run:04d}.log", "w")
    exit_file = GATEWAY / f"run-{run:04d}.exit"
    cmd = f'agent/drive.sh {max_sessions}; echo $? > "{exit_file}"'
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.close()
    (GATEWAY / "current").write_text(
        json.dumps(
            {
                "run": run,
                "pid": proc.pid,
                "max_sessions": max_sessions,
                "started": time.time(),
            }
        )
    )
    return 202, {"accepted": True, "run": run, "pid": proc.pid}


def agent_done():
    """The agent's own end-of-mission note, or None while it is still working.

    Existence is what drive.sh stops on; the content is the agent's words and
    is carried as written. start_mission clears a previous mission's file.
    """
    try:
        return (STATE / "done").read_text()
    except OSError:
        return None


def agent_notes():
    """The agent's NOTES.md, whole. It writes it; nothing here parses it."""
    try:
        return (STATE / "NOTES.md").read_text()
    except OSError:
        return None


def mission_text(path):
    """The mission as written."""
    try:
        return path.read_text()
    except OSError:
        return None


def session_summaries(since=None):
    # since: epoch seconds; only sessions written at/after it are returned,
    # so /status can report the current run instead of every run ever.
    out = []
    sessions = STATE / "sessions"
    current_records = sorted(sessions.glob("session-*.run.json"))
    legacy_records = sorted(p for p in sessions.glob("session-*.json")
                            if not p.name.endswith(".run.json"))
    for p in [*legacy_records, *current_records]:
        if since is not None and p.stat().st_mtime < since - 1:
            continue
        row = {"file": p.name}
        try:
            d = json.loads(p.read_text())
            row.update(
                is_error=d.get("is_error", d.get("outcome") == "failed"),
                mission_consumed=d.get("mission_consumed"),
                turns=d.get("num_turns"),
                cost_usd=d.get("cost_usd", d.get("total_cost_usd")),
                duration_s=round(d.get("duration_ms", 0) / 1000),
            )
        except ValueError:
            # The live session's file exists from the moment claude starts and
            # only becomes JSON when it finishes, so mid-run it is not a broken
            # session — it is the one still being written.
            row["is_error"] = "in progress" if drive_running() else "unparsed"
        out.append(row)
    return out


def sessions_on_disk():
    sessions = STATE / "sessions"
    return len(list(sessions.glob("session-*.run.json"))) + len([
        p for p in sessions.glob("session-*.json") if not p.name.endswith(".run.json")
    ])


def game_info(started=None):
    """Describe the served build so a fresh install is distinguishable
    from a stale one left by a previous mission."""
    index = SERVE / "index.html"
    if not index.is_file():
        return {"installed": False, "installed_mtime": None, "installed_this_run": False}
    mtime = index.stat().st_mtime
    return {
        "installed": True,
        "installed_mtime": mtime,
        "installed_this_run": started is not None and mtime >= started - 1,
    }


def sessions_cost():
    """Cumulative mediator cost over every session on disk, plus the subset
    belonging to the current run. Cost is the single most decision-relevant
    number for a human watching this thing, so it is computed unconditionally."""
    total = 0.0
    started = (current_run() or {}).get("started")
    run_total = 0.0
    records = list((STATE / "sessions").glob("session-*.run.json"))
    records += [p for p in (STATE / "sessions").glob("session-*.json")
                if not p.name.endswith(".run.json")]
    for p in records:
        try:
            doc = json.loads(p.read_text())
            c = doc.get("cost_usd", doc.get("total_cost_usd"))
        except (OSError, ValueError):
            continue
        if not isinstance(c, (int, float)):
            continue
        total += c
        if started is None or p.stat().st_mtime >= started - 1:
            run_total += c
    return {
        "sessions_usd": round(total, 6),
        "current_run_sessions_usd": round(run_total, 6) if started else None,
    }


def read_json(path):
    """Parse a JSON file, or return None. Tolerates the half-written file a
    live iteration leaves behind mid-write."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def job_yaml_fields(job_dir):
    """The few job.yaml fields the monitor shows. Uses PyYAML when the gateway
    process happens to have it (it runs bare python3, not under uv), and falls
    back to a tolerant scan of top-level scalars otherwise — a monitor must not
    hard-depend on a package the server may not have."""
    path = job_dir / "job.yaml"
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        import yaml  # noqa: PLC0415 - optional, resolved per call on purpose

        doc = yaml.safe_load(text) or {}
        if isinstance(doc, dict):
            return _job_fields(doc)
    except Exception:
        pass
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(project|profile|adapter|max_iterations|push):\s*(\S+)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip("\"'")
    return _job_fields(out)


def _job_fields(doc):
    """Keep the shown job.yaml fields in their expected types, whichever
    parser produced them — the page renders `iteration / max` arithmetically."""
    out = {}
    if isinstance(doc.get("adapter"), str):
        out["adapter"] = doc["adapter"]
    if isinstance(doc.get("profile"), str):
        out["profile"] = doc["profile"]
    if isinstance(doc.get("project"), str):
        out["project"] = doc["project"]
    try:
        out["max_iterations"] = int(doc["max_iterations"])
    except (KeyError, TypeError, ValueError):
        pass
    push = doc.get("push")
    if push is not None:
        out["push"] = push if isinstance(push, bool) else str(push).lower() == "true"
    return out


def projects_document():
    """Return every local project with its effective selectable role profiles.

    A broken project file is represented on that project's row. Other project
    rows remain useful while a human or agent is between valid edits.
    """
    config, overlay = load_config(AGENTS_CONFIG, AGENTS_LOCAL_CONFIG)
    profiles = sorted(config.get("profiles", {}))
    roles = config.get("roles", {})
    overlay_roles = overlay.get("roles", {})
    defaults = {
        role: overlay_roles.get(role, {}).get("profile", roles.get(role, {}).get("profile"))
        for role in sorted(PROJECT_AGENT_ROLES)
    }
    project_dirs = (
        sorted(path for path in PROJECTS_ROOT.iterdir() if path.is_dir())
        if PROJECTS_ROOT.is_dir()
        else []
    )
    projects = []
    for project_dir in project_dirs:
        row = {"name": project_dir.name}
        try:
            selected = load_project_roles(project_dir.name)
            row["roles"] = {
                role: {
                    "profile": selected.get(role, defaults[role]),
                    "source": "project" if role in selected else "default",
                }
                for role in sorted(PROJECT_AGENT_ROLES)
            }
            unknown = sorted(
                {value["profile"] for value in row["roles"].values()} - set(profiles)
            )
            if unknown:
                row["error"] = f"unknown profile(s): {', '.join(unknown)}"
        except ProjectSettingsError as error:
            row["error"] = str(error)
        projects.append(row)
    return {
        "kind": PROJECTS_KIND,
        "type": "projects",
        "profiles": profiles,
        "projects": projects,
    }


def evidence_iters(job_dir):
    d = job_dir / "evidence"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and ITER_NAME.match(p.name))


def iter_cost(job_dir, name):
    """Per-iteration cost from the normalized adapter record."""
    doc = read_json(job_dir / "evidence" / name / "adapter_result.json")
    if isinstance(doc, dict):
        cost = doc.get("cost_usd", doc.get("total_cost_usd"))
        if isinstance(cost, (int, float)):
            return cost
    return None


# --- iteration summaries -------------------------------------------------
#
# An iteration's evidence is summarized where it lives: a one-shot profile run
# reads .local/jobs/<job>/evidence/<iter>/ and writes prose next to it under
# summaries/. Callers outside this node get the prose, never the raw files —
# that boundary is the point of the feature, not an implementation detail.
#
# Everything this path writes stays inside summaries/: it never touches
# state.json, evidence, MISSION.md, NOTES.md or the job's .lock, so it is safe
# to run against an iteration that is still being written (such a summary is
# allowed to be wrong; `?force=1` regenerates it).

SUMMARY_PROMPT = """`{rel}` holds the evidence of one autolab coding-agent
iteration: `prompt.txt` (what the agent was asked), `diff.patch` (what it
changed), `gates.json` (the verification commands and their exit codes),
`adapter_result.json` (turns, duration, exit code, cost in USD),
`error.txt` when the iteration failed, and whatever else is there.

Summarize that iteration for someone watching this job who has not seen the
files. Your reply is shown to them as written.
"""

def summaries_dir(job_dir):
    return job_dir / "summaries"


def summary_paths(job_dir, iteration):
    d = summaries_dir(job_dir)
    return {
        "md": d / f"{iteration}.md",
        "raw": d / f"{iteration}.raw.json",
        "cost": d / f"{iteration}.cost.json",
        "run": d / f"{iteration}.run.json",
        "exit": d / f"{iteration}.exit",
        "log": d / f"{iteration}.log",
        "prompt": d / f"{iteration}.prompt.txt",
    }


def summary_status(job_dir, iteration):
    """State of one iteration's summary. The `.md` file is the cache and the
    only success signal: a summarizer that exited 0 without producing prose is
    an error, not a done."""
    p = summary_paths(job_dir, iteration)
    if p["md"].is_file():
        doc = {"status": "done", "mtime": p["md"].stat().st_mtime}
        cost = read_json(p["cost"])
        if isinstance(cost, dict):
            doc["summarizer"] = cost
        return doc
    run = read_json(p["run"])
    if not isinstance(run, dict):
        return {"status": "absent"}
    try:
        code = int(p["exit"].read_text().strip())
    except (OSError, ValueError):
        code = None
    if code is None:
        if pid_alive(run.get("pid")):
            return {"status": "pending", "started": run.get("started")}
        return {"status": "error", "error": "summarizer died without writing a summary"}
    return {"status": "error", "error": f"summarizer exited {code} without a summary"}


def summary_running(job_dir=None, iteration=None):
    """The one-at-a-time guard. Scans every job's summaries/ for a live run —
    cheap (a handful of small files) and it keeps an open POST from
    fanning out into arbitrarily many paid processes."""
    if not JOBS.is_dir():
        return None
    for d in sorted(JOBS.iterdir()):
        sdir = summaries_dir(d)
        if not sdir.is_dir():
            continue
        for run_file in sorted(sdir.glob("*.run.json")):
            it = run_file.name[: -len(".run.json")]
            if (sdir / f"{it}.exit").exists():
                continue
            run = read_json(run_file)
            if isinstance(run, dict) and pid_alive(run.get("pid")):
                if job_dir is not None and d == job_dir and it == iteration:
                    return {"job": d.name, "iter": it, "self": True, **run}
                return {"job": d.name, "iter": it, "self": False, **run}
    return None


def start_summarizer(job_dir, iteration):
    """Spawn the one-shot summarizer detached, in the same shape as
    start_mission's drive.sh wrapper: log file, exit file, pid recorded."""
    p = summary_paths(job_dir, iteration)
    summaries_dir(job_dir).mkdir(parents=True, exist_ok=True)
    rel = f".local/jobs/{job_dir.name}/evidence/{iteration}"
    p["prompt"].write_text(SUMMARY_PROMPT.format(rel=rel))
    for stale in ("md", "raw", "cost", "exit"):
        p[stale].unlink(missing_ok=True)
    temporary = p["md"].with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    # The role runner extracts either protocol to prose, preserves the raw
    # transcript, and writes the normalized record to the historic cost path.
    cmd = (
        f'PYTHONPATH="{ROOT / "src"}${{PYTHONPATH:+:$PYTHONPATH}}" '
        f'python3 -m agautolab.role_run summarizer --cwd "{ROOT}" --timeout 300 '
        f'--prompt-file "{p["prompt"]}" --transcript "{p["raw"]}" '
        f'--record "{p["cost"]}" >"{temporary}"; rc=$?; '
        f'if [ $rc -eq 0 ] && [ -s "{temporary}" ]; then mv "{temporary}" "{p["md"]}"; '
        f'else rm -f "{temporary}"; fi; echo $rc > "{p["exit"]}"'
    )
    log = open(p["log"], "w")
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ},
    )
    log.close()
    p["run"].write_text(
        json.dumps({"pid": proc.pid, "started": time.time(), "role": "summarizer"})
    )
    return proc.pid


def job_summary(job_dir):
    """One row per job. Never raises: an unreadable job becomes a row with an
    `error` note, because a half-written state.json must not 500 the list."""
    row = {"name": job_dir.name}
    state = read_json(job_dir / "state.json")
    if state is None:
        # A job the mediator has just written job.yaml for has no state.json
        # until the first `run-once` — that is the normal start of the
        # lifecycle, not a fault, and must not be shown as one.
        if (job_dir / "state.json").exists():
            row["error"] = "state.json unparsable"
        else:
            row["not_started"] = True
        state = {}
    row.update(
        status=state.get("status"),
        terminal=state.get("status") in TERMINAL_STATUSES,
        phase=state.get("phase"),
        awaiting_approval=state.get("status") == "awaiting_approval",
        iteration=state.get("iteration"),
        last_gate_summary=state.get("last_gate_summary"),
        state_error=state.get("error"),
    )
    row.update(job_yaml_fields(job_dir))
    iters = evidence_iters(job_dir)
    costs = [c for c in (iter_cost(job_dir, i) for i in iters) if c is not None]
    row["iterations_on_disk"] = len(iters)
    row["last_evidence"] = f"evidence/{iters[-1]}" if iters else None
    row["cost_usd"] = round(sum(costs), 6) if costs else None
    row["has_notes"] = (job_dir / "NOTES.md").is_file()
    return row


def job_detail(job_dir):
    doc = job_summary(job_dir)
    timeline = []
    for name in evidence_iters(job_dir):
        d = job_dir / "evidence" / name
        entry = {
            "iter": name,
            "files": sorted(p.name for p in d.iterdir() if p.is_file()),
            "cost_usd": iter_cost(job_dir, name),
            "mtime": d.stat().st_mtime,
        }
        result = read_json(d / "adapter_result.json")
        if isinstance(result, dict):
            entry.update(
                exit_code=result.get("exit_code"),
                timed_out=result.get("timed_out"),
                num_turns=result.get("num_turns"),
                duration_ms=result.get("duration_ms"),
                is_error=result.get("is_error"),
            )
        # Summary state, not the summary text: the list stays small, and a UI
        # knows without a probe request which iterations are already paid for.
        entry["summary"] = summary_status(job_dir, name)["status"]
        gates = read_json(d / "gates.json")
        if isinstance(gates, list):
            entry["gates"] = [
                {"command": g.get("command"), "exit_code": g.get("exit_code"),
                 "timed_out": g.get("timed_out")}
                for g in gates
                if isinstance(g, dict)
            ]
        timeline.append(entry)
    doc["evidence"] = timeline
    return doc


# --- the conversational window -------------------------------------------
#
# One free-text entrance (devpolicy/policy.md, Single Entrance). It is a
# reader, not a doer: the context it hands the model is the same job state
# the typed GETs serve, built from the helpers above rather than by
# re-walking the job dirs, and the only thing it writes is its own run
# record under .local/agent/window/.
#
# 300, not 120: a window answer may itself launch a project director's
# profile-selected one-shot, so the budget covers a nested run.
WINDOW_TIMEOUT_SECONDS = 300

WINDOW_PROMPT = """You are the conversational window of an autolab node: a
headless auto-development loop that runs coding-agent iterations against jobs,
leaving evidence (prompt, diff, gate results, cost) on disk per iteration.

You are this node's entrance, and you can start work yourself. When the user
asks for something to be built, include in your reply a block of the form

<<mission>>
...the mission text the auto-development loop should pursue...
<</mission>>

(optionally `<<mission max_sessions=N>>`, 1..50, default 12). The gateway
executes the block after you answer: it writes MISSION.md and spawns the
drive loop. One mission runs at a time — while one is alive the start is
refused and the refusal is recorded next to your answer. Only start a
mission when the user clearly asks for work, and put everything the loop
needs to know inside the block: it is the whole briefing. The block itself
is removed from the reply the user sees; the rest of your reply is shown as
written.

GUIDE is this node's capability card and JOB STATE is its live job state,
both below.

=== GUIDE ===
{guide}

=== JOB STATE (JSON) ===
{state}

=== USER MESSAGE ===
{text}
"""


# The window's mission ability (devpolicy: Tool Giving): a structured block in
# the reply that the gateway executes. Optional max_sessions attribute; the
# block body is the mission text. The tags are parsed tolerantly: live local
# models have emitted `<<mission max_sessions=20` (no `>>`) and `<</mission>`
# (one `>` short), losing whole missions to a single character. An unclosed
# block runs to the end of the reply.
MISSION_BLOCK = re.compile(
    r"<<mission(?:\s+max_sessions=(\d+))?\s*(?:>>)?\s*(.*?)\s*(?:<</mission>?>?|$)",
    re.S,
)


def apply_mission_block(reply, record):
    """Execute the reply's mission block, if any. The start's outcome goes on
    the window record under `mission`; the block is cut from the shown reply."""
    match = MISSION_BLOCK.search(reply)
    if not match:
        return reply
    max_sessions = int(match.group(1)) if match.group(1) else 12
    code, doc = start_mission(match.group(2), max_sessions)
    record["mission"] = {"status": code, **doc}
    return (reply[: match.start()] + reply[match.end() :]).strip()


def read_guide():
    """Re-read per request (cagent's llms.txt pattern): editing the card is a
    no-restart change, and a missing card must not break the window."""
    try:
        return GUIDE.read_text()
    except OSError:
        return "(no capability card is installed on this node)"


def window_state():
    """The context blob: the same job/status material the typed GETs expose,
    assembled from the same helpers — never a second read of the job dirs."""
    cur = current_run()
    jobs = sorted(d for d in JOBS.iterdir() if d.is_dir()) if JOBS.is_dir() else []
    return {
        "node_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mission": {
            "text": mission_text(STATE / "MISSION.md"),
            "driver_running": drive_running() is not None,
            "done": agent_done(),
            "current": cur,
        },
        "cost": sessions_cost(),
        "summarizer_running": summary_running(),
        "jobs": [job_summary(d) for d in jobs],
    }


# One answer at a time — the window gets the same cost/concurrency guard the summarizer
# has — here an in-process lock, because a window answer is served inside
# the request thread rather than by a detached process.
window_lock = threading.Lock()


def next_window_id():
    WINDOW.mkdir(parents=True, exist_ok=True)
    n = 1
    while (WINDOW / f"run-{n:04d}.json").exists():
        n += 1
    return n


def record_window_run(run_id, record):
    """Persist canonical identity, outcome, cost/time, and failure words."""
    WINDOW.mkdir(parents=True, exist_ok=True)
    path = WINDOW / f"run-{run_id:04d}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return path


def answer_window(text):
    """Resolve the front role, run it once, persist its canonical record."""
    run_id = next_window_id()
    started = time.monotonic()
    record = {
        "id": f"window/run-{run_id:04d}",
        "started": time.time(),
        "question": text,
        "outcome": "failed",
    }
    try:
        prompt = WINDOW_PROMPT.format(
            guide=read_guide(),
            state=json.dumps(window_state(), indent=2, default=str),
            text=text,
        )
        reply, meta, code = run_role(
            "front", prompt, cwd=ROOT, timeout=WINDOW_TIMEOUT_SECONDS,
            transcript=WINDOW / f"run-{run_id:04d}.agent.jsonl",
        )
    except AgentConfigError as error:
        record["failure"] = str(error)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        record_window_run(run_id, record)
        return record
    record.update(meta)
    if code == 0 and record.get("outcome") == "done":
        record["reply"] = apply_mission_block(reply, record)
    record_window_run(run_id, record)
    return record


class Handler(BaseHTTPRequestHandler):
    server_version = "autolab-gateway/1"

    def send_json(self, code, obj):
        body = (json.dumps(obj, indent=2) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/guide":
            return self.send_text(200, read_guide())
        if path == "/game" or path.startswith("/game/"):
            return self.serve_game(path)
        if path == "/monitor" or path.startswith("/monitor/"):
            return self.serve_static(MONITOR, path[len("/monitor"):])
        if path == "/status":
            return self.get_status()
        if path == "/log":
            return self.get_log()
        if path == "/projects":
            return self.send_json(200, projects_document())
        if path == "/jobs" or path.startswith("/jobs/"):
            return self.get_jobs(path)
        self.send_json(404, {"error": "unknown route"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/window":
            return self.post_window()
        if path.startswith("/jobs/"):
            return self.post_summarize(path)
        self.send_json(404, {"error": "unknown route"})

    def post_window(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            text = req["text"]
            assert isinstance(text, str) and text.strip()
        except Exception:
            return self.send_json(400, {"error": 'body must be {"text": "..."}'})
        if not window_lock.acquire(blocking=False):
            return self.send_json(
                409, {"error": "the window is already answering someone"}
            )
        try:
            record = answer_window(text.strip())
        finally:
            window_lock.release()
        # The record is the response: a caller sees which harness answered and
        # what it cost without a second request; a failed harness returns its
        # own words rather than a silent empty reply.
        body = {"kind": KIND, "type": "window", **record}
        self.send_json(200 if record["outcome"] == "done" else 502, body)

    def get_status(self):
        cur = current_run()
        exit_code = None
        if cur:
            try:
                exit_code = int((GATEWAY / f"run-{cur['run']:04d}.exit").read_text())
            except (OSError, ValueError):
                pass
        # "started" is absent in current-files written before run scoping
        # existed; fall back to the old everything-on-disk view then.
        started = cur.get("started") if cur else None
        mission = STATE / "MISSION.md"
        game = game_info(started)
        self.send_json(
            200,
            {
                "kind": KIND,
                "type": "status",
                "cost": sessions_cost(),
                "mission": mission_text(mission),
                "driver": {
                    "running": drive_running() is not None,
                    "current": cur,
                    "exit_code": exit_code,
                },
                "done": agent_done(),
                "notes": agent_notes(),
                "sessions": session_summaries(since=started),
                "sessions_total_on_disk": sessions_on_disk(),
                "game": game,
                "game_served": game["installed"],
            },
        )

    def get_log(self):
        cur = current_run()
        if not cur:
            return self.send_json(404, {"error": "no run yet"})
        m = re.search(r"tail=(\d+)", self.path)
        tail = int(m.group(1)) if m else 100
        try:
            lines = (GATEWAY / f"run-{cur['run']:04d}.log").read_text().splitlines()
        except OSError:
            return self.send_json(404, {"error": "log missing"})
        body = ("\n".join(lines[-tail:]) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_jobs(self, path):
        parts = [p for p in path.split("/") if p][1:]  # drop "jobs"
        if not parts:
            jobs = sorted(d for d in JOBS.iterdir() if d.is_dir()) if JOBS.is_dir() else []
            return self.send_json(
                200,
                {"kind": KIND, "type": "jobs", "jobs": [job_summary(d) for d in jobs]},
            )
        name = parts[0]
        if not SAFE_NAME.match(name):
            return self.send_json(400, {"error": "bad job name"})
        job_dir = JOBS / name
        if not job_dir.is_dir():
            return self.send_json(404, {"error": f"no such job: {name}"})
        if len(parts) == 1:
            return self.send_json(
                200, {"kind": KIND, "type": "job", "job": job_detail(job_dir)}
            )
        if len(parts) == 4 and parts[1] == "evidence":
            return self.serve_evidence(job_dir, parts[2], parts[3])
        if len(parts) == 3 and parts[1] == "summarize":
            return self.get_summary(job_dir, parts[2])
        self.send_json(404, {"error": "unknown route"})

    def get_summary(self, job_dir, iteration):
        if not ITER_NAME.match(iteration):
            return self.send_json(400, {"error": "bad iteration name"})
        doc = summary_status(job_dir, iteration)
        if doc["status"] == "done":
            try:
                doc["summary"] = summary_paths(job_dir, iteration)["md"].read_text()
            except OSError:
                doc = {"status": "error", "error": "summary file unreadable"}
        self.send_json(
            200,
            {"kind": KIND, "type": "summary", "job": job_dir.name,
             "iter": iteration, **doc},
        )

    def post_summarize(self, path):
        parts = [p for p in path.split("/") if p][1:]  # drop "jobs"
        if len(parts) != 3 or parts[1] != "summarize":
            return self.send_json(404, {"error": "unknown route"})
        name, iteration = parts[0], parts[2]
        if not SAFE_NAME.match(name) or not ITER_NAME.match(iteration):
            return self.send_json(400, {"error": "bad job or iteration name"})
        job_dir = JOBS / name
        if not (job_dir / "evidence" / iteration).is_dir():
            return self.send_json(
                404, {"error": f"no such iteration: {name}/{iteration}"}
            )
        force = re.search(r"[?&]force=1\b", self.path) is not None
        state = summary_status(job_dir, iteration)
        if state["status"] == "done" and not force:
            return self.get_summary(job_dir, iteration)
        if state["status"] == "pending":
            return self.send_json(
                202, {"kind": KIND, "type": "summary", "job": name,
                      "iter": iteration, "status": "pending", "started": True},
            )
        busy = summary_running()
        if busy:
            return self.send_json(
                409, {"error": "a summarizer is already running", "current": busy}
            )
        pid = start_summarizer(job_dir, iteration)
        self.send_json(
            202,
            {"kind": KIND, "type": "summary", "job": name, "iter": iteration,
             "status": "pending", "started": True, "pid": pid},
        )

    def serve_evidence(self, job_dir, iteration, filename):
        if not ITER_NAME.match(iteration) or not SAFE_NAME.match(filename):
            return self.send_json(400, {"error": "bad evidence path"})
        base = (job_dir / "evidence").resolve()
        target = (base / iteration / filename).resolve()
        # Same containment guard serve_game uses: names are already
        # pattern-checked, this catches anything a symlink could still do.
        if not str(target).startswith(str(base) + os.sep):
            return self.send_json(403, {"error": "path escapes evidence dir"})
        if not target.is_file():
            return self.send_json(404, {"error": "no such evidence file"})
        ctype = (
            "application/json"
            if target.suffix == ".json"
            else "text/plain; charset=utf-8"
        )
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_game(self, path):
        return self.serve_static(
            SERVE, path[len("/game"):], missing="not found (game not installed yet?)"
        )

    def serve_static(self, base, rel, missing="not found"):
        rel = rel.lstrip("/") or "index.html"
        target = (base / rel).resolve()
        if not str(target).startswith(str(base.resolve()) + os.sep) and target != base.resolve():
            return self.send_json(403, {"error": "path escapes served dir"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self.send_json(404, {"error": missing})
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    host = os.environ.get("AUTOLAB_GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("AUTOLAB_GATEWAY_PORT", "8791"))
    for d in (GATEWAY, SERVE, STATE / "sessions"):
        d.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # auto-reap drive.sh wrappers
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"autolab-gateway listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
