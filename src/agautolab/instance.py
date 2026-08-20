"""This autolab instance's own name — the one place the code reads it.

`autolab` is the agent; `autolab-agstudio1` is *this running instance of it*
(`<agent>-<instance label><N>`, the label being the host for now). The name is
what the Zulip account, the instance's own channel and its `intro-<name>` topic
all agree on, so it lives in a file rather than being spelled out at each use
site.

Local-only because the label carries host information: `.local/instance.toml`
holds the real name, `instance.example.toml` shows the shape. With no local
file the plain agent name is used — wrong for an instance, but visibly wrong
rather than silently absent.

Only the placement that runs the Zulip listener needs a name today: the
listener is what owns a channel and answers in it. A node that runs the
gateway alone has nothing to be addressed at, so it is deliberately left
unnamed rather than given a name nothing reads.

The reading itself is `agag.instance.instance_name`, shared with the other
standardized agents; what is autolab's own is the three values below.
"""

from __future__ import annotations

from pathlib import Path

from agag.instance import instance_name as _instance_name

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_TOML = AGAUTOLAB_ROOT / ".local" / "instance.toml"
FALLBACK_NAME = "autolab"
INSTANCE_ENV_VAR = "AUTOLAB_INSTANCE_NAME"

__all__ = [
    "AGAUTOLAB_ROOT", "FALLBACK_NAME", "INSTANCE_ENV_VAR", "INSTANCE_TOML", "instance_name",
]


def instance_name(path: Path | None = None) -> str:
    """This instance's name, from `AUTOLAB_INSTANCE_NAME` or `instance.toml`."""
    return _instance_name(
        INSTANCE_TOML if path is None else path,
        fallback=FALLBACK_NAME,
        env_var=INSTANCE_ENV_VAR,
    )
