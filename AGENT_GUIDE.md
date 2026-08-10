# autolab — the manual

`autolab` runs a coding agent against a job directory, one iteration per
process, until the job's acceptance gates exit 0. One job = one directory you
can read with normal file tools at any time; there is no hidden state.

## Commands

Run from this repo (`agautolab/`):

| command | what it does |
|---|---|
| `uv run autolab run-once <job-dir>` | exactly one iteration |
| `uv run autolab loop <job-dir>` | run-once while it returns 10; `--sleep SECONDS` (default 5) |
| `--detach` on either | starts it in a session of its own, outliving the caller, and returns with a pid; output appends to `<job-dir>/detached.log`, the verdict is in `status` |
| `uv run autolab status <job-dir>` | job state; `--json` for structured. Lock-free, safe during a live run |
| `uv run autolab approve <job-dir>` | accept a plan's gates → implement phase. `--gates FILE`, `--gate CMD` (repeatable), else `target/proposed_gates.yaml` |
| `uv run autolab reject <job-dir> --feedback <file\|text>` | append feedback to `NOTES.md`; the job replans |

Exit codes (`run-once`, and `loop`'s final iteration): `0` converged (every
gate exited 0) · `10` continue · `20` stuck (`max_iterations` reached) · `30`
error · `40` awaiting approval. `run-once` exits 0 silently when another
process holds the job lock; `status` disambiguates. `loop` exits `130` on
Ctrl-C.

## Job directory

```
<job-dir>/
  job.yaml              # you write this
  target/               # the repo being developed; git-init'd on first run
  state.json            # status, iteration, phase, gate summary, approved gates
  NOTES.md              # the coding agent's handoff between iterations
  evidence/iter-NNNN/   # per-iteration record
  .lock                 # flock; one iteration at a time
```

`evidence/iter-NNNN/`: `prompt.txt` (what the agent was told) · `diff.patch`
(what the iteration changed in `target/`) · `gates.json` (per-gate exit codes
and output tails) · `adapter_output.txt` and `adapter_result.json` (the
agent's final message, canonical identity, exit code, timing, cost) ·
`agent_output.json` or `agent_output.jsonl` (raw harness output) · `push.json`
(when `push` triggered one) · `error.txt` (error
iterations only).

`NOTES.md` is written by the coding agent, not by autolab, and is passed
forward into the next iteration's prompt as written.

## job.yaml

```yaml
goal: |
  The client's request.
profile: sonnet-coder         # optional; defaults to the coding role profile
adapter_config:
  allowed_tools: "Write,Edit,Read,Bash(node:*)"  # Claude Code role grant
  add_job_dir: true           # grant the job dir via --add-dir (default true)
gates:                        # omit → the job starts in the plan phase
  - "node --test"
max_iterations: 10            # default 30
iteration_timeout_seconds: 900
gate_timeout_seconds: 300
push: true                    # push target/ to `origin` after commits and on terminal status
```

The profile determines both harness and canonical model. Do not pass model
flags in `adapter_config`; executable paths and globs are local overlay facts
in `.local/agents.local.toml`. The shared profiles are `local-coder`
(OpenCode + Ollama), `sonnet-coder` (Claude Code), and test-only `stub`.

`goal` heads every iteration's prompt unchanged, in both phases — the client's
standing request, not one iteration's instruction. A plan-phase sentence left
in it ("write no code yet") is read again by the agent implementing.

## Phases

- **Plan** (no `gates` in job.yaml): the coding agent gets the goal; the
  iteration ends in `awaiting_approval` for you to `approve` or `reject`.
  `approve` records the gates in `state.json approved_gates`; job.yaml is
  never rewritten.
- **Implement** (gates in job.yaml, or approved gates): each iteration runs
  the agent, then the gates, then records both.

To retry a stuck/error job, edit `state.json`: `"status": "running"`.
Evidence numbering continues from `iteration`.

## Around a job

- With a git remote: create the repo, then in `<job-dir>/target/` run
  `git init` and `git remote add origin <url>`, and set `push: true`.
  autolab commits each iteration as author `autolab`. Credentials go in the
  remote URL or a credential store, never in tracked files.
- The coding agent's cwd is `target/`; the job directory is granted via
  `--add-dir`, so `NOTES.md` and `evidence/` are reachable.
- `http://<host>:8791/monitor/` shows all of this live, read-only, without
  taking the lock — assume someone may be watching.
- `POST /jobs/<job>/summarize/<iter>` on that gateway resolves the
  `summarizer` role for a one-shot over one evidence directory (once per iteration,
  cached; the cached summary carries its own `summarizer.cost_usd` and
  `duration_ms`, lately ~$0.13–0.21 and 11–18 s). Its prose is the only
  iteration content that leaves this node.
- Unattended runs: `devenv/systemd/autolab@.service`, one instance per job.

## Projects

A project is a pair of git repositories under the `autodev` org on this
node's gitea (`http://agstudio.local:3000`; API token in
`.local/gitea/autolab-agent.token`; `POST /api/v1/orgs/autodev/repos`
creates a repo):

- `<name>` — the main repository. Coding agents grow its contents.
- `<name>-direction` — the director's workspace. The autolab agent creates
  the pair and plants and maintains the direction files itself: a `GUIDE.md`
  telling the director its role, a `concept.md` stating the project's theme,
  and a `.gitignore` containing `.local`.

Locally both are cloned under `.local/projects/<name>/`, as `main/` and
`direction/`. `.local/projects/projects.md` lists every project, one line
each.
