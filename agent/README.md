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

Per-job runtime state lives under `../.local/jobs/<job>/`; the gateway's
summarizer adds `summaries/iter-NNNN.{md,raw.json,cost.json,prompt.txt,log,
run.json,exit}` there and writes nowhere else — never `state.json`, evidence,
`MISSION.md`, `NOTES.md` or the job's `.lock`.

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

- `POST /window` `{"text": "..."}` — **the conversational window** (see
  below). Free text in, prose out; it accepts no work.
- `GET /guide` — `agent/GUIDE.md`, the capability card, as plain text
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
- `POST /jobs/<job>/summarize/<iter>` `?force=1` — summarize that iteration's
  evidence **on this node** with a one-shot `claude -p`; returns the cached
  summary when one exists, otherwise `202 {"status": "pending"}`. Unauthenticated
  like the reads, though it spends money: one summarizer runs at a time
  (`409` otherwise) and each iteration is paid for once (the cache is the file).
- `GET /jobs/<job>/summarize/<iter>` — `{"status": absent|pending|done|error,
  "summary"?, "summarizer"?}`; `summarizer` carries the summarizer's own cost,
  turns and duration. `GET /jobs/<job>` reports each iteration's summary status
  in its timeline.
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

## The conversational window

`POST /window {"text": "..."}` is this node's single desire-accepting
entrance (devpolicy/policy.md, *Single Entrance*). Everything else above it
is a deterministic read, not a second place to express a wish.

It answers three kinds of message and nothing else:

- **job/progress/spend questions** — from the same job state `/status` and
  `/jobs` serve, assembled from the same helpers rather than a second walk
  of the job dirs;
- **capability/cost questions** — from `agent/GUIDE.md`, re-read from disk
  per request (cagent's `llms.txt` pattern), so editing the card needs no
  restart;
- **development requests** — refused, with the `POST /mission` + bearer
  token redirect. The window starts nothing and writes no job state.

Unauthenticated like the reads, and guarded one-answer-at-a-time (`409`
otherwise). The response *is* the run record: `backend`, `backend_model`,
`outcome`, `duration_ms`, `cost_usd`/tokens when the backend reports them,
and on failure the backend's verbatim words with HTTP 502. The same record
is written to `.local/agent/window/run-NNNN.json`
(devpolicy/agent_records.md).

### Backend (Agent ≠ Model)

Resolved process env first, then `../.local/.env` — the same order and shape
as agforge's `AGFORGE_AGENT_BACKEND`:

| variable | default | meaning |
|---|---|---|
| `AUTOLAB_WINDOW_BACKEND` | `ollama` | `ollama` \| `claude` |
| `AUTOLAB_WINDOW_MODEL` | `gemma3:latest` / `claude-sonnet-5` | model for the chosen backend |
| `AUTOLAB_OLLAMA_URL` | `http://127.0.0.1:11434` | ollama endpoint (a node without a local ollama points this at one) |

The `claude` backend reuses `claude_bin()` (`AUTOLAB_CLAUDE_BIN`, then
`.local/agent/claude_bin`, then PATH). **Either of the first two may be a
glob**, and should be: the usual value is an absolute path into a
version-numbered editor-extension directory, which goes stale on every update
and then fails as `No such file or directory` — an infra-looking error with a
config cause. Write

```text
/path/to/extensions/anthropic.claude-code-*-<arch>/resources/native-binary/claude
```

and the newest match is resolved per call. A plain path is still returned
as written, so a genuinely wrong one fails loudly with the path in the
message. Measured on agstudio 2026-08-09:
ollama/gemma3 answers in 1–5 s at no reported price; claude/claude-sonnet-5
answered the same question in ~10 s for 0.09 USD, and got a multi-job
question right that gemma3 got wrong — the switch is the point, the local
default keeps idle chatter free.

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
