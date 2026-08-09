# autolab agent

The agent layer on top of the `autolab` toolbelt: a mission goes in, the agent
plans it, seeds jobs, drives coding agents through `autolab`, verifies, and
declares done. [CHARTER.md](CHARTER.md) is what it is given.

## Files

Tracked here: `CHARTER.md` (the session prompt), `session.sh` (one headless
session), `drive.sh` (re-invoke until done), `gateway.py` (the HTTP front
door), `GUIDE.md` (the entrance capability card), `monitor/` (the watch page).

Runtime state under `../.local/agent/`:

- `MISSION.md` — the mission; the only external input.
- `NOTES.md` — the agent's own continuity, agent-written.
- `done` — the agent's end-of-mission note. `drive.sh` stops on its existence
  and never reads it; `POST /mission` clears it.
- `claude_bin` — absolute path (or glob) to the claude binary.
- `sessions/session-NNNN.json` — full claude output per session.
- `gateway/`, `window/`, `director/` — run logs and per-answer records.

Per-job state lives under `../.local/jobs/<job>/`; the summarizer adds
`summaries/iter-NNNN.*` there and writes nowhere else.

## Run

```bash
echo "/path/to/claude" > .local/agent/claude_bin   # once
$EDITOR .local/agent/MISSION.md                    # the mission
agent/drive.sh [max_sessions]                      # default 12; 0 = done file exists, 10 = budget spent
```

`AUTOLAB_AGENT_MODEL` sets the agent model (default `claude-sonnet-5`);
`AUTOLAB_CLAUDE_BIN` the binary. Sessions are never resumed.

## Gateway routes (default `:8791`)

Stdlib-only. Refuses to start without a token in `.local/agent/gateway_token`.

- `POST /mission` `{"mission": str, "max_sessions": int?}` — writes MISSION.md
  and launches `drive.sh` detached; 409 while one runs. **The only
  authenticated route** (`Authorization: Bearer <token>`).
- `POST /window` `{"text": str}` — the conversational entrance. One answer at
  a time (409).
- `POST /director` `{"text": str}` — a workspace-backed director window,
  half-implemented. Read-only tools, cwd = the configured direction clone.
- `GET /guide` — `GUIDE.md` as plain text.
- `GET /status` — driver liveness/exit, mission text, the agent's `done` note
  and NOTES.md, per-session and cumulative cost, game-build presence.
- `GET /log?tail=N` — tail of the current drive log.
- `GET /jobs` — one row per `.local/jobs/<job>/`.
- `GET /jobs/<job>` — that row plus the evidence timeline.
- `GET /jobs/<job>/evidence/<iter>/<file>` — the raw evidence file.
- `POST /jobs/<job>/summarize/<iter>` `?force=1` — summarize one evidence
  directory on this node with a one-shot `claude -p`. Unauthenticated but
  paid: one summarizer at a time, one paid call per iteration ever.
- `GET /jobs/<job>/summarize/<iter>` — `{status, summary?, summarizer?}`.
- `GET /monitor/` — the watch page. `GET /game/` — `.local/agent/serve/`.
  `GET /healthz` — liveness.

Every `GET` is unauthenticated on this experimental node; auth is designed
system-wide later. Reads never write and never take a job's `.lock`. JSON
carries a `"kind": "autolab.monitor.v1"` envelope.

## Window and director backends (Agent ≠ Model)

Process env first, then `../.local/.env`:

| variable | default | meaning |
|---|---|---|
| `AUTOLAB_WINDOW_BACKEND` | `ollama` | `ollama` \| `claude` |
| `AUTOLAB_WINDOW_MODEL` | `qwen3.6:35b-a3b-coding-nvfp4` / `claude-sonnet-5` | model |
| `AUTOLAB_OLLAMA_URL` | `http://127.0.0.1:11434` | ollama endpoint |
| `AUTOLAB_DIRECTOR_WORKSPACE` | `.local/direction/scifi-direction` | director cwd |
| `AUTOLAB_DIRECTOR_MODEL` | `claude-sonnet-5` | director model |

`AUTOLAB_CLAUDE_BIN` and `.local/agent/claude_bin` may be globs, and should
be: the usual value points into a version-numbered editor-extension directory
that goes stale on every update. Write
`/path/to/anthropic.claude-code-*-<arch>/resources/native-binary/claude` and
the newest match resolves per call.

## Monitoring page

`http://<host>:8791/monitor/` — vanilla JS in `monitor/`, no build step,
polls every 3 s. Mission, driver state, cumulative cost, the agent's own
notes, the jobs table, an evidence browser linking every raw artefact, the
session table, and the drive log tail. `#job=<name>` survives a reload.

Deployment to a job-runner node is owned by the `autolab_node` role in
clusterintent's `ansible_agdev`.
