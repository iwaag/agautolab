"""Resolve an agautolab role and launch its configured harness."""

from __future__ import annotations

from collections.abc import Callable
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
    # folder. It writes files, so it gets the writable set rather than
    # `director`'s read-only one.
    "superdirector": WORKING_ALLOWED_TOOLS,
    # `supercoder` does the coding work of one `workrun-` topic in the project
    # folder and writes `report.md` into the serving workspace, so it gets the
    # same writable set as `coding`.
    "supercoder": WORKING_ALLOWED_TOOLS,
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


# agcode's built-in turn budget (20) starves real coding runs — every
# pj-foodchain work run died on turn_budget_exhausted (2026-08-18) — so hand
# it a budget large enough that the wall clock, not the turn counter, is the
# effective limit.
AGCODE_MAX_TURNS = 200

# agcode ends itself this many seconds before the caller's subprocess timeout
# would kill it, so a long run still reports its own outcome record instead of
# dying mid-turn.
AGCODE_DEADLINE_MARGIN_S = 60

# agcode's default response ceiling (4096) is too low for this work: a coding
# turn is a long thinking block plus a whole source file in one write call,
# and a foodchain run (2026-08-18) was cut off mid-file by it. agcode recovers
# from a cut-off turn now, but recovery costs a turn and re-plans work the
# model had already done, so the ceiling is raised to where a normal file
# write fits in one response.
AGCODE_MAX_TOKENS = 16384


def _agcode_args(role: str, timeout: float) -> list[str]:
    args = [
        "--max-turns", str(AGCODE_MAX_TURNS),
        "--max-tokens", str(AGCODE_MAX_TOKENS),
        "--deadline-s", str(max(60.0, timeout - AGCODE_DEADLINE_MARGIN_S)),
    ]
    if role in READONLY_ROLES:
        args += ["--tools", "read-only"]
    return args


def run_role(role: str, prompt: str, *, cwd: Path, timeout: float,
             profile: str | None = None, transcript: Path | None = None,
             record: Path | None = None,
             project: str | None = None,
             on_event: Callable[[dict], None] | None = None) -> tuple[str, dict, int]:
    """Resolve `role`, run it once, and return output, record, and exit code.

    `on_event` is run_harness's live-progress seam: when set, the harness
    streams its conversation events to it as the run proceeds.
    """
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
        extra_args=_agcode_args(role, timeout) if agent.harness == "agcode" else None,
        # claude_code's permission classifier blocks commands the allowlist
        # covers (seen 2026-08-18: `ls -la direction/ 2>&1` inside a compound
        # command, despite `Bash(ls:*)`), and a non-interactive run turns that
        # denial into a dead end. The roles are workspace-bound, so the
        # classifier is bypassed; the allowlist stays as documentation of
        # what a role is expected to reach for.
        skip_permissions=agent.harness == "claude_code",
        on_event=on_event,
        transcript_path=transcript,
    )
    result.meta["project"] = project
    run_record = {"schema": "ag.agent-run.v1", **result.meta}
    if record:
        write_run_record(record, request_id=record.stem, meta=result.meta)
    return result.output, run_record, result.exit_code
