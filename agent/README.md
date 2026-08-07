# autolab agent

The agent layer on top of the `autolab` toolbelt: a mission goes in, the
agent plans it, seeds jobs, drives coding agents through `autolab`, verifies,
and declares done. Policy is Tool Arming — the only fixed rules live in
[CHARTER.md](CHARTER.md); everything else is the agent's judgment.

## Layout

Tracked (this directory): `CHARTER.md` (the system prompt/contract),
`session.sh` (one headless session), `drive.sh` (re-invoke until done).

Runtime state (local-only, under `../.local/agent/`):

```
.local/agent/
  MISSION.md            # the only external input — write the mission here
  NOTES.md              # agent-owned continuity; first line STATUS: working|complete|blocked
  claude_bin            # absolute path to the claude binary (one line)
  sessions/session-NNNN.json   # full claude output per session (cost/turns evidence)
```

## Run

```bash
echo "/path/to/claude" > .local/agent/claude_bin   # once
$EDITOR .local/agent/MISSION.md                    # the mission
agent/drive.sh [max_sessions]                      # default 12; exits 0 complete, 20 blocked, 10 budget
```

`AUTOLAB_AGENT_MODEL` overrides the agent model (default `claude-sonnet-5`);
`AUTOLAB_CLAUDE_BIN` overrides the binary. Sessions are never resumed — each
one reconstructs context from MISSION + NOTES + `autolab status`, the same
philosophy as `run-once` one level down.
