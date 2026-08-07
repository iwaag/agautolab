# agautolab

Headless auto-development loop orchestrator. `autolab` drives one-shot,
non-interactive coding-agent iterations against a job directory until the
job's acceptance gates pass.

Design decisions and history: `pj-agdev/../devdocs/episodes/autodev/plan.md`
(the autodev episode) and `devdocs/episodes/agautolab/begin/report.md` here.

## Model

- 1 iteration = 1 process: `autolab run-once <job-dir>`. All inter-iteration
  state lives on disk inside the job directory; sessions are never resumed.
- Each iteration gets a fresh prompt built from the job goal + current gate
  failures + the previous iteration's `NOTES.md` handoff.
- Coding-agent backends are adapters with a tiny interface:
  `run(prompt, workdir, timeout) -> {output, exit}` (plus optional structured
  meta and evidence artifacts). The `fake` adapter (appends a line to a file)
  makes the loop testable without tokens.

## Adapters

- `fake` — appends a line to a file per run; no credentials needed.
- `claude_code` — runs `claude -p --output-format json` one-shot with
  `cwd=target/`, prompt on stdin. Captures the stdout JSON as
  `claude_output.json` evidence and logs token/cost fields
  (`total_cost_usd`, `usage`, `num_turns`, …) into `adapter_result.json`.
  `adapter_config`: `command` (binary path, default `claude`), `args`
  (extra CLI args such as `--model` / `--allowedTools`), `skip_permissions`
  (adds `--dangerously-skip-permissions`; policy: only on experimental
  nodes/VMs, never on a machine holding real credentials beyond what the
  job needs — prefer `--allowedTools` locally).

## Job directory layout

```
<job-dir>/
  job.yaml        # goal, adapter, gate commands, max_iterations, no_progress_limit
  state.json      # {status, iteration, consecutive_no_progress, last_gate_summary}
  target/         # the app repo being developed (auto git-init on first run)
  evidence/iter-NNNN/   # prompt, adapter output, diff, gate results
  NOTES.md        # handoff regenerated at end of each iteration
  .local/         # job-scoped secrets, never tracked
```

### job.yaml

```yaml
goal: |
  Make `python -m pytest` pass in this repo by implementing fizzbuzz + tests.
adapter: fake            # adapter name; "fake" needs no credentials
adapter_config: {}       # adapter-specific settings
gates:                   # all must exit 0 (run from target/ root)
  - python -m pytest -q
max_iterations: 30
no_progress_limit: 3
iteration_timeout_seconds: 900
gate_timeout_seconds: 300
```

## States and exit codes

`pending → running → (continue) → converged | stuck | error`, plus
`awaiting_approval` (defined for future semi-auto mode; auto-passed in
full-auto mode, the only mode implemented).

`run-once` exit codes: `0` converged, `10` continue, `20` stuck, `30` error.
If another process holds the job lock, it exits `0` silently. Stuck =
`no_progress_limit` consecutive iterations where the failing-gate set did not
shrink and the diff was effectively empty, or `max_iterations` reached.

`loop` repeats `run-once` while it returns `10` (with `--sleep SECONDS`
between iterations, default 5) and exits with the terminal code (`0`/`20`/`30`;
`130` on Ctrl-C). Crash recovery needs no extra code: state is on disk, the
next `loop` or `run-once` reconstructs and continues.

## Usage

```bash
uv run autolab run-once path/to/job   # exactly one iteration
uv run autolab loop path/to/job       # iterate until converged/stuck/error
uv run autolab status path/to/job     # compact read-only status (--json)
```

With `push: true` in job.yaml, `run-once` pushes `target/` to its `origin`
remote after each iteration commit and on reaching a terminal status;
failures are non-fatal and recorded in `evidence/iter-NNNN/push.json`.

An LLM agent operating autolab as a toolbelt should read
[AGENT_GUIDE.md](AGENT_GUIDE.md) — the complete operator contract.

For unattended runs on a dev node, a template systemd user unit lives at
`devenv/systemd/autolab@.service` (one instance per job,
`Restart=on-failure` with stuck/error exempted). Step 5 of the autodev
episode installs and adapts it.

## Tests

```bash
uv run pytest -q
```
