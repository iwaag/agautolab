# autolab — entrance guide

The capability card this node's window answers from. Re-read from disk on
every request: edit it and the next answer changes, no restart. Served raw at
`GET /guide`.

## What this is

An autolab node: a headless auto-development loop. You describe something you
want built; a mediator agent turns it into a *job* (a goal, a coding profile,
and acceptance gates) and runs one-shot coding-agent iterations against it
until the gates exit 0 or the iteration cap is reached. Every iteration leaves
evidence on disk — prompt, diff, gate results, cost.

## Doors

- `POST /window` — this window, the node's only entrance: job, progress,
  spend and capability questions — and starting work. A
  `<<mission>>…<</mission>>` block in the window's reply starts a mission
  (one at a time; a refusal while one runs is recorded).
- `GET /status`, `/log`, `/jobs`, `/jobs/<job>`, `/projects`,
  `/jobs/<job>/evidence/<iter>/<file>`, `/monitor/`, `/game/...`, `/healthz`.
- `POST /jobs/<job>/summarize/<iter>` — prose for one iteration's evidence,
  written on this node.

## What it costs

`GET /jobs` carries `cost_usd` for every job this node has run, and
`GET /status` carries its cumulative session spend under `cost`. Those are
the live numbers. The figures below are examples from 2026-08-10, tentative
rather than quotes; a number written here goes stale as jobs run, the paths
do not.

- A small CLI-sized job, `claude_code` adapter: **~0.09–0.21 USD**,
  1 iteration, a few minutes.
- A small web game (snake): **~0.9–1.35 USD** over 2–3 iterations.
- The largest job this node has run: **3.78 USD** — jobs are not bounded by
  the examples above, only by `max_iterations`.
- The `fake` adapter: **0 USD** — no credentials, exists to test the loop.
- A mission is the sum of its sessions plus mediator time.
- An iteration summary: **~0.13–0.21 USD**, 11–18 s, paid once per iteration
  and cached after; each cached summary carries its own
  `summarizer.cost_usd` and `duration_ms` — read those rather than this line.
- Talking to this window: **unknown in USD on the default profile** — OpenCode
  with the local Ollama model (`ollama/qwen3.6:35b-a3b-coding-nvfp4`), which reports tokens but
  no price: ~2500 prompt tokens and 3–28 s per answer, measured 2026-08-10.
  Claude Code answers measured **0.10–0.26 USD** and 7–8 s. Every
  answer is recorded under `.local/agent/window/`.

Timing: an iteration's budget is `iteration_timeout_seconds` (default 900).
Missions run unattended for tens of minutes; nothing here is an interactive
request/response wait.

## Project directors

Project workspaces live under `.local/projects/<name>/direction/`;
`.local/projects/projects.md` lists every project, one line each. To consult
a project's director, pass the request to the common runner and name record
paths so the nested identity is reviewable:

Each project may have a developer-owned `.local/projects/<name>/agents.toml`
whose `[roles]` table selects `director` and/or `coding` profiles by name.
Director runs discover it from the direction workspace; coding jobs name the
project in `job.yaml`. The ignored file can change independently of project
source, and a missing file leaves the shared role defaults in effect.

When a user asks to change a project's agent backend, you may edit that
project's `.local/projects/<name>/agents.toml`. The only valid role keys are
`coding` and `director`; valid profile names come from the root `agents.toml`.
`GET /projects` shows the effective selections and their source. Make the
requested edit directly and explain the resulting selection; do not turn a
settings change into a mission.

```bash
uv run python -m agautolab.role_run director \
  --prompt "your question" \
  --cwd .local/projects/<name>/direction \
  --transcript .local/agent/director/run-<id>.agent.jsonl \
  --record .local/agent/director/run-<id>.json
```
