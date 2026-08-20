# agautolab

agautolab is being rebuilt as a small chat-driven project and mission
registration service. The old headless development loop was deleted
(`discard_garbage` episode, `pj-agdev/devdocs/episodes/`); its empty read-side
surface remains for existing consumers while the new path is added.

What is kept is what was worth keeping — this node's input/output surface and
its agent configuration:

- **The gateway** (`agent/gateway.py`, default `:8791`). `POST /window` runs
  the real front agent with the request text unchanged. Existing `/status`,
  `/jobs`, and `/projects` consumers retain their response surfaces; the
  removed loop's read-side documents remain marked `"stub": true`.
- **The chat entrance** (`src/agautolab/zulip_listener.py`): `workplan-*`
  topics in `#pj-<name>` channels are still heard and answered, and nothing is
  started. The topic prefix says what kind of request it is, and — since
  `agent_standardize` p3 (2026-08-21) — whether it plans or executes:

  | prefix | swept where | means |
  |---|---|---|
  | `workplan-` | `#pj-<name>` | plan a mission; never executes it |
  | `workrun-` | a `work-<label>` channel, as `workrun-task<N>-<label>` | execute one Sub-Work |
  | `assetplan-` | `#pj-<name>`, as `assetplan-asset_<work id>` | agforge's asset conversation for one Work |
  | `bmining-` | `#pj-<name>` | unchanged by p3 |

  These were `mission-`, `run-` and `create-`. There is no compatibility
  shim: an old-prefix topic matches no sweep at all, and the whole realm's
  old-prefix topics were deleted at the cutover.
- **The agent configuration**: `agents.toml` (five roles, three profiles, the
  models behind them), the ignored `.local/agents.local.toml` overlay, the
  per-role tool grants in `src/agautolab/role_run.py`. Those grants are
  spelled twice, once per harness: `ROLE_ALLOWED_TOOLS` for `claude_code`, and
  the offered agcode tool set for the `local` profile — `director` and
  `summarizer` get `--tools read-only`, everyone else the full four.

Role resolution and execution are live: `GET /projects` and every window
answer resolve through `ag.agent-config.v1`, so a broken profile, missing
harness, or project selection fails loudly. Front and mediator runs use their
dedicated uv workspace directories.

```bash
uv run python agent/gateway.py     # the gateway
agent/zulip_listen.sh              # the chat entrance
```
