"""This autolab instance's own name and the spec the skeleton runs it by.

`autolab` is the agent; `autolab-agstudio1` is *this running instance of it*
(`<agent>-<instance label><N>`, the label being the host for now). The name
lives in `.local/instance.toml` (`instance.example.toml` shows the shape) and
`AUTOLAB_INSTANCE_NAME` overrides it — both read by `agag.agent.AgentSpec`.

Only the placement that runs the Zulip listener needs a name today: the
listener is what owns a channel and answers in it. A gateway-only node has
nothing to be addressed at and stays unnamed.

What is autolab's own is `SPEC`: its short name, its root, and its topic
vocabulary — `workplan-` plans and `workrun-` tasks, plus `bmining-`
brain-mining topics, all swept in the project channels autolab is subscribed
to. Its own channel is swept whole and is the entrance.

Since `runtime-profile` step3 the spec also carries autolab's **execution
options** (`ag.exec-options.v1`): the public names another agent may ask it to
run under. They are derived from `agents.toml` rather than written twice — a
name is published only if the profile behind it is actually configured — and
every one of them covers *every* role a mission uses, planning and task work
alike, because an option that only changed the entrance's answer would be a
menu that lies.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from agag.agent import AgentSpec
from agag.execopt import Option

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
FALLBACK_NAME = "autolab"

WORKPLAN_TOPIC_PREFIX = "workplan-"
WORKRUN_TOPIC_PREFIX = "workrun-"
BMINING_TOPIC_PREFIX = "bmining-"
PROJECT_CHANNEL_PREFIX = "pj-"
PROVISIONER_ENV = AGAUTOLAB_ROOT.parent / ".local" / "zulip" / "provisioner.env"
COMFYNOTIFY_BIN = AGAUTOLAB_ROOT.parent / "comfynotify" / ".venv" / "bin"


def extra_environment(_environment) -> dict[str, str]:
    """Paths to local capabilities offered to every role run."""
    environment = {"AGAG_ZULIP_ADMIN_ENV": str(PROVISIONER_ENV)}
    if COMFYNOTIFY_BIN.is_dir():
        environment["PATH"] = os.pathsep.join([
            str(COMFYNOTIFY_BIN), _environment.get("PATH", os.environ.get("PATH", "")),
        ])
    return environment

#: What autolab is willing to be asked for, and what each costs.
#: `(profile name, usage pool, one phrase)`. The profile name *is* the public
#: name — a one-to-one mapping, which the contract calls a valid start — so
#: there is nothing here to keep in step with `agents.toml` except whether the
#: profile exists at all, and `exec_options` checks that rather than trusting
#: this list. Everything else in `agents.toml` (`sonnet`, `local`, `stub`,
#: the per-role profiles) stays private: this is a menu, not a dump.
#:
#: The pool is the shared account window a condition like "until usage exceeds
#: 70 %" is judged against, and it is the reason `agy` and `agy-claude` are
#: separate names on one pool: the account is the cost, the model is the
#: choice (Agent ≠ Model).
PUBLIC_PROFILES = (
    ("agy", "antigravity", "Antigravity CLI (`agy`), Gemini 3.8 Flash"),
    ("agy-claude", "antigravity", "Antigravity CLI (`agy`), Claude Sonnet 4.6"),
    ("codex", "openai", "OpenAI Codex CLI, GPT-5.6"),
    ("gemini", "google", "Gemini CLI, Gemini 2.5 Flash"),
)
#: One phrase, and it has to be true: every role a mission uses runs under the
#: selected option — the entrance answer, the superdirector that plans, the
#: supercoder that does a task, the director that mines. Auxiliary roles do
#: not quietly stay on another pool, so there is no exception to explain.
COVERS = "my entrance, mission planning, task work and brain-mining"
#: The four roles that sentence is about, in the order a reader meets them,
#: and the roles the published pool is **derived** from (`agag.execpool`).
#: `mediator`, `coding` and `summarizer` are configured but no live serving
#: launches them, so they are not covered and not derived from.
EXEC_ROLES = ("front", "superdirector", "supercoder", "director")
#: What running under no selection costs, published like any other option:
#: a condition of the form "until the pool is N % used" cannot be judged
#: against a menu whose default declines to name a pool.
DEFAULT_OPTION_DETAIL = ("anthropic", "my configured defaults — Claude Sonnet 5 through claude_code")


def configured_profiles(path: Path | None = None) -> frozenset[str]:
    """The profile names `agents.toml` actually declares.

    Read straight, not through `load_config`: this runs at import time and a
    schema complaint here would take the listener down, while the only fact
    needed is which names exist. An unreadable config publishes nothing,
    which leaves a reader at *unknown* — the honest answer for an instance
    that cannot say.
    """
    try:
        data = tomllib.loads((path or (AGAUTOLAB_ROOT / "agents.toml")).read_text(encoding="utf-8"))
        return frozenset(data.get("profiles", {}))
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return frozenset()


def exec_options(path: Path | None = None) -> tuple[Option, ...]:
    """The menu, from the profiles this instance is actually configured with.

    A published option whose profile has been removed from `agents.toml` is
    not published at all — better than advertising a name that would fail at
    execution time, and it means the menu cannot drift from the configuration
    it is a view of.
    """
    profiles = configured_profiles(path)
    if not profiles:
        return ()
    pool, summary = DEFAULT_OPTION_DETAIL
    return (
        Option("default", pool, COVERS, summary),
        *(
            Option(name, option_pool, COVERS, option_summary)
            for name, option_pool, option_summary in PUBLIC_PROFILES
            if name in profiles
        ),
    )


SPEC = AgentSpec(
    FALLBACK_NAME, AGAUTOLAB_ROOT,
    plan_prefix=WORKPLAN_TOPIC_PREFIX,
    run_prefix=WORKRUN_TOPIC_PREFIX,
    extra_prefixes=(BMINING_TOPIC_PREFIX,),
    extra_environment=extra_environment,
    exec_options=exec_options(),
    exec_roles=EXEC_ROLES,
)

__all__ = [
    "AGAUTOLAB_ROOT", "BMINING_TOPIC_PREFIX", "FALLBACK_NAME", "PROJECT_CHANNEL_PREFIX",
    "COMFYNOTIFY_BIN", "COVERS", "DEFAULT_OPTION_DETAIL", "PROVISIONER_ENV", "PUBLIC_PROFILES", "SPEC",
    "WORKPLAN_TOPIC_PREFIX", "WORKRUN_TOPIC_PREFIX",
    "configured_profiles", "exec_options", "extra_environment", "instance_name",
]


def instance_name() -> str:
    """This instance's name, from `AUTOLAB_INSTANCE_NAME` or `.local/instance.toml`."""
    return SPEC.instance_name()
