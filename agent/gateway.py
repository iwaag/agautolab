#!/usr/bin/env python3
"""Mission gateway: submit missions to the autolab agent over HTTP, no SSH.

Stdlib-only single-file server. Routes:

  POST /mission   {"mission": str, "max_sessions": int?}  -> start drive.sh
  GET  /status    mission/driver/NOTES/session summary, scoped to the
                  current run (sessions started before it are excluded;
                  "game" says whether the served build is from this run)
  GET  /log       ?tail=N  tail of the current (or last) drive log
  GET  /jobs      one summary row per .local/jobs/<job>/
  GET  /jobs/<job>              status document + cost rollup + evidence timeline
  GET  /jobs/<job>/evidence/<iter>/<file>   raw evidence file passthrough
  POST /jobs/<job>/summarize/<iter>  ?force=1  start a one-shot summarizer
  GET  /jobs/<job>/summarize/<iter>  {status: absent|pending|done|error, summary?}
  GET  /game/...  static files from .local/agent/serve/ (unauthenticated)
  GET  /healthz   liveness probe (unauthenticated)

Only POST /mission requires `Authorization: Bearer <token>` matching
.local/agent/gateway_token. Every GET is unauthenticated: this is an
experimental node and the read side is deliberately thin-auth until auth is
designed system-wide. POST /jobs/.../summarize/... is unauthenticated too even
though it spends money: accepted for this phase, bounded by a one-at-a-time
guard and by the per-iteration cache (one paid call per iteration ever).

Monitoring reads never write and never take a job's `.lock`, so they are safe
against a live iteration; half-written JSON degrades to an `error` field on
the affected row instead of a 500.

One mission at a time: POST /mission returns 409 while drive.sh is alive.
State lives under .local/agent/ next to the rest of the agent layer:

  gateway_token          bearer token (0600, provisioned by ansible)
  serve/                 static dir the finished game is installed into
  gateway/run-NNNN.log   drive.sh combined output per accepted mission
  gateway/run-NNNN.exit  drive.sh exit code, written when it finishes
  gateway/current        run id + pid + start time of the active (or last) drive
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".local" / "agent"
GATEWAY = STATE / "gateway"
SERVE = STATE / "serve"
TOKEN_FILE = STATE / "gateway_token"
JOBS = ROOT / ".local" / "jobs"
MONITOR = Path(__file__).resolve().parent / "monitor"

# Versioned envelope, in the spirit of nctl's `nctl.drift.v1`: scope 3 points
# agdevworld at this same feed, and the kind is what keeps that cheap.
KIND = "autolab.monitor.v1"

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


def read_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return None


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


def notes_are_stale():
    """NOTES.md older than MISSION.md is the previous mission's, not this one's."""
    notes = STATE / "NOTES.md"
    mission = STATE / "MISSION.md"
    return (
        notes.is_file()
        and mission.is_file()
        and mission.stat().st_mtime > notes.stat().st_mtime
    )


def notes_status():
    notes = STATE / "NOTES.md"
    if not notes.is_file():
        return "STATUS: (no notes)"
    if notes_are_stale():
        return "STATUS: (stale notes, predates mission)"
    with notes.open() as f:
        return f.readline().strip() or "STATUS: (empty notes)"


def mission_headline(path):
    """The mission's first paragraph of substance, on one line.

    Missions are markdown: they open with a `# Mission` heading that tells a
    human nothing, and their prose is hard-wrapped, so the literal first line
    stops mid-sentence. Skip headings, then join until the blank line.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    para = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            if para:
                break
            continue
        para.append(line)
    return " ".join(para) or None


def devstyle_report():
    """The 3-line devstyle report every final mission report must answer
    (`Style chosen / Why / Was it right in hindsight`, see styles/*/STYLE.md).
    It is an ENT asset, so the monitor surfaces it whenever NOTES.md has it —
    except when NOTES.md predates the mission, where the report on disk is the
    previous mission's and showing it under this one's headline would lie."""
    if notes_are_stale():
        return None
    try:
        text = (STATE / "NOTES.md").read_text()
    except OSError:
        return None
    keys = {
        "style chosen": "style_chosen",
        "why": "why",
        "was it right in hindsight": "hindsight",
    }
    label = re.compile(r"^\s*[-*]?\s*([^:]{1,40}):\s*(.*)$")
    out = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = label.match(line)
        if not m or m.group(1).strip().lower() not in keys:
            continue
        # The answers are prose in a hard-wrapped file, so a one-line read
        # truncates them mid-sentence. Absorb the continuation lines: anything
        # up to the next blank line, bullet, heading or `Key:` line.
        parts = [m.group(2).strip()]
        for nxt in lines[i + 1:]:
            stripped = nxt.strip()
            nm = label.match(nxt)
            if (
                not stripped
                or stripped[0] in "-*#"
                or (nm and nm.group(1).strip().lower() in keys)
            ):
                break
            parts.append(stripped)
        out[keys[m.group(1).strip().lower()]] = " ".join(p for p in parts if p)
    return out if "style_chosen" in out else None


