# autolab — operator guide for agents

You are an agent operating `autolab`, a headless coding-iteration machine.
You supply judgment (what to build, what the acceptance gates are, when to
stop); autolab supplies mechanics (running a coding agent in a loop against
your gates, with locking, evidence, and state kept on disk for you).

Everything below is the complete interface. There is no hidden state: one
job = one directory you can read with normal file tools at any time.

## Commands

Run from this repo (`agautolab/`):

```bash
uv run autolab run-once <job-dir>   # exactly one iteration
uv run autolab loop <job-dir>       # iterate until terminal; --sleep SECONDS (default 5)
uv run autolab status <job-dir>     # compact read-only status; --json for structured
```

Exit codes of `run-once` (and of `loop`, for its final iteration):

| code | meaning |
|---|---|
| 0 | converged — all gates passed (or another process held the job lock; `status` disambiguates) |
| 10 | continue — iteration done, gates still failing, more iterations allowed |
| 20 | stuck — no progress for `no_progress_limit` consecutive iterations, or `max_iterations` reached |
| 30 | error — broken job.yaml, adapter failure, git failure; see `state.json` `error` |

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

States: `pending → running → converged | stuck | error` (all three are
terminal: `run-once` on a terminal job exits immediately with its code and
does nothing). To retry a stuck/error job after changing something, edit
`state.json`: set `"status": "running"` and reset `"consecutive_no_progress": 0`
(keep `iteration` — evidence numbering continues from there).

### job.yaml

```yaml
goal: |
  Full statement of what the coding agent must build. It sees ONLY this,
  the gate commands + current failures, and the previous NOTES.md. Write it
  like a contract: behavior, file layout, and how it will be verified.
adapter: claude_code          # or "fake" (no-token test adapter)
adapter_config:
  command: "claude"           # binary path if not on PATH
  args:                       # extra CLI args for every iteration
    - "--model"
    - "claude-sonnet-5"
    - "--allowedTools"
    - "Write,Edit,Read,Glob,Grep,Bash(node:*),Bash(ls:*)"
gates:                        # shell commands run in target/; ALL must exit 0
  - "node --test"
max_iterations: 10            # hard ceiling (default 30)
no_progress_limit: 3          # consecutive no-progress iterations -> stuck
iteration_timeout_seconds: 900
gate_timeout_seconds: 300
push: true                    # push target/ to its `origin` after each commit
                              # and on terminal status (non-fatal if it fails)
```

Policy on this machine (agstudio): never set `skip_permissions: true`; grant
the coding agent what it needs via `--allowedTools` instead. Scope Bash
patterns to the commands the gates and build genuinely need.

### Seeding a job from scratch

1. `mkdir -p <job-dir>/target`
2. Put the contract into `target/`: a README describing the product, and the
   acceptance tests the gates will run. Anything you pre-place is the part
   the coding agent cannot weaken (state in `goal` that gates/tests must not
   be modified — the gate diff evidence lets you audit this later).
3. If the result should live on a git remote: create the repo on the remote
   first, then in `target/`: `git init`, `git remote add origin <url>`, and
   set `push: true`. autolab commits every iteration as author `autolab` and
   pushes `HEAD`; embed credentials in the remote URL
   (`http://<user>:<token>@host/...`) or a credential store — never in
   tracked files.
4. Write `job.yaml`.
5. `uv run autolab run-once <job-dir>` once and read `status` + the first
   evidence dir before committing to a long loop.

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

## Lessons from previous runs (advice, not rules)

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
