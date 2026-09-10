# agautolab

agautolab is a small chat-driven project and mission registration service.
The old headless development loop was deleted (`discard_garbage` episode,
`pj-agdev/devdocs/episodes/`), and the empty read-side surface it left behind
went with its last consumer in `refactor` p3 ex1.

What is kept is what was worth keeping — this node's input/output surface and
its agent configuration:

- **The gateway** (`agent/gateway.py`, default `:8791`). `POST /window` runs
  the real front agent with the request text unchanged, and `GET /healthz` is
  the deployment's liveness probe. Nothing else: the stub `/status`, `/log`,
  `/jobs…`, `/projects`, `/game` and `/monitor` routes were kept alive for
  `agdevworld`'s `autolab / now` view, which `refactor` p3 ex1 deleted, so
  they were deleted with it. This window is *the node's*; development work is
  asked for in the Zulip channel below.
- **The chat entrance** (`src/agautolab/listener.py` — `agag.agent.listener_main`
  over autolab's `SPEC`, routing to the handlers in `zulip_listener.py`;
  `agag_builder` p2): `workplan-*`
  topics in `#pj-<name>` channels are still heard and answered, and nothing is
  started. The topic prefix says what kind of request it is, and — since
  `agent_standardize` p3 (2026-08-21) — whether it plans or executes:

  | prefix | swept where | means |
  |---|---|---|
  | `workplan-` | `#pj-<name>` | plan a mission; never executes it |
  | `workrun-` | a `work-m<id>` channel, as `workrun-task<N>-m<id>` | execute one task |
  | `bmining-` | `#pj-<name>` | unchanged by p3 |

  These were `mission-` and `run-`. There is no compatibility
  shim: an old-prefix topic matches no sweep at all, and the whole realm's
  old-prefix topics were deleted at the cutover.
- **The instance and its own channel** (`agent_standardize` p4, 2026-08-21).
  The placement that runs this listener has a name — `.local/instance.toml`,
  one `name` key, `instance.example.toml` for the shape — and the Zulip
  channel of that name is its entrance. Every topic there is swept, and
  **none of them executes anything**: development work still goes in the
  project's `pj-<slug>` channel, because that channel is the only thing that
  says which project the work is for. The prefixes above still apply in
  every other subscribed channel.

  **Since `agent_standardize` p10 the entrance is a run, not a canned
  redirect** (`serve_entrance`, guide `agent/guides/entrance_front/guide.md`):
  `roles.front` over a generation workspace holding the conversation, with
  `agentchat` on PATH. Asked where its plans stand, it reads the board —
  `channels --prefix pj-` for the projects, their `workplan-` topics for the
  missions, `channels --prefix work-` whose descriptions name the mission
  each belongs to, and the `workrun-task<N>-…` topics inside them, `✔ ` for
  finished. Asked to close finished work out, it verifies by reading,
  `agentchat resolve`s the finished topics and runs `mission_done`. It never
  tidies on its own, and every question there is one paid `sonnet` run.

  A placement with no Zulip listener — the agautolab1 node — is deliberately
  left unnamed: it owns no channel and answers nothing, so a name would
  advertise an entrance that does not exist.
- **The introduction** (`params/intro.md`). autolab's self-description, and
  the contract another agent reads to learn all of the above. Post it with:

  ```bash
  uv run python -m agautolab.intro
  ```

  It appends to `#agents` under `intro-<instance>`, stamped with the date and
  the checked-out revision; nothing deduplicates, so the newest post is the
  current contract. `{instance}` in the file is filled in as it is posted, so
  the tracked file carries no host label. **Re-post it whenever the behavior
  it describes changes** — an agent that reads a stale introduction will act
  on it. Proven in p4: agfront reached this agent for the first time knowing
  nothing but that post.
