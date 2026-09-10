#!/usr/bin/env python3
"""Autolab node gateway: this node's conversational window.

Stdlib-only single-file server. Two routes:

  POST /window    {"text": str}  -> the front role
  GET  /healthz   liveness probe

The auto-development loop was deleted (`discard_garbage`), and until
`refactor` p3 ex1 the gateway kept a **stub** surface over its grave —
`/status`, `/log`, `/jobs…`, `/projects`, `/game`, `/monitor` — answering with
empty documents marked `"stub": true`. That existed for one caller,
`agdevworld`'s `autolab / now` view, which proxied it at
`/api/autolab/<node>/…`. The proxy itself had been deleted a month earlier
(`modernize_agdevworld` p1) and the view was deleted in p3 ex1, so the stub
answered nobody and is gone.

POST /window remains this node's single desire-accepting conversational
entrance. It passes the caller's text through without a capability card or a
gateway-owned mission protocol; the front workspace's tools are its evidence.
It is *this node's* window, not the agent's entrance — development work is
asked for in autolab's Zulip channel, which its listener owns.

No route carries authentication: this node serves a single-user experimental
cluster.

State written under .local/agent/:

  window/run-NNNN.json   one record per window answer (devpolicy/agent_records.md)

It is the only thing this server writes.
"""

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agag.agent_config import AgentConfigError  # noqa: E402
from agautolab.role_run import run_role  # noqa: E402

STATE = ROOT / ".local" / "agent"
WINDOW = STATE / "window"

# Versioned envelope, in the spirit of nctl's `nctl.drift.v1`: the window's
# answer says what it is, and a reader keys off the kind rather than the shape.
KIND = "autolab.monitor.v1"


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

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "unknown route"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/window":
            return self.post_window()
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
