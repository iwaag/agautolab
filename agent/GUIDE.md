# autolab — entrance guide

The capability card this node's window answers from. Re-read from disk on
every request: edit it and the next answer changes, no restart. Served raw at
`GET /guide`.

## What this is

An autolab node **stub**. An autolab node was a headless auto-development
loop: you described something you wanted built, a mediator agent turned it
into a job with acceptance gates, and one-shot coding-agent iterations ran
against it until the gates exited 0, leaving evidence on disk.

That loop has been removed. What is left is this node's surface and its agent
configuration. Nothing is built here, no agent runs, and nothing costs money.

## Doors

- `POST /window` — this window, the node's only entrance. It answers with a
  fixed text and records which role, profile, harness and model would have
  served the request. A `<<mission>>…<</mission>>` block is still parsed and
  still validated, but no mission is started.
- `GET /status`, `/log`, `/jobs`, `/jobs/<job>`, `/projects`,
  `/jobs/<job>/evidence/<iter>/<file>`, `/monitor/`, `/game/...`, `/healthz`.
- `POST /jobs/<job>/summarize/<iter>`.

Only `GET /projects` and the role resolution behind `POST /window` read
anything real. The job-, mission- and evidence-shaped routes keep their
response shape and answer empty: no job exists on this node, so `/jobs` is
`[]` and any named job is a 404. `/monitor/` and `/game/...` are 404 — the
pages they served were removed. Every stub document carries `"stub": true`.

## What it costs

Nothing. No harness is launched from this node, by any route.

## Agents

Five roles resolve through `ag.agent-config.v1`: `front` (this window),
`director`, `mediator`, `coding`, `summarizer`. `agents.toml` names the
profiles; the ignored `.local/agents.local.toml` selects per-node profiles and
supplies commands and provider endpoints. Each role's tool grant and OpenCode
permission file are in `src/agautolab/role_run.py` and `agent/opencode-*.json`.

Resolution is real and unforgiving: an unknown role or an unknown profile
fails loudly rather than falling back. `GET /projects` shows every project's
effective `coding` and `director` profile and whether it comes from the
project's own `.local/projects/<name>/agents.toml` or from the shared default.

The `#pj-<name>` Zulip channels are still heard: a `mission-*` topic gets one
reply saying this node is a stub.
