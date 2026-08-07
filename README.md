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
  `run(prompt, workdir, timeout) -> {output, exit}`. The `fake` adapter
  (appends a line to a file) makes the loop testable without tokens.

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

## Usage

```bash
uv run autolab run-once path/to/job
```

## Tests

```bash
uv run pytest -q
```
