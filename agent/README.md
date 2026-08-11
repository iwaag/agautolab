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
  and never reads it; starting a new mission clears it.
- `sessions/session-NNNN.agent.jsonl` — raw mediator harness output.
- `sessions/session-NNNN.run.json` — normalized mediator run record.
- `gateway/`, `window/` — run logs and per-answer records. (`director/` holds
  records of the removed `/director` route, kept as evidence.)

Per-job state lives under `../.local/jobs/<job>/`; the summarizer adds
`summaries/iter-NNNN.*` there and writes nowhere else.

## Run

```bash
$EDITOR .local/agent/MISSION.md                    # the mission
agent/drive.sh [max_sessions]                      # default 12; 0 = done file exists, 10 = budget spent
```

Configure role profiles in `agents.toml`; put binary paths/globs, the Ollama
endpoint, and per-node role overrides in `.local/agents.local.toml`. Sessions
are never resumed and unavailable selections fail without fallback.

## Gateway routes (default `:8791`)

Stdlib-only. No route carries authentication (zero_auth episode).

- `POST /window` `{"text": str}` — the conversational entrance, and the only
  way in for work: a `<<mission>>…<</mission>>` block in the window's reply
  writes MISSION.md and launches `drive.sh` detached (409 while one runs).
  One answer at a time (409). It may launch the profile-selected project
  director in a `.local/projects/<name>/direction/` workspace (see
  `GUIDE.md`).
- `GET /guide` — `GUIDE.md` as plain text.
- `GET /status` — driver liveness/exit, mission text, the agent's `done` note
  and NOTES.md, per-session and cumulative cost, game-build presence.
- `GET /log?tail=N` — tail of the current drive log.
- `GET /jobs` — one row per `.local/jobs/<job>/`.
- `GET /jobs/<job>` — that row plus the evidence timeline.
- `GET /jobs/<job>/evidence/<iter>/<file>` — the raw evidence file.
- `POST /jobs/<job>/summarize/<iter>` `?force=1` — summarize one evidence
  directory on this node with the `summarizer` role. One summarizer
  at a time, one paid call per iteration ever.
- `GET /jobs/<job>/summarize/<iter>` — `{status, summary?, summarizer?}`.
- `GET /monitor/` — the watch page. `GET /game/` — `.local/agent/serve/`.
  `GET /healthz` — liveness.

Reads never write and never take a job's `.lock`. JSON carries a
`"kind": "autolab.monitor.v1"` envelope.

## Agent profiles (Agent ≠ Model)

All five roles resolve through `ag.agent-config.v1`: `front`, `director`,
`mediator`, `coding`, and `summarizer`. The committed file names profiles;
the ignored local overlay selects per-node profiles and supplies commands and
provider endpoints. Every new window, session, summary, director, and coding
record carries role, profile, harness, provider, canonical model, and outcome.

## Monitoring page

`http://<host>:8791/monitor/` — vanilla JS in `monitor/`, no build step,
polls every 3 s. Mission, driver state, cumulative cost, the agent's own
notes, the jobs table, an evidence browser linking every raw artefact, the
session table, and the drive log tail. `#job=<name>` survives a reload.

Deployment to a job-runner node is owned by
`ansible_agdev/playbooks/agent/setup_autolab_node.yml` in clusterintent. The
`autolab_node` placement profile supplies the provider endpoint and role
profiles; the playbook installs both pinned harness CLIs and generates the
ignored overlay. Anthropic keys, when used, are copied from a controller-local
secret file and appear in the overlay only as a `_file` reference.
