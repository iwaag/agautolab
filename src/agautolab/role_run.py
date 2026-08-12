"""Role resolution for the stubbed node. Resolves; never launches.

This module used to resolve a role to a harness and then run it. The running
half is gone with the rest of the implementation (see the `discard_garbage`
episode). What remains is the half the episode keeps:

- the per-role tool grants and OpenCode permission-file mapping below, which
  are sub-agent configuration, not implementation;
- real resolution through `ag.agent-config.v1`, so `agents.toml`, the ignored
  `.local/agents.local.toml` overlay, and a project's own
  `.local/projects/<name>/agents.toml` are still read on every call and a
  broken selection still fails loudly.

`run_role` then returns a canned answer and a canonical `ag.agent-run.v1`
record carrying the identity that *would* have served the run. No subprocess
is started and nothing is ever charged.

Availability is deliberately not checked (`check_available=False`): a stub
that never launches a binary must not demand that the binary is installed.
Unknown roles and unknown profiles still raise `AgentConfigError`.
"""

from __future__ import annotations

import json
from pathlib import Path

from agag.harness import identity, write_run_record

from .agent_settings import PROJECT_ROOT, resolve_project_role
from .project_settings import load_project_roles, project_name_from_direction

ROLE_ALLOWED_TOOLS = {
    "front": "Read,Write,Edit,Glob,Grep,Bash(cd:*),Bash(uv run python -m agautolab.role_run director:*)",
    "director": "Read,Glob,Grep",
    "summarizer": "Read,Glob,Grep",
    "mediator": (
        "Read,Write,Edit,Glob,Grep,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch,NotebookEdit,"
        "Bash(git:*),Bash(uv:*),Bash(uvx:*),Bash(curl:*),Bash(wget:*),Bash(node:*),"
        "Bash(npm:*),Bash(npx:*),Bash(python3:*),Bash(pip:*),Bash(jq:*),Bash(autolab:*),"
        "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*),Bash(sort:*),"
        "Bash(find:*),Bash(rg:*),Bash(sed:*),Bash(awk:*),Bash(mkdir:*),Bash(cp:*),"
        "Bash(mv:*),Bash(rm:*),Bash(chmod:*),Bash(touch:*),Bash(date:*),Bash(pwd:*),"
        "Bash(cd:*),Bash(which:*),Bash(env:*),Bash(sleep:*),Bash(kill:*),Bash(ps:*),"
        "Bash(open:*),Bash(tar:*),Bash(make:*),Bash(bash:*),Bash(sh:*)"
    ),
}

STUB_REPLY = (
    "This autolab node is a stub. Its input/output surface and its sub-agent "
    "configuration are intact, but the implementation behind them has been "
    "removed, so no agent ran and this answer was not written by a model. "
    "The run record next to it names the role, profile, harness and model "
    "that would have served the request."
)


def _opencode_config(role: str) -> Path:
    if role in {"director", "summarizer"}:
        return PROJECT_ROOT / "agent" / "opencode-readonly.json"
    return PROJECT_ROOT / "agent" / f"opencode-{role}.json"


def run_role(role: str, prompt: str, *, cwd: Path, timeout: float,
             profile: str | None = None, transcript: Path | None = None,
             record: Path | None = None,
             project: str | None = None) -> tuple[str, dict, int]:
    """Resolve `role`, return the canned answer and its run record.

    Signature, exceptions and return shape are the ones the gateway already
    calls with. `prompt`, `timeout` and `transcript` are accepted and ignored:
    nothing runs, so there is no transcript to write and no budget to spend.
    """
    project = project or (project_name_from_direction(cwd) if role == "director" else None)
    project_roles = load_project_roles(project)
    profile_override = profile or project_roles.get(role)
    agent = resolve_project_role(role, profile_override=profile_override,
                                 check_available=False)
    meta = {
        **identity(agent),
        "outcome": "done",
        "stub": True,
        "duration_ms": 0,
        "cost_usd": 0.0,
        "num_turns": 0,
        "project": project,
    }
    run_record = {"schema": "ag.agent-run.v1", **meta}
    if record:
        write_run_record(record, request_id=record.stem, meta=meta)
        run_record = _reload_record(record, project)
    return STUB_REPLY, run_record, 0


def _reload_record(record: Path, project: str | None) -> dict:
    """Re-read what `write_run_record` normalized, then restore the two fields
    it drops: the project the run belonged to, and the stub marker. A reader
    of these files must never mistake one for a real run."""
    document = json.loads(record.read_text(encoding="utf-8"))
    document["project"] = project
    document["stub"] = True
    record.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document