- **The work record is the conversation** (`refactor` p1). A mission is the
  `workplan-` topic that asked for it, a task is the `workrun-` topic that
  runs it, and a result is a post where the task ran. There is no Plane Work
  and no Sub-Work: `agautolab.worklog` is the model, and `agautolab.anchor`
  is the four selfnotes a conversation carries about itself —

  | note | written in | says |
  |---|---|---|
  | `[selfnote][mission] <slug>` | a `workplan-` topic | this is a mission; **its own message id is the mission** |
  | `[selfnote][task] <mission id>#<serial>` | a `workrun-` topic | this is a task; **its own message id is the task** |
  | `[selfnote][doc] <message id>` | either | which post is the current plan or description |
  | `[selfnote][state] <word>` | either | where the work has got to; the newest wins |

  Identity is a **message id**, never a name: a topic may be resolved,
  renamed by hand, moved or replaced by other work of the same name, and the
  record still says which conversation is which. A deleted anchor is absent —
  not whatever took its name.

  A task is `open`, `completed`, `cancelled` or `accepted`. The three that
  are not `cancelled` are kept apart on purpose: `completed` is the run and
  the developer agreeing the work is done and is what the next task's gate
  waits for, `accepted` is a human accepting the request, and Zulip's `✔ ` is
  neither — it closes the conversation. A mission is `planned`, `started`,
  `cancelled` or `done`.
- **Marking a finished mission done** (`agent_standardize` p10, rebuilt in
  `refactor` p1). A task is closed by the run that executed it; nothing ever
  closed the mission above them, so p9 finished a mission and left it with
  four completed tasks and no state of its own. `mission_done` is the
  counting that closes it:

  ```bash
  uv run python -m agautolab.mission_done            # sweep pj- channels
  uv run python -m agautolab.mission_done m5512      # one mission, label or id
  uv run python -m agautolab.mission_done --dry-run  # say, move nothing
  ```

  It moves only missions whose every live task is finished; cancelled tasks
  do not hold a mission open, and a mission with no task never ran. One line
  per mission, moved or not. A named mission that is not finished says how
  far it is and exits 1; one already done is reported and exits 0.
- **The board harvest** (`agent_standardize` p6, 2026-08-21). Before a
  `workplan-` run and before a `workrun-` run, the latest introduction of
  every live `intro-*` topic in `#agents` is written verbatim into that run's
  workspace as `tools/agents.md` (`agag.intro.write_agents_md`, shared with
  agfront). That file is the only place a run learns another agent exists;
  no agent's name, channel or topic vocabulary is compiled in here. The runs
  also get `agentchat` on PATH and `AGENTCHAT_ZULIP_ENV` pointing at
  `.local/zulip.env`, so a task that delegates speaks as this instance.

  A delegating task spends its run waiting on another agent, which is why
  `WORK_TIMEOUT_SECONDS` is 3600 (planning and brain-mining stay at 1200 —
  they wait on nobody).
- **The agent configuration**: `agents.toml` (the roles, four profiles —
  `sonnet`, `local`, `gemini`, `stub` — and the models behind them; a
  `gemini` run records tokens but no cost, the CLI prints none). **Every role runs on `sonnet` since
  `agent_standardize` p10** — `roles.front` was the last committed `local`
  one, and its `nested_harness` requirement belonged to the in-process
  backend it used rather than to the role. The `local` profile is left
  defined and unused: Agent ≠ Model, and every run records its backend
  anyway, the ignored `.local/agents.local.toml` overlay, the
  per-role tool grants in `src/agautolab/role_run.py`. Those grants are
  spelled twice, once per harness: `ROLE_ALLOWED_TOOLS` for `claude_code`, and
  the offered agcode tool set for the `local` profile — `director` and
  `summarizer` get `--tools read-only`, everyone else the full four.

Role resolution and execution are live: every window answer and every listener
serving resolve through `ag.agent-config.v1`, so a broken profile, missing
harness, or project selection fails loudly. Front and mediator runs use their
dedicated uv workspace directories.

```bash
uv run python agent/gateway.py     # the gateway
agent/zulip_listen.sh              # the chat entrance
```
