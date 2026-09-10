"""Close a mission whose tasks are all finished.

Each task is closed by the run that executed it (`worklog.record_result`),
but nothing ever closed the *mission*: `agent_standardize` p9 finished one and
left it sitting `planned` with every task completed. That is the gap this
command exists to close, and it is deterministic on purpose — deciding that
"all the tasks are finished" is counting, not judgement.

Until `refactor` p1 this was the entrance's one Plane operation: everything
the entrance *read* it read from Zulip, and the one thing that existed nowhere
in the chat was the mission Work's own state. It exists in the chat now — a
`[selfnote][state]` note in the `workplan-` topic — so this command reads and
writes the same conversations as everything else, and autolab has no Plane
credential at all.

    python -m agautolab.mission_done              # every project channel
    python -m agautolab.mission_done m5512        # one mission, by label or id
    python -m agautolab.mission_done --dry-run    # say what would move

One line per mission, whether it moved or not, so the caller can report what
happened without asking again. Exit 1 when a mission named explicitly cannot
be closed — that is a question answered "no", and it should not read as
success. A mission that is *already* done is not that: like resolving a
resolved topic, it is reported and exits 0.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

from agag.zulip import ZulipClient, ZulipError, log

from .instance import PROJECT_CHANNEL_PREFIX, SPEC, WORKPLAN_TOPIC_PREFIX
from .worklog import (
    MISSION_CANCELLED,
    MISSION_DONE,
    Mission,
    Task,
    WorklogError,
    mission_at,
    mission_tasks,
    read_mission,
    set_mission_state,
)

#: A mission that is already done, said the way a caller can recognise.
ALREADY_DONE = "it is already done"

#: `m5512` — how a mission is named on a command line.
LABEL = re.compile(r"^m?(?P<id>\d+)$", re.IGNORECASE)

__all__ = [
    "ALREADY_DONE",
    "Candidate",
    "finished_missions",
    "main",
    "reason_not_finished",
    "target_id",
]


@dataclass(frozen=True)
class Candidate:
    """One mission and what its tasks say about it."""

    mission: Mission
    tasks: list[Task]

    @property
    def label(self) -> str:
        return self.mission.label


def reason_not_finished(mission: Mission, tasks: list[Task]) -> str | None:
    """Why this mission may not be marked done, or None when it may.

    The rule the Plane era kept in `agag.plane.reason_not_completed`, said
    against the conversations: a mission with no live task never ran, a
    mission with an unfinished task is still running, and a cancelled mission
    is not something to finish.
    """
    if mission.state == MISSION_DONE:
        return ALREADY_DONE
    if mission.state == MISSION_CANCELLED:
        return "it is cancelled"
    if not tasks:
        return "it has no task, so there is nothing that could have finished"
    unfinished = [task for task in tasks if not task.finished]
    if unfinished:
        serials = ", ".join(str(task.serial) for task in unfinished)
        return f"task {serials} of {len(tasks)} is not finished"
    return None


def target_id(value: str) -> int | None:
    """The mission id a command-line argument names, or None."""
    match = LABEL.match(value.strip())
    return int(match.group("id")) if match else None


def _plan_topics(client: ZulipClient, channel: str) -> list[str]:
    try:
        return [
            name for name in client.channel_topics(client.stream_id(channel))
            if WORKPLAN_TOPIC_PREFIX in name
        ]
    except ZulipError as error:
        log(f"could not list {channel!r}: {error!r}")
        return []


def finished_missions(client: ZulipClient, self_id: int) -> list[Candidate]:
    """Every mission in every project channel whose tasks are all finished.

    Only the closable ones: a sweep says nothing about the missions still
    running, which is what keeps its output short enough to relay.
    """
    closable: list[Candidate] = []
    for row in client.channels():
        channel = str(row.get("name", ""))
        if not channel.startswith(PROJECT_CHANNEL_PREFIX):
            continue
        for topic in _plan_topics(client, channel):
            mission = read_mission(client, channel, topic, self_id)
            if mission is None:
                continue
            tasks = sorted(mission_tasks(client, mission, self_id).values(),
                           key=lambda task: task.serial)
            if reason_not_finished(mission, tasks) is None:
                closable.append(Candidate(mission, tasks))
    return closable


def _named(client: ZulipClient, self_id: int, mission_id: int) -> Candidate | None:
    """One mission, found through its anchor rather than by walking the realm."""
    mission = mission_at(client, mission_id, self_id)
    if mission is None:
        return None
    tasks = sorted(mission_tasks(client, mission, self_id).values(),
                   key=lambda task: task.serial)
    return Candidate(mission, tasks)


def main(argv: list[str] | None = None, out=None, err=None, client=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(
        prog="python -m agautolab.mission_done",
        description=(
            "Mark a mission done once every one of its tasks is finished. "
            "With no argument, every project channel is swept."
        ),
    )
    parser.add_argument(
        "mission", nargs="?", default=None,
        help="one mission, by its label (m5512) or its anchor id; default: sweep",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="say what would move, and move nothing",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        client = client or ZulipClient.from_env(SPEC.zulip_env)
        self_id = int(client.whoami()["user_id"])
        closable: list[Candidate] = []
        refused: list[tuple[Candidate, str]] = []
        if args.mission is None:
            closable = finished_missions(client, self_id)
        else:
            wanted = target_id(args.mission)
            candidate = _named(client, self_id, wanted) if wanted else None
            if candidate is not None:
                reason = reason_not_finished(candidate.mission, candidate.tasks)
                (refused.append((candidate, reason)) if reason
                 else closable.append(candidate))

        for candidate, reason in refused:
            print(f"{candidate.label} not moved: {reason}", file=out)
        for candidate in closable:
            title = candidate.mission.title
            count = len(candidate.tasks)
            if args.dry_run:
                print(f'{candidate.label} would be done "{title}" ({count} tasks)', file=out)
                continue
            set_mission_state(client, candidate.mission, MISSION_DONE)
            print(f'{candidate.label} done "{title}" ({count} tasks)', file=out)
        if args.mission is not None and not closable and not refused:
            print(f"agautolab.mission_done: no mission named {args.mission}", file=err)
            return 1
        if not closable and not refused:
            print("no mission is ready to be done", file=out)
        blocking = [reason for _, reason in refused if reason != ALREADY_DONE]
        return 1 if blocking and not closable else 0
    except (WorklogError, ZulipError) as error:
        print(f"agautolab.mission_done: {error}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
