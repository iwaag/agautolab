#!/usr/bin/env python3
"""Mission gateway: submit missions to the autolab agent over HTTP, no SSH.

Stdlib-only single-file server. Routes:

  POST /mission   {"mission": str, "max_sessions": int?}  -> start drive.sh
  GET  /status    mission/driver/NOTES/session summary
  GET  /log       ?tail=N  tail of the current (or last) drive log
  GET  /game/...  static files from .local/agent/serve/ (unauthenticated)
  GET  /healthz   liveness probe (unauthenticated)

Every route except /game/ and /healthz requires
`Authorization: Bearer <token>` matching .local/agent/gateway_token.

One mission at a time: POST /mission returns 409 while drive.sh is alive.
State lives under .local/agent/ next to the rest of the agent layer:

  gateway_token          bearer token (0600, provisioned by ansible)
  serve/                 static dir the finished game is installed into
  gateway/run-NNNN.log   drive.sh combined output per accepted mission
  gateway/run-NNNN.exit  drive.sh exit code, written when it finishes
  gateway/current        run id + pid of the active (or last) drive
"""

import json
import os
import re
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".local" / "agent"
GATEWAY = STATE / "gateway"
SERVE = STATE / "serve"
TOKEN_FILE = STATE / "gateway_token"

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


def session_summaries():
    out = []
    sessions = STATE / "sessions"
    for p in sorted(sessions.glob("session-*.json")):
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
        if not self.authorized():
            return
        if path == "/status":
            return self.get_status()
        if path == "/log":
            return self.get_log()
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
            json.dumps({"run": run, "pid": proc.pid, "max_sessions": max_sessions})
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
        mission = STATE / "MISSION.md"
        self.send_json(
            200,
            {
                "mission_first_line": (
                    mission.read_text().splitlines()[0] if mission.is_file() else None
                ),
                "driver": {
                    "running": drive_running() is not None,
                    "current": cur,
                    "exit_code": exit_code,
                },
                "notes_status": notes_status(),
                "sessions": session_summaries(),
                "game_served": (SERVE / "index.html").is_file(),
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
