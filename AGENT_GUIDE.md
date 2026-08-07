# autolab — operator guide for agents

You are an agent operating `autolab`, a headless coding-iteration machine.
You supply the client's request and review judgment (is the delegate's plan
faithful to the request? when to stop); autolab supplies mechanics (running a
coding agent in a loop, with locking, evidence, and state kept on disk for
you). The coding agent plans, proposes its own acceptance gates, and
implements; you approve or reject.

Everything below is the complete interface. There is no hidden state: one
job = one directory you can read with normal file tools at any time.

## Commands

Run from this repo (`agautolab/`):

```bash
uv run autolab run-once <job-dir>   # exactly one iteration
uv run autolab loop <job-dir>       # iterate until terminal or awaiting approval; --sleep SECONDS (default 5)
uv run autolab status <job-dir>     # compact read-only status; --json for structured
uv run autolab approve <job-dir>    # accept the proposed plan+gates -> implement phase
uv run autolab reject <job-dir> --feedback <file|text>   # send the plan back with your reasons
```

Exit codes of `run-once` (and of `loop`, for its final iteration):

| code | meaning |
|---|---|
| 0 | converged — all gates passed (or another process held the job lock; `status` disambiguates) |
| 10 | continue — iteration done, more iterations allowed |
| 20 | stuck — no progress for `no_progress_limit` consecutive iterations, or `max_iterations` reached |
| 30 | error — broken job.yaml, adapter failure, git failure; see `state.json` `error` |
| 40 | awaiting approval — a plan + proposed gates are ready for your review |

`status` never takes the job lock and never writes; safe while a loop is live.
A long-running `loop` is best launched in the background; poll with `status`.

## Job directory contract

```
<job-dir>/
  job.yaml              # you write this once (see below)
  target/               # the repo being developed; auto git-init if missing
  state.json            # machine state: status, iteration, gate summary, error
  NOTES.md              # handoff the coding agent leaves itself between iterations
  evidence/iter-NNNN/   # per-iteration proof (see below)
  .lock                 # flock; one iteration at a time
```

Phases and states: a job with **no `gates` in job.yaml** (the normal case)
starts in the **plan phase**: the coding agent receives the goal verbatim and
must produce `target/PLAN.md` + `target/proposed_gates.yaml`. When both
exist, the job stops in `awaiting_approval` (exit 40) until you `approve`
(the proposed gates become official, recorded in `state.json
approved_gates`; job.yaml is never rewritten) or `reject --feedback`
(your feedback is appended to NOTES.md and planning resumes). After
approval the **implement phase** runs the classic loop:
`running → converged | stuck | error` (terminal: `run-once` on a terminal
job exits immediately and does nothing). A job.yaml that does carry `gates`
skips planning entirely. To retry a stuck/error job after changing
something, edit `state.json`: set `"status": "running"` and reset
`"consecutive_no_progress": 0` (keep `iteration` — evidence numbering
continues from there).

### job.yaml

```yaml
goal: |
  The client's request, nearly verbatim. Do NOT translate it into a
  technical contract — the coding agent plans and proposes the acceptance
  gates itself; your leverage is the review, not the wording here.
adapter: claude_code          # or "fake" (no-token test adapter)
adapter_config:
  command: "claude"           # binary path if not on PATH
  args:                       # extra CLI args for every iteration
    - "--model"
    - "claude-sonnet-5"
    - "--allowedTools"
    - "Write,Edit,Read,Glob,Grep,Bash(node:*),Bash(ls:*)"
# gates: omit -> job starts in the plan phase (normal).
# Providing gates here skips planning and runs the implement phase directly.
max_iterations: 10            # hard ceiling (default 30)
no_progress_limit: 3          # consecutive no-progress iterations -> stuck (implement phase only)
iteration_timeout_seconds: 900
gate_timeout_seconds: 300
push: true                    # push target/ to its `origin` after each commit,
                              # on terminal status, and on awaiting_approval
```

Policy on this machine (agstudio): never set `skip_permissions: true`; grant
the coding agent what it needs via `--allowedTools` instead. Scope Bash
patterns to the commands the gates and build genuinely need.