def session_summaries(since=None):
    # since: epoch seconds; only sessions written at/after it are returned,
    # so /status can report the current run instead of every run ever.
    out = []
    sessions = STATE / "sessions"
    for p in sorted(sessions.glob("session-*.json")):
        if since is not None and p.stat().st_mtime < since - 1:
            continue
        row = {"file": p.name}
        try:
            d = json.loads(p.read_text())
            row.update(
                is_error=d.get("is_error"),
                turns=d.get("num_turns"),
                cost_usd=d.get("total_cost_usd"),
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
    return len(list((STATE / "sessions").glob("session-*.json")))


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
    for p in (STATE / "sessions").glob("session-*.json"):
        try:
            c = json.loads(p.read_text()).get("total_cost_usd")
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
        m = re.match(r"^(adapter|max_iterations|push):\s*(\S+)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip("\"'")
    return _job_fields(out)


def _job_fields(doc):
    """Keep the shown job.yaml fields in their expected types, whichever
    parser produced them — the page renders `iteration / max` arithmetically."""
    out = {}
    if isinstance(doc.get("adapter"), str):
        out["adapter"] = doc["adapter"]
    try:
        out["max_iterations"] = int(doc["max_iterations"])
    except (KeyError, TypeError, ValueError):
        pass
    push = doc.get("push")
    if push is not None:
        out["push"] = push if isinstance(push, bool) else str(push).lower() == "true"
    return out


def evidence_iters(job_dir):
    d = job_dir / "evidence"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and ITER_NAME.match(p.name))


def iter_cost(job_dir, name):
    """Per-iteration cost. adapter_result.json is the adapter's own record;
    claude_output.json is the raw agent JSON and carries the same number."""
    for fname in ("adapter_result.json", "claude_output.json"):
        doc = read_json(job_dir / "evidence" / name / fname)
        if isinstance(doc, dict) and isinstance(
            doc.get("total_cost_usd"), (int, float)
        ):
            return doc["total_cost_usd"]
    return None


# --- iteration summaries -------------------------------------------------
#
# An iteration's evidence is summarized where it lives: a one-shot `claude -p`
# reads .local/jobs/<job>/evidence/<iter>/ and writes prose next to it under
# summaries/. Callers outside this node get the prose, never the raw files —
# that boundary is the point of the feature, not an implementation detail.
#
# Everything this path writes stays inside summaries/: it never touches
# state.json, evidence, MISSION.md, NOTES.md or the job's .lock, so it is safe
# to run against an iteration that is still being written (such a summary is
# allowed to be wrong; `?force=1` regenerates it).

SUMMARY_ALLOWED_TOOLS = "Read,Glob,Grep"

SUMMARY_PROMPT = """You are a one-shot summarizer for an autolab job iteration.

Read only the files in the directory `{rel}` (relative to the current working
directory). Do not read, write or modify anything else, and do not run
commands. That directory holds the evidence of one coding-agent iteration:

- `prompt.txt`      what the coding agent was asked to do
- `diff.patch`      what it actually changed
- `gates.json`      the verification commands and their exit codes
- `adapter_result.json` turns, duration, exit code, cost in USD
- `error.txt`       present only when the iteration failed
- other files may be present; read what helps.

Write 5 to 10 sentences of plain prose for a human who is watching this job
and has not seen the files. Cover: what the iteration was asked to do, what
changed, which gates ran and which failed, whether it errored, and what it
cost in turns/time/dollars. Be concrete (name files, gate commands and
numbers) but do not dump file contents, diffs or JSON. No headings, no bullet
lists, no preamble such as "Here is the summary" — output the prose only.
Do not narrate your reading process: your final message must begin with the
first sentence of the summary itself.
"""

# The model still opens with a line of narration often enough to matter, and
# the summary is shown to the user unabridged, so drop a leading one-line
# paragraph that is clearly throat-clearing rather than content.
NARRATION = re.compile(
    r"^(now|ok|okay|alright|right|good|let me|i(?:'ve| have)? |here(?:'s| is)|"
    r"based on|i'll|i will)",
    re.IGNORECASE,
)


def tidy_summary(text):
    text = text.strip()
    head, sep, rest = text.partition("\n\n")
    if sep and "\n" not in head and len(head) < 200 and NARRATION.match(head):
        return rest.strip()
    return text


