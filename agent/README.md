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
submitted without SSH. It refuses to start without a bearer token in
`.local/agent/gateway_token`. Routes (default `:8791`):

- `POST /mission` `{"mission": "...", "max_sessions": 12}` — writes
  `MISSION.md` and launches `drive.sh` detached; `409` while one is running
- `GET /status` — driver liveness/exit, `NOTES.md` STATUS line, per-session
  cost summaries, whether a game build is installed
- `GET /log?tail=N` — tail of the current drive log
- `GET /game/` — unauthenticated static serving of `.local/agent/serve/`
  (a mission that ships a browser game should install its verified build
  there)
- `GET /healthz` — unauthenticated liveness probe

Deployment to a job-runner node (checkout update, token, systemd user unit)
is owned by the `autolab_node` role in clusterintent's `ansible_agdev`.