### Starting a job

1. `mkdir -p <job-dir>` and write `job.yaml`: `goal` = the request (nearly
   verbatim), no `gates`. You seed nothing in `target/` — no README, no
   tests; those are the coding agent's deliverables.
2. If the result should live on a git remote: create the repo on the remote
   first, then `mkdir -p <job-dir>/target` and in `target/`: `git init`,
   `git remote add origin <url>`, and set `push: true`. autolab commits every
   iteration as author `autolab` and pushes `HEAD`; embed credentials in the
   remote URL (`http://<user>:<token>@host/...`) or a credential store —
   never in tracked files.
3. `uv run autolab run-once <job-dir>` (or `loop`) until it stops with
   exit 40 / `status` shows `awaiting_approval`.
4. Review: read `target/PLAN.md` and the proposed gates (`status --json`
   shows them). Judge against the request — see the review checklist in
   Lessons below.
5. `uv run autolab approve <job-dir>`, or
   `uv run autolab reject <job-dir> --feedback <file|text>` and go back
   to 3.
6. After approval, `run-once`/`loop` drives the implement phase; read
   `status` + the first evidence dir before committing to a long loop.

### Evidence — how to see what actually happened

`evidence/iter-NNNN/` per iteration:

- `prompt.txt` — exactly what the coding agent was told.
- `adapter_output.txt` — its final message. `claude_output.json` — full JSON
  including `total_cost_usd`, `num_turns`, `permission_denials`.
- `adapter_result.json` — exit code, timing, cost/turn metadata.
- `diff.patch` — the complete change that iteration made to `target/`
  (this is your audit trail: check nobody touched the tests/gates).
- `gates.json` — per-gate exit codes and output tails.
- `push.json` — push result, only when `push: true` triggered one.
- `error.txt` — only on error-status iterations.

`NOTES.md` in the job dir is the latest iteration's handoff (status, gate
results, diff stat, output tails). `status --json` is a cheaper first read.

## Reviewing a proposed plan (the mediator's craft)

- **Traceability**: map each sentence of the request to at least one
  proposed gate. A request sentence with no gate is unverified scope; a gate
  serving no sentence is scope creep. PLAN.md is required to state this
  mapping — reject if it doesn't.
- **No trivial passes**: would the gates pass against an empty or unmodified
  repo? Would they pass with the feature stubbed out? If yes, reject.
- **Named endpoints**: each gate should say exactly what process/endpoint it
  verifies. "Tests pass" is not an endpoint.
- **Demand adversarial testability instead of authoring it.** Where you would
  once have written the trap yourself (e.g. injectable RNG so "it's random,
  can't test" is no excuse), require it in reject feedback: "propose a gate
  that proves X even under Y". The delegate writes it; you judge it.
- Self-authored gates passing is self-approval. For anything user-facing,
  plan one independent audit (e.g. a Playwright probe you run yourself)
  after convergence — auditing is your job; authoring tests is not.

## Lessons from previous runs (advice, not rules)

- Run driver loops in the **foreground** of a live session. Background tasks
  started inside a headless session die with the session.
- Cheap deterministic gates win: plain ES modules + bare `node --test` with
  zero npm deps converged fastest. Caveat: `node --test test/` (directory
  argument) misbehaves on newer Node — use bare `node --test`, which picks up
  `test/*.test.js` itself.
- Name the verification endpoint before verifying. A past run "verified" a
  screenshot from the wrong server. Serve the exact checkout you mean to
  prove (e.g. `python3 -m http.server <port>` inside `target/`), and record
  which port/process the probe hit alongside the claim.
- One iteration of a sonnet-class coding agent on a small web-game job ran
  $0.31–0.48 and 12–17 turns. Budget `max_iterations` accordingly.
- If a job converges instantly and you need to exercise multi-iteration
  behavior, tighten the gates in a follow-up (add requirements) rather than
  restarting from zero: reset `state.json` to `running` and continue.
- The coding agent works with `cwd=target/` and sees nothing outside it.
  Everything it must know goes in `goal`, the README contract, or the tests.