# Promotion runs in a separate interpreter so the wrapper stays a one-liner:
# claude's JSON goes in, prose comes out only when the run really succeeded,
# and the summarizer's own cost is recorded — an unauthenticated route that
# spends money should at least say how much.
EXTRACT_PY = r"""
import json, pathlib, sys
raw, md, cost, tidy = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, tidy)
from gateway import tidy_summary
try:
    doc = json.loads(pathlib.Path(raw).read_text())
except Exception as exc:
    sys.exit(f"summarizer output was not JSON: {exc}")
if doc.get("is_error"):
    sys.exit(f"summarizer reported an error: {doc.get('subtype')}")
text = tidy_summary(str(doc.get("result") or ""))
if not text:
    sys.exit("summarizer produced no text")
pathlib.Path(md).write_text(text + "\n")
pathlib.Path(cost).write_text(json.dumps({
    "cost_usd": doc.get("total_cost_usd"),
    "num_turns": doc.get("num_turns"),
    "duration_ms": doc.get("duration_ms"),
}))
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


def claude_bin():
    """Same resolution order as session.sh: env, then the .local pointer file,
    then PATH. The gateway may run without the developer's interactive PATH."""
    env = os.environ.get("AUTOLAB_CLAUDE_BIN")
    if env:
        return env
    try:
        line = (STATE / "claude_bin").read_text().strip()
        if line:
            return line
    except OSError:
        pass
    return "claude"


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
    cheap (a handful of small files) and it keeps an unauthenticated POST from
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
    POST /mission's drive.sh wrapper: log file, exit file, pid recorded."""
    p = summary_paths(job_dir, iteration)
    summaries_dir(job_dir).mkdir(parents=True, exist_ok=True)
    rel = f".local/jobs/{job_dir.name}/evidence/{iteration}"
    p["prompt"].write_text(SUMMARY_PROMPT.format(rel=rel))
    for stale in ("md", "raw", "cost", "exit"):
        p[stale].unlink(missing_ok=True)
    model = os.environ.get("AUTOLAB_SUMMARY_MODEL", "claude-sonnet-5")
    # claude's JSON lands in .raw.json; the extractor promotes it to .md only
    # on a clean, non-empty, non-error run, so a failed summarizer can never
    # be served as a cached summary.
    agent_dir = Path(__file__).resolve().parent
    cmd = (
        f'"$BIN" -p --output-format json --model "$MODEL" '
        f'--allowedTools "$TOOLS" <"{p["prompt"]}" >"{p["raw"]}"; rc=$?; '
        f'if [ $rc -eq 0 ]; then python3 -c "$EXTRACT" "{p["raw"]}" "{p["md"]}" '
        f'"{p["cost"]}" "{agent_dir}" || rc=$?; fi; echo $rc > "{p["exit"]}"'
    )
    log = open(p["log"], "w")
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "BIN": claude_bin(), "MODEL": model,
             "TOOLS": SUMMARY_ALLOWED_TOOLS, "EXTRACT": EXTRACT_PY},
    )
    log.close()
    p["run"].write_text(
        json.dumps({"pid": proc.pid, "started": time.time(), "model": model})
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
        consecutive_no_progress=state.get("consecutive_no_progress"),
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


class Handler(BaseHTTPRequestHandler):
    server_version = "autolab-gateway/1"

    def send_json(self, code, obj):
        body = (json.dumps(obj, indent=2) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        token = read_token()
        if not token:
            self.send_json(500, {"error": "gateway_token missing on server"})
            return False
        got = self.headers.get("Authorization", "")
        if got == f"Bearer {token}":
            return True
        self.send_json(401, {"error": "missing or wrong bearer token"})
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/game" or path.startswith("/game/"):
            return self.serve_game(path)
        if path == "/monitor" or path.startswith("/monitor/"):
            return self.serve_static(MONITOR, path[len("/monitor"):])
        if path == "/status":
            return self.get_status()
        if path == "/log":
            return self.get_log()
        if path == "/jobs" or path.startswith("/jobs/"):
            return self.get_jobs(path)
        self.send_json(404, {"error": "unknown route"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/jobs/"):
            return self.post_summarize(path)
        if path != "/mission":
            return self.send_json(404, {"error": "unknown route"})
        if not self.authorized():
            return
        running = drive_running()
        if running:
            return self.send_json(
                409, {"error": "a mission is already running", "current": running}
            )
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            mission = req["mission"]
            assert isinstance(mission, str) and mission.strip()
        except Exception:
            return self.send_json(400, {"error": 'body must be {"mission": "..."}'})
        max_sessions = req.get("max_sessions", 12)
        if not (isinstance(max_sessions, int) and 1 <= max_sessions <= 50):
            return self.send_json(400, {"error": "max_sessions must be 1..50"})

        GATEWAY.mkdir(parents=True, exist_ok=True)
        run = 1
        while (GATEWAY / f"run-{run:04d}.log").exists():
            run += 1
        (STATE / "MISSION.md").write_text(mission)
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
        self.send_json(202, {"accepted": True, "run": run, "pid": proc.pid})

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
                "mission_headline": mission_headline(mission),
                "driver": {
                    "running": drive_running() is not None,
                    "current": cur,
                    "exit_code": exit_code,
                },
                "notes_status": notes_status(),
                "devstyle": devstyle_report(),
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
    if not read_token():
        sys.exit(f"refusing to start: {TOKEN_FILE} is missing or empty")
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # auto-reap drive.sh wrappers
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"autolab-gateway listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
