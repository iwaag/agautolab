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
agent's final message, exit code, timing, cost) · `claude_output.json` (raw
backend JSON) · `push.json` (when `push` triggered one) · `error.txt` (error
iterations only).

`NOTES.md` is written by the coding agent, not by autolab, and is passed
forward into the next iteration's prompt as written.

## job.yaml

```yaml
goal: |
  The client's request.
adapter: claude_code          # or "fake" (no-token test adapter)
adapter_config:
  command: "claude"           # binary path if not on PATH
  args: ["--model", "claude-sonnet-5", "--allowedTools", "Write,Edit,Read,Bash(node:*)"]
  add_job_dir: true           # grant the job dir via --add-dir (default true)
gates:                        # omit → the job starts in the plan phase
  - "node --test"
max_iterations: 10            # default 30
iteration_timeout_seconds: 900
gate_timeout_seconds: 300
push: true                    # push target/ to `origin` after commits and on terminal status
```

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
- `POST /jobs/<job>/summarize/<iter>` on that gateway runs a separate
  one-shot `claude -p` over one evidence directory (~$0.15, once per
  iteration, cached). Its prose is the only iteration content that leaves
  this node.
- Unattended runs: `devenv/systemd/autolab@.service`, one instance per job.
