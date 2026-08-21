"""Close a mission Work whose tasks are all finished.

A mission is a Work with one Sub-Work per task. Each task is closed by the
run that executed it (`mission.report_work`), but nothing ever closed the
*parent*: `agent_standardize` p9 finished a mission and left its Work sitting
in `unstarted` with every child completed. That is the gap this command
exists to close, and it is deterministic on purpose — deciding that "all the
children are Done" is counting, not judgement.

It is also the only Plane operation the entrance performs. Everything the
entrance *reads* it reads from Zulip; the one thing that exists nowhere in
the chat is the mission Work's own state, so the one thing it writes is this.

    python -m agautolab.mission_done              # every [AUTO] project
    python -m agautolab.mission_done S2-30        # one Work, by label or id
    python -m agautolab.mission_done --dry-run    # say what would move

One line per Work, whether it moved or not, so the caller can report what
happened without asking Plane again. Exit 1 when a Work named explicitly cannot be closed — that is a question
answered "no", and it should not read as success. A Work that is *already*
Done is not that: like resolving a resolved topic, it is reported and exits
0.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from .mission import (
    EXTERNAL_SOURCE,
    MissionError,
    issue_label,
    list_issues,
    list_plane_projects,
    load_plane_config,
    project_slug,
    state_groups,
    state_id_for_group,
    sub_works,
    update_issue,
)

#: The refusal that means "you asked for a state it is already in". Like
#: `agentchat resolve` on a resolved topic, that is an answer, not a failure,
#: so it is reported and exits 0.
ALREADY_DONE = "already Done"

__all__ = [
    "ALREADY_DONE",
    "Candidate",
    "finished_missions",
    "main",
    "reason_not_finished",
]


@dataclass(frozen=True)
class Candidate:
    """One mission Work and what its children say about it."""

    issue: dict
    label: str
    project_id: str
    slug: str
    children: list[dict]


def reason_not_finished(issue: dict, children: list[dict], groups: dict[str, str]) -> str | None:
    """Why this Work may not be marked Done, or None when it may.

    A Work with no live children is not a mission — it is a task, or a Work
    nobody has planned yet — and closing one would be inventing a decision.
    """
    own = groups.get(str(issue.get("state") or ""))
    if own == "completed":
        return ALREADY_DONE
    if own == "cancelled":
        return "cancelled"
    if not children:
        return "no sub-work: this is not a mission"
    open_children = [
        child for child in children
        if groups.get(str(child.get("state") or "")) != "completed"
    ]
    if open_children:
        return f"{len(open_children)} of {len(children)} sub-works are not completed"
    return None


def finished_missions(issues: list[dict], groups: dict[str, str]) -> list[dict]:
    """The Works of this agent whose every live Sub-Work is completed.

    Only issues autolab itself registered (`external_source`) are considered:
    an issue somebody made by hand is not this command's business, however
    its children look.
    """
    finished = []
    for issue in issues:
        if str(issue.get("external_source") or "") != EXTERNAL_SOURCE:
            continue
        children = sub_works(issues, str(issue.get("id")), groups)
        if reason_not_finished(issue, children, groups) is None:
            finished.append(issue)
    return finished


def _projects(config) -> list[tuple[str, str, dict]]:
    """`(slug, project_id, project row)` for every `[AUTO]` Plane project."""
    rows = []
    for row in list_plane_projects(config):
        slug = project_slug(row)
        if slug and row.get("id"):
            rows.append((slug, str(row["id"]), row))
    return rows


def _matches(issue: dict, project_row: dict, wanted: str) -> bool:
    return wanted in {issue_label(project_row, issue).lower(), str(issue.get("id")).lower()}


def _candidates(config, target: str | None) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """`(closable, refused)` across every `[AUTO]` project.

    Without a target, only the closable ones are collected — a sweep says
    nothing about the missions still running. With one, the single Work it
    names is looked up and its refusal, if any, is carried back so the caller
    is told why rather than left with silence.
    """
    closable: list[Candidate] = []
    refused: list[tuple[Candidate, str]] = []
    for slug, project_id, project_row in _projects(config):
        issues = list_issues(config, project_id)
        groups = state_groups(config, project_id)
        if target is None:
            for issue in finished_missions(issues, groups):
                closable.append(
                    Candidate(issue, issue_label(project_row, issue), project_id, slug,
                              sub_works(issues, str(issue["id"]), groups))
                )
            continue
        for issue in issues:
            if not _matches(issue, project_row, target.lower()):
                continue
            children = sub_works(issues, str(issue["id"]), groups)
            candidate = Candidate(
                issue, issue_label(project_row, issue), project_id, slug, children
            )
            reason = reason_not_finished(issue, children, groups)
            (refused.append((candidate, reason)) if reason else closable.append(candidate))
    return closable, refused


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(
        prog="python -m agautolab.mission_done",
        description=(
            "Mark a mission Work Done once every one of its Sub-Works is "
            "completed. With no argument, every [AUTO] project is swept."
        ),
    )
    parser.add_argument(
        "work", nargs="?", default=None,
        help="one Work, by its Plane label (S2-30) or its id; default: sweep",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="say what would move, and move nothing",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        config = load_plane_config()
        closable, refused = _candidates(config, args.work)
        for candidate, reason in refused:
            print(f"{candidate.label} not moved: {reason}", file=out)
        for candidate in closable:
            title = str(candidate.issue.get("name", ""))
            done = len(candidate.children)
            if args.dry_run:
                print(f'{candidate.label} would be Done "{title}" ({done} sub-works)', file=out)
                continue
            update_issue(
                config, candidate.project_id, str(candidate.issue["id"]),
                {"state": state_id_for_group(config, candidate.project_id, "completed")},
            )
            print(f'{candidate.label} Done "{title}" ({done} sub-works)', file=out)
        if args.work is not None and not closable and not refused:
            print(f"agautolab.mission_done: no Work named {args.work}", file=err)
            return 1
        if not closable and not refused:
            print("no mission Work is ready to be Done", file=out)
        blocking = [reason for _, reason in refused if reason != ALREADY_DONE]
        return 1 if blocking and not closable else 0
    except MissionError as error:
        print(f"agautolab.mission_done: {error}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
