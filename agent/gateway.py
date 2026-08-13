#!/usr/bin/env python3
"""Autolab node gateway: a real conversational window over a retained stub surface.

Stdlib-only single-file server. Routes, unchanged from the implementation
this replaces:

  POST /window    {"text": str}  -> the front role
  GET  /status    mission/driver/done/session summary
  GET  /log       ?tail=N  tail of the current (or last) drive log
  GET  /jobs      one summary row per job
  GET  /projects  project role-profile selections and available profiles
  GET  /jobs/<job>              status document + cost rollup + evidence timeline
  GET  /jobs/<job>/evidence/<iter>/<file>   raw evidence file passthrough
  POST /jobs/<job>/summarize/<iter>  ?force=1  start a one-shot summarizer
  GET  /jobs/<job>/summarize/<iter>  {status: absent|pending|done|error, summary?}
  GET  /game/...  static files from .local/agent/serve/
  GET  /healthz   liveness probe

The auto-development loop behind these routes was deleted (the
`discard_garbage` episode): there are no jobs, no evidence, no missions, and
no summarizer. The route table, the request validation, the status vocabulary
(400 bad input, 404 unknown, 409 busy, 202 accepted) and the response
envelopes are kept, because the surface is the part of this node that was
worth keeping. Job-shaped routes answer with empty documents rather than
absent keys, so `agdevworld/assistant`, which proxies them at
`/api/autolab/<node>/…`, keeps working against an empty node instead of a
broken one. Every stub document carries `"stub": true`.

Two things are real:

  GET  /projects  reads agents.toml, the ignored .local/agents.local.toml
                  overlay, and each project's own selection.
  POST /window    runs the `front` role for real and records its answer,
                  identity, outcome, cost and timing.

POST /window remains this node's single desire-accepting conversational
entrance. It passes the caller's text through without a capability card or a
gateway-owned mission protocol; the front workspace's tools are its evidence.

No route carries authentication: this node serves a single-user experimental
cluster.

State written under .local/agent/:

  window/run-NNNN.json   one record per window answer (devpolicy/agent_records.md)

It is the only thing this server writes.
"""

import json
import os
import re
import signal
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
WINDOW = STATE / "window"

# Versioned envelope, in the spirit of nctl's `nctl.drift.v1`: scope 3 points
# agdevworld at this same feed, and the kind is what keeps that cheap.
KIND = "autolab.monitor.v1"
PROJECTS_KIND = "autolab.projects.v1"

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ITER_NAME = re.compile(r"^iter-\d+$")

STUB_LOG = (
    "this node is a stub: the drive loop was removed, so there is no drive "
    "log to tail\n"
)


def projects_document():
    """Return every local project with its effective selectable role profiles.

    Real, and the reason the resolution layer was kept alive: this is where a
    broken `agents.toml` or a bad project selection becomes visible from
    outside the node. A broken project file is represented on that project's
    row; other project rows remain useful while a human or agent is between
    valid edits.
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


def status_document():
    """The shape /status has always had, emptied. Zeroes and empty lists, not
    absent keys: a consumer reading `cost.sessions_usd` must find a number."""
    return {
        "kind": KIND,
        "type": "status",
        "stub": True,
        "cost": {"sessions_usd": 0.0, "current_run_sessions_usd": None},
        "mission": None,
        "driver": {"running": False, "current": None, "exit_code": None},
        "done": None,
        "notes": None,
        "sessions": [],
        "sessions_total_on_disk": 0,
        "game": {"installed": False, "installed_mtime": None,
                 "installed_this_run": False},
        "game_served": False,
    }


# --- the conversational window -------------------------------------------
# One free-text entrance (devpolicy/policy.md, Single Entrance).
WINDOW_TIMEOUT_SECONDS = 300


# One answer at a time, as before: the guard is part of the entrance's
# contract, and a caller that handles the 409 must keep being able to see it.
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
    """Pass text to the front role unchanged and persist its run record."""
    run_id = next_window_id()
    started = time.monotonic()
    record = {
        "id": f"window/run-{run_id:04d}",
        "started": time.time(),
        "question": text,
        "outcome": "failed",
    }
    try:
        reply, meta, code = run_role(
            "front", text, cwd=ROOT / "agent" / "front", timeout=WINDOW_TIMEOUT_SECONDS,
        )
    except AgentConfigError as error:
        record["failure"] = str(error)
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        record_window_run(run_id, record)
        return record
    record.update(meta)
    if code == 0 and record.get("outcome") == "done":
        record["reply"] = reply
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
        if path == "/game" or path.startswith("/game/"):
            return self.send_json(404, {"error": "no game is served by a stub node"})
        if path == "/monitor" or path.startswith("/monitor/"):
            return self.send_json(404, {"error": "the monitor page was removed"})
        if path == "/status":
            return self.send_json(200, status_document())
        if path == "/log":
            return self.send_text(200, STUB_LOG)
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
        # what it cost without a second request.
        body = {"kind": KIND, "type": "window", **record}
        self.send_json(200 if record["outcome"] == "done" else 502, body)

    def get_jobs(self, path):
        parts = [p for p in path.split("/") if p][1:]  # drop "jobs"
        if not parts:
            return self.send_json(
                200, {"kind": KIND, "type": "jobs", "stub": True, "jobs": []}
            )
        name = parts[0]
        if not SAFE_NAME.match(name):
            return self.send_json(400, {"error": "bad job name"})
        if len(parts) == 1:
            return self.send_json(404, {"error": f"no such job: {name}"})
        if len(parts) == 4 and parts[1] == "evidence":
            iteration, filename = parts[2], parts[3]
            if not ITER_NAME.match(iteration) or not SAFE_NAME.match(filename):
                return self.send_json(400, {"error": "bad evidence path"})
            return self.send_json(404, {"error": "no such evidence file"})
        if len(parts) == 3 and parts[1] == "summarize":
            return self.get_summary(name, parts[2])
        self.send_json(404, {"error": "unknown route"})

    def get_summary(self, name, iteration):
        if not ITER_NAME.match(iteration):
            return self.send_json(400, {"error": "bad iteration name"})
        self.send_json(
            200,
            {"kind": KIND, "type": "summary", "stub": True, "job": name,
             "iter": iteration, "status": "absent"},
        )

    def post_summarize(self, path):
        parts = [p for p in path.split("/") if p][1:]  # drop "jobs"
        if len(parts) != 3 or parts[1] != "summarize":
            return self.send_json(404, {"error": "unknown route"})
        name, iteration = parts[0], parts[2]
        if not SAFE_NAME.match(name) or not ITER_NAME.match(iteration):
            return self.send_json(400, {"error": "bad job or iteration name"})
        # 404, not 202: there is no evidence directory to summarize, and the
        # route's own contract is that an unknown iteration is a 404. A 202
        # here would promise prose that never arrives.
        self.send_json(404, {"error": f"no such iteration: {name}/{iteration}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    host = os.environ.get("AUTOLAB_GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("AUTOLAB_GATEWAY_PORT", "8791"))
    WINDOW.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"autolab-gateway listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
