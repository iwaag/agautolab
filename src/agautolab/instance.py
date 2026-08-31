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
"""

from __future__ import annotations

import os
from pathlib import Path

from agag.agent import AgentSpec

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

SPEC = AgentSpec(
    FALLBACK_NAME, AGAUTOLAB_ROOT,
    plan_prefix=WORKPLAN_TOPIC_PREFIX,
    run_prefix=WORKRUN_TOPIC_PREFIX,
    extra_prefixes=(BMINING_TOPIC_PREFIX,),
    extra_environment=extra_environment,
)

__all__ = [
    "AGAUTOLAB_ROOT", "BMINING_TOPIC_PREFIX", "FALLBACK_NAME", "PROJECT_CHANNEL_PREFIX",
    "COMFYNOTIFY_BIN", "PROVISIONER_ENV", "SPEC", "WORKPLAN_TOPIC_PREFIX", "WORKRUN_TOPIC_PREFIX",
    "extra_environment", "instance_name",
]


def instance_name() -> str:
    """This instance's name, from `AUTOLAB_INSTANCE_NAME` or `.local/instance.toml`."""
    return SPEC.instance_name()
