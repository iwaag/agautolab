# agautolab

Headless auto-development loop orchestrator. `autolab` drives one-shot,
non-interactive coding-agent iterations against a job directory until the
job's acceptance gates exit 0.

## Model

- 1 iteration = 1 process: `autolab run-once <job-dir>`. All inter-iteration
  state lives on disk inside the job directory; sessions are never resumed,
  so crash recovery needs no code.
- Each iteration's prompt is built from the goal, the current gate results,
  and the handoff the coding agent left in `NOTES.md`.
- Backends are adapters with a tiny interface:
  `run(prompt, workdir, timeout) -> {output, exit}` plus optional metadata and
  evidence artifacts.

## Adapters

- `fake` — appends a line per run; no credentials, 0 USD, used by the tests.
- `claude_code` — `claude -p --output-format json` one-shot, `cwd=target/`,
  prompt on stdin. Saves stdout as `claude_output.json` and the parsed result
  JSON into `adapter_result.json`. `adapter_config`: `command`, `args`,
  `add_job_dir` (grants the job dir via `--add-dir`, default true),
  `skip_permissions` (experimental nodes only; never on a machine holding
  real credentials).

## Layout

```
<job-dir>/
  job.yaml        # goal, adapter, gates, max_iterations, timeouts, push
  state.json      # status, iteration, phase, gate summary, approved gates
  target/         # the repo being developed (auto git-init on first run)
  evidence/iter-NNNN/   # prompt, adapter output, diff, gate results, cost
  NOTES.md        # the coding agent's handoff, agent-written
  .local/         # job-scoped secrets, never tracked
```

States: `pending → running → converged | stuck | error`, plus
`awaiting_approval` between the plan and implement phases. Exit codes:
`0` converged · `10` continue · `20` stuck (`max_iterations`) · `30` error ·
`40` awaiting approval. `loop` repeats while `run-once` returns 10 and exits
with the terminal code.

## Usage

```bash
uv run autolab run-once path/to/job
uv run autolab loop path/to/job
uv run autolab status path/to/job --json
uv run autolab approve path/to/job [--gates FILE | --gate CMD]
uv run autolab reject path/to/job --feedback <file|text>
uv run pytest -q
```

## Around it

- [AGENT_GUIDE.md](AGENT_GUIDE.md) — the manual for an agent operating autolab.
- [agent/](agent/) — the mediator agent layer and the HTTP gateway
  (`/monitor/`, `POST /mission`, the conversational window).
- `devenv/systemd/autolab@.service` — one unit instance per job.
- Design history: the autodev episode in `devdocs/episodes/`.
