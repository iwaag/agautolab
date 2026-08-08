# autolab agent

The agent layer on top of the `autolab` toolbelt: a mission goes in, the
agent plans it, seeds jobs, drives coding agents through `autolab`, verifies,
and declares done. Policy is Tool Arming — the only fixed rules live in
[CHARTER.md](CHARTER.md); everything else is the agent's judgment.

## Layout

Tracked (this directory): `CHARTER.md` (the system prompt/contract),
`session.sh` (one headless session), `drive.sh` (re-invoke until done).

Runtime state (local-only, under `../.local/agent/`):

```
.local/agent/
  MISSION.md            # the only external input — write the mission here
  NOTES.md              # agent-owned continuity; first line STATUS: working|complete|blocked
  claude_bin            # absolute path to the claude binary (one line)
  sessions/session-NNNN.json   # full claude output per session (cost/turns evidence)
```

## Run

```bash
echo "/path/to/claude" > .local/agent/claude_bin   # once
$EDITOR .local/agent/MISSION.md                    # the mission
agent/drive.sh [max_sessions]                      # default 12; exits 0 complete, 20 blocked, 10 budget
```

`AUTOLAB_AGENT_MODEL` overrides the agent model (default `claude-sonnet-5`);
`AUTOLAB_CLAUDE_BIN` overrides the binary. Sessions are never resumed — each
one reconstructs context from MISSION + NOTES + `autolab status`, the same
philosophy as `run-once` one level down.

## Remote missions (gateway)

`agent/gateway.py` is a stdlib-only HTTP front door so a mission can be
submitted and watched without SSH. It refuses to start without a bearer token
in `.local/agent/gateway_token`. Routes (default `:8791`):

- `POST /mission` `{"mission": "...", "max_sessions": 12}` — writes
  `MISSION.md` and launches `drive.sh` detached; `409` while one is running.
  **The only authenticated route.**
- `GET /status` — driver liveness/exit, mission headline, `NOTES.md` STATUS
  line, the devstyle 3-line report when NOTES carries it, per-session cost
  summaries, cumulative cost, whether a game build is installed
- `GET /log?tail=N` — tail of the current drive log
- `GET /jobs` — one summary row per `.local/jobs/<job>/`: status, iteration,
  gates, cost rollup, latest evidence dir
- `GET /jobs/<job>` — the same row plus the `evidence/iter-NNNN/` timeline
  (per-iteration cost, turns, duration, exit code, gate results, file list)
- `GET /jobs/<job>/evidence/<iter>/<file>` — the raw evidence file
- `GET /monitor/` — the human monitoring page (see below)
- `GET /game/` — static serving of `.local/agent/serve/` (a mission that ships
  a browser game should install its verified build there)
- `GET /healthz` — liveness probe

Every `GET` is unauthenticated. That is deliberate for this experimental
node — auth will be designed system-wide in a later phase.

Read routes never write and never take a job's `.lock`, so they are safe to
poll against a live iteration. Unreadable or half-written files degrade to a
row carrying an `error` note instead of failing the request. JSON responses
carry a `"kind": "autolab.monitor.v1"` envelope.

## Monitoring page

`http://<host>:8791/monitor/` — one page showing what the autolab is doing,
no SSH. Vanilla JS in `agent/monitor/`, no build step and no dependency; it
polls the routes above every 3 s.

Mission headline, driver state, `STATUS:` line and cumulative cost in the
header; then the jobs table (status, `iteration / max`, gates `n/m` with the
failing gate commands spelled out, cost, latest evidence), an evidence
browser that links every raw artefact (`prompt.txt`, `diff.patch`,
`gates.json`, `claude_output.json`, …), the per-session table, and the drive
log tail. Clicking a job expands it and puts it in the URL
(`/monitor/#job=<name>`), so the link survives a reload.

Polling is sufficient by design: session JSON is written once at session end
and `state.json` / `evidence/iter-NNNN/` once per iteration. The drive log is
the only append-only stream, and the page tails it.

Deployment to a job-runner node (checkout update, token, systemd user unit)
is owned by the `autolab_node` role in clusterintent's `ansible_agdev`.
