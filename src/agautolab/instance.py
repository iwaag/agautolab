"""What the workflow executor needs to know about this checkout.

`SPEC` is autolab as `agag.agent` resolves it: its short name and its root,
which locate `agents.toml`, the `.local/agents.local.toml` overlay and the
bot credential `.local/zulip.env` that the notifier and intake post and read
with. autolab sweeps no topics: the executor is not a listener.

`PROVISIONER_ENV` is the realm credential the notifier uses to create a
project's channel folder and a workflow's channel when the editor asks.
"""

from __future__ import annotations

from pathlib import Path

from agag.agent import AgentSpec

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
PROVISIONER_ENV = AGAUTOLAB_ROOT.parent / ".local" / "zulip" / "provisioner.env"

SPEC = AgentSpec("autolab", AGAUTOLAB_ROOT)

__all__ = ["AGAUTOLAB_ROOT", "PROVISIONER_ENV", "SPEC"]
