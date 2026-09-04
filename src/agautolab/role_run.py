"""Run an agautolab role: the skeleton's `run_role`, with autolab's own budget.

The run itself is `agag.agent.run_role` — config pair, the `agentchat`
handover (`AGENTCHAT_ZULIP_ENV`, `AGENTCHAT_HOME`, PATH), the role's own grant
from `agents.toml` (`ag.agent-config.v2`), the `ag.agent-run.v1` record.
What is autolab's own:

- the per-project profile override (`.local/projects/<p>/agents.toml`, read by
  `project_settings.load_project_roles`), and for `director` the project
  inferred from the direction clone it runs in;
- `ROLE_WORKSPACES`: `mediator` runs in its fixed workspace, everything else
  where the caller points it;
- agcode's budget (`--max-turns`, `--max-tokens`, `--deadline-s`, and
  `--tools read-only` for the roles that only read);
- `skip_permissions` under claude_code: its permission classifier blocks
  commands the allowlist covers (seen 2026-08-18: `ls -la direction/ 2>&1`
  inside a compound command, despite `Bash(ls:*)`), and a non-interactive
  run turns that denial into a dead end. The roles are workspace-bound, so
  the classifier is bypassed; the grant in `agents.toml` stays as the
  statement of what a role is expected to reach for. gemini_cli gets the
  same bypass (`--approval-mode yolo`); its read-only roles get `plan`
  instead, the way agcode's get `--tools read-only`. agy has no read-only
  door — headless mode auto-denies reads too, and its `plan` mode writes a
  plan file instead of answering — so every role, `summarizer` included,
  runs on the bypass; the grant stays as documentation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agag.agent import resolve_spec_role, run_role as skeleton_run_role

from .instance import AGAUTOLAB_ROOT, SPEC
from .project_settings import load_project_roles, project_name_from_direction

PROJECT_ROOT = AGAUTOLAB_ROOT
#: This instance's Zulip credentials. A delegation is attributable to the
#: instance that made it.
ZULIP_ENV = SPEC.zulip_env

# `front` is deliberately absent: the zulip listener runs it in the topic
# workspace and the gateway passes its own workspace, so the caller's cwd wins.
ROLE_WORKSPACES = {
    "mediator": PROJECT_ROOT / "agent" / "mediator",
}

# The roles that only read. Under claude_code that is their `allowed_tools`
# grant; under agcode it is the offered tool set itself — agcode has no
# permission engine, so a read-only door is simply handed fewer tools.
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

__all__ = [
    "AGCODE_DEADLINE_MARGIN_S", "AGCODE_MAX_TOKENS", "AGCODE_MAX_TURNS", "PROJECT_ROOT",
    "READONLY_ROLES", "ROLE_WORKSPACES", "SPEC", "ZULIP_ENV", "agcode_args", "gemini_args",
    "run_role",
]


def agcode_args(role: str, timeout: float) -> list[str]:
    args = [
        "--max-turns", str(AGCODE_MAX_TURNS),
        "--max-tokens", str(AGCODE_MAX_TOKENS),
        "--deadline-s", str(max(60.0, timeout - AGCODE_DEADLINE_MARGIN_S)),
    ]
    if role in READONLY_ROLES:
        args += ["--tools", "read-only"]
    return args


def gemini_args(role: str) -> list[str]:
    """`plan` is gemini's read-only door; every other role runs on the bypass."""
    return ["--approval-mode", "plan"] if role in READONLY_ROLES else []


def harness_args(harness: str, role: str, timeout: float) -> list[str] | None:
    if harness == "agcode":
        return agcode_args(role, timeout)
    if harness == "gemini_cli":
        return gemini_args(role)
    return None


def run_role(role: str, prompt: str, *, cwd: Path, timeout: float,
             profile: str | None = None, transcript: Path | None = None,
             record: Path | None = None,
             project: str | None = None,
             home: tuple[str, str] | None = None,
             stream: bool = False,
             on_event: Callable[[dict], None] | None = None) -> tuple[str, dict, int]:
    """Resolve `role` (project profile first), run it once, return output, record, exit code.

    `on_event` is run_harness's live-progress seam: when set, the harness
    streams its conversation events to it as the run proceeds.
    """
    project = project or (project_name_from_direction(cwd) if role == "director" else None)
    profile = profile or load_project_roles(project).get(role)
    # The harness is decided by the profile; agcode's budget and claude_code's
    # bypass are decided here from that same resolution.
    agent = resolve_spec_role(SPEC, role, profile_override=profile, home=home)
    return skeleton_run_role(
        SPEC,
        role,
        prompt,
        cwd=ROLE_WORKSPACES.get(role, cwd),
        timeout=timeout,
        profile=profile,
        transcript=transcript,
        record=record,
        home=home,
        stream=stream,
        # A read-only gemini role must keep its `plan`: the bypass would turn
        # it into `yolo`.
        skip_permissions=agent.harness in ("claude_code", "agy")
        or (agent.harness == "gemini_cli" and role not in READONLY_ROLES),
        extra_args=harness_args(agent.harness, role, timeout),
        on_event=on_event,
        extra_meta={"project": project},
        agent=agent,
    )
