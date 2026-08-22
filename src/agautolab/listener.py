"""autolab's chat entrance: the agag skeleton with autolab's routes.

`listener_main` sweeps every topic in this instance's own channel and
`workplan-`/`workrun-`/`bmining-` topics in any subscribed channel. A topic
matching no route is in the own channel and goes to `agag.entrance`: a
`front` run that reads the conversation and answers about this instance's
work (`agent/guides/entrance_front/guide.md` is its guide). Mentions of this
instance elsewhere bring the task that delegated back (`handle_mention`).

No subscription reconciliation here: what this listener is subscribed to is
the project creator's decision about who the work goes to. The one thing
that widens it is a run posting somewhere — `agentchat send` joins the
channel it posts into, because being in the room is what makes the answer
arrive. Posting *is* the routing decision; a listener guessing is not.
"""

from __future__ import annotations

from agag.agent import listener_main

from .instance import BMINING_TOPIC_PREFIX, SPEC, WORKPLAN_TOPIC_PREFIX, WORKRUN_TOPIC_PREFIX
from .zulip_listener import handle_bmining, handle_mention, handle_topic, handle_workrun


def main() -> None:
    listener_main(
        SPEC,
        {
            WORKPLAN_TOPIC_PREFIX: handle_topic,
            WORKRUN_TOPIC_PREFIX: handle_workrun,
            BMINING_TOPIC_PREFIX: handle_bmining,
        },
        on_mention=handle_mention,
    )


if __name__ == "__main__":
    main()
