# agautolab

agautolab is the executor of workflow runs managed by the workflow editor
(WFE, `agdevworld/workflow_editor`), and the Zulip transport for their
requests and coordination. A person asks for a run (WFE run view, `wfe
request`, or a `request-…` post in a workflow's `wf-…` channel); WFE records
it and queues it; this process claims it and launches the agent the workflow
names, in the run's own copy of the project. Workflow definitions and run
state belong to WFE and the managed projects, not to this checkout.

The old development system that lived here — the Zulip listener with its
`workplan-`/`workrun-`/`bmining-` topics, missions and tasks, mission working
copies, the `:8791` gateway window and the `autolab` CLI — was retired in
`retire/old_autolab` (devdocs episode). Nothing here receives work outside WFE.

## Modules

- `agautolab.wfexec` — one execution slot. Claims the next job (`wfe exec
  claim`), launches the workflow's agent behind a launch gate with the common
  contract (`agent/guides/wfrun/guide.md`), the run's scoped context and its
  brief, reports how the process ended, carries out stops, and reconciles
  vanished processes every minute. Sessions, launch ownership and every state
  rule are described in its module docstring; the rules themselves live in
  WFE.
- `agautolab.wfnotify` — delivers each run's coordination to its `run-…`
  topic in the workflow's `wf-…` channel, creating a project's channel folder
  and a workflow's channel when WFE asks. Runs on a thread of `wfexec`.
- `agautolab.wfintake` — reads the channels WFE routes and hands every post to
  `wfe intake receive`. Runs on a thread of `wfexec`.
- `agautolab.instance` — the root, the bot credential path and `SPEC`, which
  `agag.agent` resolves the `wfrun` role through.

## Configuration

- `agents.toml` (`ag.agent-config.v2`): the models and profiles a workflow may
  select with `execution.profile` (`sonnet`, `luna`, …; Agent ≠ Model) and
  the `wfrun` role with its tool grant.
- `.local/agents.local.toml`: host overlay (harness commands, provider URLs,
  secret file paths).
- `.local/wfexec.toml`: `wfe` = the launcher argv; `[notify] enabled` and
  `[intake] enabled`, with optional `zulip_env` / `admin_env`; optional
  `agent_command` replaces the harness for trials.
- `.local/zulip.env`: the autolab bot's credential (posts run topics, reads
  routed channels). `../.local/zulip/provisioner.env`: the realm credential
  for creating folders and channels.

## Running

```bash
uv run python -m agautolab.wfexec          # the loop (launchd: com.agdev.agautolab-wfexec)
uv run python -m agautolab.wfexec --once   # one claim
uv run python -m agautolab.wfnotify --once # one delivery pass, alone
uv run python -m agautolab.wfintake --once # one intake pass, alone
```

The launchd template is `pj-agdev/devenv/launchd/com.agdev.agautolab-wfexec.plist.in`
(`agent/wfexec.sh`). Restart it only between attempts: a restart ends the
running agent's process, which WFE records as an interruption.

## Tests

```bash
uv run pytest -q
```

The tests drive the real `wfe` CLI under node with a stub agent, from the
sibling `../agdevworld/workflow_editor` (`WFE_PACKAGE` overrides it); they
skip when node or the package is unavailable.

`devenv/gitea/` describes agstudio's Gitea, where WFE creates managed
projects' repositories.
