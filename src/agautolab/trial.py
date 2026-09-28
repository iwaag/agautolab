"""`python -m agautolab.trial <probe> --out <dir>` — one autolab serving of a
fixture probe (`agent_guide` p2 ex1; p2 step 7's drivers).

- `planner-other-project`: the planner (`superdirector`) in a `workplan-`
  topic, with a small stand-in project under `<out>/run/project` (the real
  projects are not touched) and the chatlog and board files autolab's
  listener would give it.
- `entrance-plans`: autolab's entrance (`agag.entrance.serve_entrance`).

Every run's `agentchat` reads the fixture board and posts nowhere. `--guides
<tree>` or `--guides-rev <commit>` serves with another `agent/guides` tree,
`--no-shared` without pyagag's shared sections, `--dry-run` writes the prompt
and runs no model. The outcome goes to `<out>/outcome.json` and `reply.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agag.fixture.board import AUTOLAB
from agag.fixture.run import Trial, client, newest, probe_history, session_log, tool_calls, trial_parser
from agag.topics import TopicContext

#: This checkout, where `--guides-rev` is looked up.
ROOT = Path(__file__).resolve().parents[2]

PROJECT_README = "# protoprey\n\n- `main/` — the game (repository, pushed).\n- `direction/` — decisions (repository).\n"
PROJECT_INDEX = "# ProtoPrey\n\nv0.1.0: one hunt across three locations.\n"


def planner(trial: Trial) -> int:
    from agag.agent import run_role
    from agag.intro import write_agents_md
    from agag.topics import chatlog_path, format_chatlog, next_record_path

    from . import zulip_listener as zl

    probe = trial.probe
    root = trial.out / "run"
    workspace, project = root / "workspace", root / "project"
    (project / "main").mkdir(parents=True, exist_ok=True)
    (project / "README_PROJECT.md").write_text(PROJECT_README, encoding="utf-8")
    (project / "main" / "INDEX.md").write_text(PROJECT_INDEX, encoding="utf-8")
    workspace.mkdir(parents=True, exist_ok=True)
    chatlog_path(workspace).write_text(format_chatlog(probe_history(probe), AUTOLAB), encoding="utf-8")
    write_agents_md(client(trial.store), workspace)
    records = zl.SPEC.records_root / "superdirector"
    with trial.session():
        prompt = zl.superdirector_prompt("autolab-agstudio1", workspace, False)
        (trial.out / "prompt.md").write_text(prompt, encoding="utf-8")
        output, _, code = run_role(zl.SPEC, "superdirector", prompt, cwd=project, timeout=600,
                                   record=next_record_path(records), home=(probe.channel, probe.topic))
    written = sorted(p.name for p in workspace.iterdir() if p.name not in ("chatlog.md", "tools"))
    return trial.finish(output, records=records, role="superdirector", exit_code=code, files_written=written,
                        calls=tool_calls(session_log(project)))


def entrance(trial: Trial) -> int:
    from agag import entrance as entrance_module

    from . import zulip_listener as zl

    probe = trial.probe
    context = TopicContext(client(trial.store), probe.channel, probe.topic, AUTOLAB, "autolab-agstudio1",
                           history=probe_history(probe))
    if trial.guides is not None:
        original = entrance_module.entrance_guide
        entrance_module.entrance_guide = lambda spec: original(_GuidesAt(spec, trial.guides))
    with trial.session():
        result = entrance_module.serve_entrance(zl.SPEC, context)
    workspace = newest(zl.SPEC.topics_root / probe.channel / probe.topic, "*")
    calls = (tool_calls(workspace / "front" / "transcript.jsonl") or tool_calls(session_log(workspace / "front"))
             if workspace is not None else [])
    return trial.finish(result.output or "", records=zl.SPEC.records_root / "entrance_front", role="entrance_front",
                        calls=calls)


class _GuidesAt:
    """An agent spec whose guides are another tree."""

    def __init__(self, spec, guides: Path):
        self._spec, self.guides = spec, guides

    def __getattr__(self, name):
        return getattr(self._spec, name)


RUNNERS = {"planner-other-project": planner, "entrance-plans": entrance}


def main(argv: list[str] | None = None) -> int:
    args = trial_parser("python -m agautolab.trial", __doc__, "agautolab").parse_args(argv)
    trial = Trial.start(args, ROOT)
    from . import zulip_listener

    # The listener keeps both roots as constants taken at import.
    zulip_listener.TOPICS_ROOT = zulip_listener.SPEC.topics_root
    zulip_listener.RECORDS_ROOT = zulip_listener.SPEC.records_root
    if trial.guides is not None:
        zulip_listener.GUIDES = trial.guides
    return RUNNERS[trial.probe.name](trial)


if __name__ == "__main__":
    sys.exit(main())
