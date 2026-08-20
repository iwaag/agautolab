"""Post this autolab instance's fixed introduction to the shared agents board.

`uv run python -m agautolab.intro` appends `params/intro.md` to the `agents`
channel under `intro-<instance>`, stamped with today's date and the checked-out
revision. Run it whenever the introduction or the behavior it describes
changes; the topic is append-only history, so the newest post is the current
contract and the older ones stay readable.
"""

from __future__ import annotations

from agag.intro import AGENTS_CHANNEL, intro_topic, post_intro
from agag.zulip import ZulipClient

from .instance import AGAUTOLAB_ROOT, instance_name
from .zulip_listener import ZULIP_ENV

INTRO_PATH = AGAUTOLAB_ROOT / "params" / "intro.md"

__all__ = ["AGENTS_CHANNEL", "INTRO_PATH", "main", "topic"]


def topic() -> str:
    return intro_topic(instance_name())


def main() -> None:
    """Append the current introduction to #agents for this instance."""
    client = ZulipClient.from_env(ZULIP_ENV)
    post_intro(client, instance=instance_name(), intro_path=INTRO_PATH, root=AGAUTOLAB_ROOT)


if __name__ == "__main__":
    main()
