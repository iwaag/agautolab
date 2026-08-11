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
- Harness adapters have a tiny interface:
  `run(prompt, workdir, timeout) -> {output, exit}` plus optional metadata and
  evidence artifacts.

## Adapters

- `fake` — appends a line per run; no credentials, 0 USD, used by the tests.
- `opencode` — `opencode run --format json` with the profile's full canonical
  model ID; raw JSONL is retained as `agent_output.jsonl`.
- `claude_code` — `claude -p --output-format json` one-shot, `cwd=target/`,
  prompt on stdin. Saves stdout as `agent_output.json` and normalized metadata
  into `adapter_result.json`. `adapter_config`: `args`, `allowed_tools`,
  `add_job_dir` (grants the job dir via `--add-dir`, default true),
  `skip_permissions` (experimental nodes only; never on a machine holding
  real credentials).

## Layout

```
<job-dir>/
  job.yaml        # goal, optional project/profile, gates, timeouts, push
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
uv run autolab run-once path/to/job [--detach]   # --detach: new session, returns at once
uv run autolab loop path/to/job [--detach]
uv run autolab status path/to/job --json
uv run autolab approve path/to/job [--gates FILE | --gate CMD]
uv run autolab reject path/to/job --feedback <file|text>
uv run pytest -q
```

`agents.toml` declares the five roles and named profiles. Machine paths,
provider endpoints, and per-node role overrides belong only in the ignored
`.local/agents.local.toml`. Selection failures use `ag.agent-config.v1` error
codes and never fall back to another harness or model.

A job's optional `project:` links it to the developer-owned
`.local/projects/<name>/agents.toml`. Its `[roles].coding` selection applies
unless `job.yaml` has a one-run `profile:` override; otherwise the shared role
default applies. Director runs launched within that project's `direction/`
workspace use `[roles].director` from the same file.

## Around it

- [AGENT_GUIDE.md](AGENT_GUIDE.md) — the manual for an agent operating autolab.
- [agent/](agent/) — the mediator agent layer and the HTTP gateway
  (`/monitor/` and the conversational window, which is the entrance and
  starts missions).
- `devenv/systemd/autolab@.service` — one unit instance per job.
- Design history: the autodev episode in `devdocs/episodes/`.
