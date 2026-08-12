# agautolab

A stub. agautolab was a headless auto-development loop orchestrator: it drove
one-shot coding-agent iterations against a job directory until the job's
acceptance gates exited 0. That implementation was deleted (`discard_garbage`
episode, `pj-agdev/devdocs/episodes/`).

What is kept is what was worth keeping — this node's input/output surface and
its agent configuration:

- **The gateway's routes** (`agent/gateway.py`, default `:8791`), with their
  validation, status codes and response envelopes intact. They answer empty
  documents marked `"stub": true`. `GET /guide` serves `agent/GUIDE.md`, the
  capability card, which lists them.
- **The chat entrance** (`src/agautolab/zulip_listener.py`): `mission-*`
  topics in `#pj-<name>` channels are still heard and answered, and nothing is
  started.
- **The agent configuration**: `agents.toml` (five roles, three profiles, the
  models behind them), the ignored `.local/agents.local.toml` overlay, the
  per-role tool grants in `src/agautolab/role_run.py`, and the OpenCode
  permission files in `agent/opencode-*.json`.

Role resolution is live, not decorative: `GET /projects` and every window
answer resolve through `ag.agent-config.v1`, so a broken profile or project
selection still fails loudly. Nothing below that line launches a harness, so
this node cannot spawn a process or be charged.

```bash
uv run python agent/gateway.py     # the gateway
agent/zulip_listen.sh              # the chat entrance
```
