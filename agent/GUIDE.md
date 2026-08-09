# autolab — entrance guide

The capability card this node's conversational window answers from. Plain
text, re-read from disk on every request: edit it and the next answer
changes, no restart. Served raw at `GET /guide`.

## What this is

An autolab node: a headless auto-development loop. You describe something
you want built; a mediator agent turns it into a *job* (a goal, a coding
adapter, and acceptance gates) and then runs one-shot coding-agent
iterations against that job until the gates pass, it stops making progress,
or it hits the iteration cap. Every iteration leaves evidence on disk
(prompt, diff, gate results, cost).

## What you can ask this window

- **Job and progress questions** — "what is running?", "how did snake-web
  end?", "how many iterations did fizzbuzz take?", "what has this node
  spent?". Answered from live job state.
- **Capability and cost questions** — this card.
- **Development requests** — the window does *not* accept work. The door is
  `POST /mission` with `Authorization: Bearer <token>`; the window will say
  so and stop there.

## What it costs

Tentative, from the jobs on this node (2026-08-09) — a real figure, not a
quote:

- A small CLI-sized job with the `claude_code` adapter: **~0.13–0.21 USD**,
  1 iteration, a few minutes.
- A small web game (snake): **~0.9–1.35 USD** over 2 iterations.
- The `fake` adapter: **0 USD** — it needs no credentials and exists to test
  the loop.
- A mission (the mediator driving jobs end to end) is the sum of its
  sessions plus mediator time; the node's cumulative session spend is
  reported by `GET /status` under `cost`.
- Talking to this window: **unknown in USD on the default backend** — it is
  a small local model via ollama, which reports tokens but no price
  (~2500 prompt tokens per answer, 1–5 seconds). Switched to the `claude`
  backend, one answer measured **0.09 USD** and about 10 seconds. Every
  answer either way is recorded under `.local/agent/window/`.

Timing: an iteration's budget is `iteration_timeout_seconds` in the job
(default 900s). Missions run unattended for tens of minutes; nothing here is
an interactive request/response wait.

## What it will not do

- Accept a desire at this window (use `POST /mission`).
- Hand out raw evidence to callers outside the node — ask for an iteration
  summary instead (`POST /jobs/<job>/summarize/<iter>`: **0.11–0.19 USD**
  and 11–15 s measured, paid once per iteration and cached forever after).

## The rest of the surface

`GET /status`, `GET /log`, `GET /jobs`, `GET /jobs/<job>`,
`GET /jobs/<job>/evidence/<iter>/<file>`, `GET /monitor`, `GET /game/...`,
`GET /healthz`. These are deterministic reads, not second entrances.
