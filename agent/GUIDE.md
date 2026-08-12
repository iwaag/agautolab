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

## Reporting a Plane-backed mission

A dispatched task carries lines such as `Plane project ID: <uuid>` and
`Plane issue ID: <uuid>`, and the state IDs that belong to that project.
Keep them with the mission and report to that same project and issue: state
IDs are per-project in Plane, so the mission's own lines are the only ones
that are right for it. The node role installs `.local/plane.env` with the
URL, API key, and workspace — the credentials only; the project travels in
the mission. Read it from the agautolab checkout; do not copy its token
into a job directory, prompt, transcript, comment, or repository.

Post concise evidence comments when the job is created, after each completed
iteration (include the iteration number and gate result), and when the mission
ends. Do not comment on every status poll. For IDs supplied by the mission:

```bash
set -a
. .local/plane.env
set +a
project_id='<Plane project ID from the mission>'
issue_id='<Plane issue ID from the mission>'
api="$PLANE_URL/api/v1/workspaces/$PLANE_WORKSPACE_SLUG/projects/$project_id"
curl --fail-with-body --silent --show-error \
  -X POST -H "X-API-Key: $PLANE_API_KEY" \
  -H 'Content-Type: application/json' \
  "$api/issues/$issue_id/comments/" \
  --data '{"comment_html":"<p>Created job <code>job-name</code>.</p>"}'
```

Plane expects HTML in `comment_html`; JSON-escape any dynamic text rather than
assembling untrusted text inside the literal above. To change state, use the
matching state ID from the mission text and PATCH the issue:

```bash
state_id='<the mission-supplied state ID, e.g. its Done id>'
curl --fail-with-body --silent --show-error \
  -X PATCH -H "X-API-Key: $PLANE_API_KEY" \
  -H 'Content-Type: application/json' \
  "$api/issues/$issue_id/" \
  --data "{\"state\":\"$state_id\"}"
```

On convergence, comment with the final gates/evidence and move to Done. On a
stuck or errored mission, comment with the observed failure and your recovery
judgement, then move it back to a dispatchable state when another attempt is
useful or to Cancelled when it is not. If the mission names a state without
supplying its ID, list the project's states first
(`GET "$api/states/"`). A failed Plane call is evidence: record it in
`.local/agent/NOTES.md` and continue or stop based on whether the mission itself
can still be completed; never claim an update without the HTTP success.

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
