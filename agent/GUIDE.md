# autolab — entrance guide

The capability card this node's window answers from. Re-read from disk on
every request: edit it and the next answer changes, no restart. Served raw at
`GET /guide`.

## What this is

An autolab node: a headless auto-development loop. You describe something you
want built; a mediator agent turns it into a *job* (a goal, a coding adapter,
and acceptance gates) and runs one-shot coding-agent iterations against it
until the gates exit 0 or the iteration cap is reached. Every iteration leaves
evidence on disk — prompt, diff, gate results, cost.

## Doors

- `POST /mission` with `Authorization: Bearer <token>` — starts a mission.
- `POST /window` — this window: job, progress, spend and capability questions.
- `GET /status`, `/log`, `/jobs`, `/jobs/<job>`,
  `/jobs/<job>/evidence/<iter>/<file>`, `/monitor/`, `/game/...`, `/healthz`.
- `POST /jobs/<job>/summarize/<iter>` — prose for one iteration's evidence,
  written on this node.

## What it costs

Measured on this node (2026-08-09), tentative figures rather than quotes:

- A small CLI-sized job, `claude_code` adapter: **~0.13–0.21 USD**,
  1 iteration, a few minutes.
- A small web game (snake): **~0.9–1.35 USD** over 2 iterations.
- The `fake` adapter: **0 USD** — no credentials, exists to test the loop.
- A mission is the sum of its sessions plus mediator time; `GET /status`
  reports this node's cumulative session spend under `cost`.
- An iteration summary: **0.11–0.19 USD**, 11–15 s, paid once per iteration
  and cached after.
- Talking to this window: **unknown in USD on the default backend** — a local
  model via ollama (`qwen3.6:35b-a3b-coding-nvfp4`), which reports tokens but
  no price: ~2500 prompt tokens and 3–28 s per answer, measured 2026-08-10.
  On the `claude` backend answers measured **0.10–0.26 USD** and 7–8 s. Every
  answer is recorded under `.local/agent/window/`.

Timing: an iteration's budget is `iteration_timeout_seconds` (default 900).
Missions run unattended for tens of minutes; nothing here is an interactive
request/response wait.
