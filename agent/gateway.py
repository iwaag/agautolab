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
  GET  /game/...  static files from .local/agent/serve/ (unauthenticated)
  GET  /healthz   liveness probe (unauthenticated)

Only POST /mission requires `Authorization: Bearer <token>` matching
.local/agent/gateway_token. Every GET is unauthenticated: this is an
experimental node and the read side is deliberately thin-auth until auth is
designed system-wide.

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


def notes_status():
    notes = STATE / "NOTES.md"
    mission = STATE / "MISSION.md"
    if not notes.is_file():
        return "STATUS: (no notes)"
    if mission.is_file() and mission.stat().st_mtime > notes.stat().st_mtime:
        return "STATUS: (stale notes, predates mission)"
    with notes.open() as f:
        return f.readline().strip() or "STATUS: (empty notes)"


def mission_first_line(path):
    """First line of substance. Missions are written as markdown and usually
    open with a `# Mission` heading, which says nothing to a human watching."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        return None
    return None


def devstyle_report():
    """The 3-line devstyle report every final mission report must answer
    (`Style chosen / Why / Was it right in hindsight`, see styles/*/STYLE.md).
    It is an ENT asset, so the monitor surfaces it whenever NOTES.md has it."""
    try:
        text = (STATE / "NOTES.md").read_text()
    except OSError:
        return None
    keys = {
        "style chosen": "style_chosen",
        "why": "why",
        "was it right in hindsight": "hindsight",
    }
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]?\s*([^:]{1,40}):\s*(.*)$", line)
        if m and m.group(1).strip().lower() in keys:
            out[keys[m.group(1).strip().lower()]] = m.group(2).strip()
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
            row["is_error"] = "unparsed"
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


def job_summary(job_dir):
    """One row per job. Never raises: an unreadable job becomes a row with an
    `error` note, because a half-written state.json must not 500 the list."""
    row = {"name": job_dir.name}
    state = read_json(job_dir / "state.json")
    if state is None:
        row["error"] = "state.json missing or unparsable"
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
        if path == "/status":
            return self.get_status()
        if path == "/log":
            return self.get_log()
        if path == "/jobs" or path.startswith("/jobs/"):
            return self.get_jobs(path)
        self.send_json(404, {"error": "unknown route"})

    def do_POST(self):
        if self.path.split("?")[0] != "/mission":
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
                "mission_first_line": mission_first_line(mission),
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
        self.send_json(404, {"error": "unknown route"})

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
        rel = path[len("/game"):].lstrip("/") or "index.html"
        target = (SERVE / rel).resolve()
        if not str(target).startswith(str(SERVE.resolve()) + os.sep) and target != SERVE.resolve():
            return self.send_json(403, {"error": "path escapes serve dir"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self.send_json(404, {"error": "not found (game not installed yet?)"})
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
