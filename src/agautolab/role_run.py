"""Resolve an agautolab role and launch its configured harness."""

from __future__ import annotations

from pathlib import Path

from agag.harness import run_harness, write_run_record

from .agent_settings import PROJECT_ROOT, resolve_project_role
from .project_settings import load_project_roles, project_name_from_direction

# The working grant shared by the roles that actually do work. `front` runs
# `uv run new_mission.py` in its own workspace, so it needs the same shell as
# `mediator`; keeping one string means a permission fix cannot land on only one
# of them.
WORKING_ALLOWED_TOOLS = (
    "Read,Write,Edit,Glob,Grep,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch,NotebookEdit,"
    "Bash(git:*),Bash(uv:*),Bash(uvx:*),Bash(curl:*),Bash(wget:*),Bash(node:*),"
    "Bash(npm:*),Bash(npx:*),Bash(python3:*),Bash(pip:*),Bash(jq:*),Bash(autolab:*),"
    "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*),Bash(sort:*),"
    "Bash(find:*),Bash(rg:*),Bash(sed:*),Bash(awk:*),Bash(mkdir:*),Bash(cp:*),"
    "Bash(mv:*),Bash(rm:*),Bash(chmod:*),Bash(touch:*),Bash(date:*),Bash(pwd:*),"
    "Bash(cd:*),Bash(which:*),Bash(env:*),Bash(sleep:*),Bash(kill:*),Bash(ps:*),Bash(echo:*),"
    "Bash(open:*),Bash(tar:*),Bash(make:*),Bash(bash:*),Bash(sh:*)"
)

ROLE_ALLOWED_TOOLS = {
    "front": WORKING_ALLOWED_TOOLS,
    # `director` records discussion notes into the direction clone it runs in
    # (brain_mining); recording is writing, so it gets the working set.
    "director": WORKING_ALLOWED_TOOLS,
    "summarizer": "Read,Glob,Grep",
    "mediator": WORKING_ALLOWED_TOOLS,
    # `coding` writes task files in whatever workspace its caller points it at.
    # Without an entry here `build_argv` omits `--allowedTools` entirely and
    # claude_code waits for an interactive permission answer until the timeout.
    "coding": WORKING_ALLOWED_TOOLS,
    # `superdirector` writes `plan.md` and the task split into the project
    # folder, and answers agforge's questions from it. It writes files, so it
    # gets the writable set rather than `director`'s read-only one.
    "superdirector": WORKING_ALLOWED_TOOLS,
}

# `front` is deliberately absent: the zulip listener runs it in the topic
# workspace and the gateway passes its own workspace, so the caller's cwd wins.
ROLE_WORKSPACES = {
    "mediator": PROJECT_ROOT / "agent" / "mediator",
}


# The roles that only read. Under claude_code that is ROLE_ALLOWED_TOOLS above;
# under agcode it is the offered tool set itself — agcode has no permission
# engine, so a read-only door is simply handed fewer tools.
READONLY_ROLES = {"summarizer"}


def _agcode_args(role: str) -> list[str]:
    return ["--tools", "read-only"] if role in READONLY_ROLES else []


def run_role(role: str, prompt: str, *, cwd: Path, timeout: float,
             profile: str | None = None, transcript: Path | None = None,
             record: Path | None = None,
             project: str | None = None) -> tuple[str, dict, int]:
    """Resolve `role`, run it once, and return output, record, and exit code."""
    project = project or (project_name_from_direction(cwd) if role == "director" else None)
    project_roles = load_project_roles(project)
    profile_override = profile or project_roles.get(role)
    agent = resolve_project_role(role, profile_override=profile_override)
    run_cwd = ROLE_WORKSPACES.get(role, cwd)
    result = run_harness(
        agent,
        prompt,
        cwd=run_cwd,
        timeout=timeout,
        allowed_tools=ROLE_ALLOWED_TOOLS.get(role),
        extra_args=_agcode_args(role) if agent.harness == "agcode" else None,
        transcript_path=transcript,
    )
    result.meta["project"] = project
    run_record = {"schema": "ag.agent-run.v1", **result.meta}
    if record:
        write_run_record(record, request_id=record.stem, meta=result.meta)
    return result.output, run_record, result.exit_code
