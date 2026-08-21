"""Plane mirror of one chat topic: its Work, Sub-Works, and their files.

The Plane client itself is `agag.plane`, shared machinery. What lives here
is autolab's own policy on top of it:

- the `AUTO` label, which is what makes an issue eligible for automatic
  execution (`next_work`), and the `[AUTO]` project marker that decides which
  projects are scanned at all
- Sub-Work keys (`<channel>/<topic>#<N>`), one per task serial for the life of
  the mission: re-planning updates the issue behind a serial rather than
  cancelling it, which is what lets a completed task stay completed. Legacy
  keys carry an `@<rev>` generation marker before the `#<N>` tail; they still
  match, because matching reads the serial, not the whole key
- which Work to execute next, and how a finished one is reported back

Both directions still live here. Reading, a topic's Work becomes `mission.md`
and its live Sub-Works `task1.md`, `task2.md`, … in the serving workspace's
`current/` mirror (`write_mission_workspace`). Writing, the superdirector's
`plan.md` updates or creates the Work (`upsert_work`) and its `task[N].md`
files are reconciled against the live Sub-Works by serial
(`reconcile_task_files`): a serial that exists is updated in place with its
**state untouched**, a new serial is created, a disappeared one is cancelled.
Sub-Works are never deleted. Only a mission-level cancel still cancels
everything at once (`cancel_sub_works`).

Works are keyed on Plane's own `external_source`/`external_id` pair, so a key
survives a wiped `.local/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agag.plane import (
    TITLE_LIMIT,
    PlaneConfig,
    PlaneError as MissionError,
    add_comment,
    compose_document,
    create_label,
    description_html,
    ensure_issue as shared_ensure_issue,
    find_issue_by_external as shared_find_issue_by_external,
    html_to_text,
    issue_label,
    labels_by_name,
    list_issues,
    list_projects,
    load_plane_config as load_shared_plane_config,
    normalized_name as _normalized_name,
    split_document,
    starting_state_id,
    state_groups,
    state_id_for_group,
    update_issue,
)

from .project_init import AUTO_MARKER, PLANE_ENV, PROJECT_NAME  # noqa: F401

EXTERNAL_SOURCE = "agautolab"
AUTO_LABEL = "AUTO"

TASK_FILE = re.compile(r"^task(?P<number>\d+)\.md$")
SUB_WORK_SERIAL = re.compile(r"#(?P<number>\d+)\s*$")
# What `sub_work_serial` answers for a key that carries no `#<N>` tail: a
# Work, or an issue somebody made by hand. It sorts last, and the reconcile
# leaves such a child alone rather than cancelling it.
NO_SERIAL = 1 << 30

_LABEL_CACHE: dict[tuple[str, str], str] = {}

__all__ = [
    "AUTO_LABEL",
    "EXTERNAL_SOURCE",
    "TITLE_LIMIT",
    "MissionError",
    "NO_SERIAL",
    "PlaneConfig",
    "RunTarget",
    "TaskChange",
    "Work",
    "add_comment",
    "cancel_sub_works",
    "compose_document",
    "description_html",
    "eligible_works",
    "ensure_issue",
    "ensure_label",
    "find_issue_by_external",
    "find_plane_project",
    "html_to_text",
    "issue_label",
    "list_issues",
    "list_plane_projects",
    "load_plane_config",
    "next_work",
    "reconcile_task_files",
    "report_work",
    "run_target",
    "split_document",
    "starting_state_id",
    "state_groups",
    "state_id_for_group",
    "sub_work_key",
    "sub_work_serial",
    "sub_works",
    "task_files",
    "transition_work",
    "update_issue",
    "upsert_work",
    "write_mission_workspace",
]


def load_plane_config(path: Path | None = None) -> PlaneConfig:
    return load_shared_plane_config(path or PLANE_ENV)


def work_key(channel: str, topic: str) -> str:
    return f"{channel}/{topic}"


def sub_work_key(channel: str, topic: str, number: int) -> str:
    """One key per task serial, for the life of the mission.

    Re-planning reconciles onto these keys instead of minting a new
    generation, so a Sub-Work that is already Done keeps its identity and its
    state. Keys written before that decision carry an `@<rev>` marker between
    the topic and the `#<N>` tail; `sub_work_serial` reads the tail, so old
    children still match their serial without any migration.
    """
    return f"{channel}/{topic}#{number}"


def find_issue_by_external(config: PlaneConfig, project_id: str, external_id: str) -> dict | None:
    return shared_find_issue_by_external(config, project_id, EXTERNAL_SOURCE, external_id)


def ensure_issue(
    config: PlaneConfig,
    project_id: str,
    *,
    name: str,
    description: str,
    state: str,
    external_id: str,
    parent: str | None = None,
    labels: list[str] | None = None,
) -> tuple[dict, bool]:
    """Create at most one issue for `external_id`, carrying the `AUTO` label.

    Every issue autolab creates — the mission Work and its Sub-Works alike —
    carries that label, which is what makes it eligible for automatic
    execution later (`next_work`).

    `labels` names further labels to attach, `AUTO` first and always. Names,
    not ids: creating them on first use is `ensure_label`'s job, so a caller
    does not have to know how a Plane label comes into being.
    """
    names = [AUTO_LABEL, *(label for label in (labels or []) if label != AUTO_LABEL)]
    return shared_ensure_issue(
        config,
        project_id,
        name=name,
        description=description,
        state=state,
        external_source=EXTERNAL_SOURCE,
        external_id=external_id,
        parent=parent,
        labels=[ensure_label(config, project_id, label) for label in names],
    )


# --- files -----------------------------------------------------------------


def task_files(directory: Path) -> list[tuple[int, Path]]:
    """`task[N].md` files in numeric order. Anything else in the directory —
    the chatlog and the `current/` mirror included — is simply not a task."""
    tasks: list[tuple[int, Path]] = []
    if not directory.is_dir():
        return tasks
    for path in directory.iterdir():
        match = TASK_FILE.fullmatch(path.name)
        if match and path.is_file():
            tasks.append((int(match.group("number")), path))
    tasks.sort(key=lambda item: item[0])
    return tasks


# --- projects --------------------------------------------------------------


def list_plane_projects(config: PlaneConfig) -> list[dict]:
    return list_projects(config)


def find_plane_project(config: PlaneConfig, project: str) -> dict:
    wanted = _normalized_name(project)
    match = next(
        (
            row
            for row in list_plane_projects(config)
            if _normalized_name(str(row.get("name", ""))) == wanted
        ),
        None,
    )
    if not match or not match.get("id"):
        raise MissionError(f"Plane project does not exist: {project}")
    return match


def project_slug(row: dict) -> str | None:
    """Recover the local slug of an `[AUTO]` Plane project, or None."""
    description = str(row.get("description") or "").strip()
    if not description.upper().startswith(AUTO_MARKER):
        return None
    remainder = description[len(AUTO_MARKER):].strip()
    _, _, tail = remainder.partition(":")
    slug = _normalized_name(tail if tail.strip() else str(row.get("name", "")))
    return slug or None


# --- labels ----------------------------------------------------------------


def ensure_label(config: PlaneConfig, project_id: str, name: str = AUTO_LABEL) -> str:
    """The project's label id for `name`, creating the label on first use.

    Cached per (project, name) for the process: labels are created once and
    never renamed, and this is called for every issue autolab writes.
    """
    key = (project_id, name.lower())
    if key in _LABEL_CACHE:
        return _LABEL_CACHE[key]
    existing = labels_by_name(config, project_id)
    if name.lower() not in existing:
        if created := create_label(config, project_id, name):
            _LABEL_CACHE[key] = created
            return created
        existing = labels_by_name(config, project_id)  # lost a race, or name taken
    if name.lower() not in existing:
        raise MissionError(f"Plane project has no label {name!r} and it could not be created")
    _LABEL_CACHE[key] = existing[name.lower()]
    return _LABEL_CACHE[key]


# --- sub-works -------------------------------------------------------------


def sub_works(issues: list[dict], parent_id: str, groups: dict[str, str]) -> list[dict]:
    """Non-cancelled children of one issue, in sequence order.

    Plane CE v1.4.1 ignores a `?parent=` filter and 404s the `sub-issues`
    endpoint, so children are filtered out of the full list client-side.
    """
    children = [
        row
        for row in issues
        if str(row.get("parent") or "") == parent_id
        and groups.get(str(row.get("state") or "")) != "cancelled"
    ]
    children.sort(key=lambda row: (row.get("sequence_id") or 0, str(row.get("id"))))
    return children


def write_mission_workspace(directory: Path, project: str, channel: str, topic: str) -> bool:
    """Mirror the topic's Plane Work into `directory` as files.

    Writes `mission.md` (title + description) when a Work keyed
    `<channel>/<topic>` exists, and its non-cancelled Sub-Works in sequence
    order as `task1.md`, `task2.md`, …. Returns whether anything was written.
    """
    config = load_plane_config()
    plane_project = find_plane_project(config, project)
    project_id = str(plane_project["id"])
    issue = find_issue_by_external(config, project_id, work_key(channel, topic))
    if not issue:
        return False
    issues = list_issues(config, project_id)
    issue = next((row for row in issues if str(row.get("id")) == str(issue["id"])), issue)
    (directory / "mission.md").write_text(
        compose_document(str(issue.get("name", "")), issue.get("description_html")),
        encoding="utf-8",
    )
    groups = state_groups(config, project_id)
    for number, child in enumerate(sub_works(issues, str(issue["id"]), groups), start=1):
        (directory / f"task{number}.md").write_text(
            compose_document(str(child.get("name", "")), child.get("description_html")),
            encoding="utf-8",
        )
    return True


# --- choosing the next work to execute -------------------------------------


def sub_work_serial(external_id: str | None) -> int:
    """The `#<N>` tail of a Sub-Work external id.

    Works and hand-made issues have no serial; they sort last within the same
    creation timestamp, which is all this number is used for.
    """
    match = SUB_WORK_SERIAL.search(str(external_id or ""))
    return int(match.group("number")) if match else NO_SERIAL


def eligible_works(issues: list[dict], groups: dict[str, str], label_id: str) -> list[dict]:
    """Issues that may be executed automatically, in execution order.

    Eligible means: carries the `AUTO` label, sits in an `unstarted` state,
    and has no sub-work at all — a parent is executed through its children,
    so running it too would do the work twice. Order is creation time first,
    then the Sub-Work serial number.
    """
    parents = {str(row.get("parent") or "") for row in issues} - {""}
    matches = [
        row
        for row in issues
        if label_id in {str(value) for value in (row.get("labels") or [])}
        and groups.get(str(row.get("state") or "")) == "unstarted"
        and str(row.get("id")) not in parents
    ]
    matches.sort(key=lambda row: (str(row.get("created_at") or ""),
                                  sub_work_serial(row.get("external_id")),
                                  str(row.get("id"))))
    return matches


@dataclass(frozen=True)
class Work:
    """One chosen Work, with everything a `workrun-` serving needs of it."""

    slug: str
    name: str
    description: str
    project_id: str
    issue_id: str


def next_work() -> Work | None:
    """The next Work to execute.

    Scans every `[AUTO]` project in the workspace and returns the first
    eligible issue across all of them. `None` when nothing is eligible.
    """
    config = load_plane_config()
    candidates: list[tuple[tuple, str, str, str, dict]] = []
    for row in list_plane_projects(config):
        slug = project_slug(row)
        if not slug or not row.get("id"):
            continue
        project_id = str(row["id"])
        labels = labels_by_name(config, project_id)
        label_id = labels.get(AUTO_LABEL.lower())
        if not label_id:
            continue  # no AUTO label in this project means no automatic work
        issues = list_issues(config, project_id)
        groups = state_groups(config, project_id)
        for issue in eligible_works(issues, groups, label_id):
            candidates.append(
                (
                    (str(issue.get("created_at") or ""),
                     sub_work_serial(issue.get("external_id")),
                     str(issue.get("id"))),
                    slug,
                    project_id,
                    issue,
                )
            )
    if not candidates:
        return None
    _, slug, project_id, issue = min(candidates, key=lambda item: item[0])
    return Work(
        slug,
        str(issue.get("name", "")),
        html_to_text(issue.get("description_html")),
        project_id,
        str(issue["id"]),
    )


# --- the Sub-Work one workrun- topic is bound to ---------------------------


@dataclass(frozen=True)
class RunTarget:
    """The Sub-Work a `workrun-` topic serves, and the gate in front of it.

    `blocked_by` is the label of the immediately preceding task when that one
    is not in a `completed` state — the handler answers with it and never
    launches an agent. Serial 1 has no predecessor and is never blocked.

    `mission_label` and `mission_title` come along for the devlog: a task's
    record is filed under its mission, and the directory is named at the first
    write from both.
    """

    work: Work
    label: str
    serial: int
    mission_label: str
    mission_title: str
    blocked_by: str | None = None


def run_target(project: str, channel: str, topic: str, serial: int) -> RunTarget:
    """Look up task `serial` of the mission Work keyed `<channel>/<topic>`.

    Reads the whole issue list once and answers both the task and its gate
    from it: Plane CE ignores a `?parent=` filter, so children are found
    client-side anyway.
    """
    config, plane_project, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, work_key(channel, topic))
    if not issue:
        raise MissionError(f"no Work is registered for {channel}/{topic}")
    issues = list_issues(config, project_id)
    issue = next((row for row in issues if str(row.get("id")) == str(issue["id"])), issue)
    groups = state_groups(config, project_id)
    by_serial = {
        sub_work_serial(child.get("external_id")): child
        for child in sub_works(issues, str(issue["id"]), groups)
    }
    task = by_serial.get(serial)
    if task is None:
        raise MissionError(f"{channel}/{topic} has no live task {serial}")

    blocked_by = None
    if previous := by_serial.get(serial - 1):
        if groups.get(str(previous.get("state") or "")) != "completed":
            blocked_by = issue_label(plane_project, previous)

    work = Work(
        project,
        str(task.get("name", "")),
        html_to_text(task.get("description_html")),
        project_id,
        str(task["id"]),
    )
    return RunTarget(
        work,
        issue_label(plane_project, task),
        serial,
        issue_label(plane_project, issue),
        str(issue.get("name", "")),
        blocked_by,
    )


def report_work(
    project_id: str, issue_id: str, report: str | None, success: bool
) -> tuple[str, bool, bool]:
    """Write one executed Work's outcome back to Plane.

    Comments `report` on the issue when there is one, and moves the issue to
    the project's `completed` state when the run reported success. Returns
    `(work label, commented, completed)` for the chat outcome line.
    """
    config = load_plane_config()
    project_row = next(
        (row for row in list_plane_projects(config) if str(row.get("id")) == project_id), {}
    )
    issue = next(
        (row for row in list_issues(config, project_id) if str(row.get("id")) == issue_id),
        {"id": issue_id},
    )
    label = issue_label(project_row, issue)
    commented = bool(report and report.strip())
    if commented:
        add_comment(config, project_id, issue_id, report.strip())
    if success:
        update_issue(
            config, project_id, issue_id,
            {"state": state_id_for_group(config, project_id, "completed")},
        )
    return label, commented, success


# --- writing the superdirector's decisions back to Plane -------------------


def _prepare(project: str) -> tuple[PlaneConfig, dict, str]:
    config = load_plane_config()
    plane_project = find_plane_project(config, project)
    return config, plane_project, str(plane_project["id"])


def upsert_work(
    project: str, channel: str, topic: str, title: str, description: str
) -> tuple[str, str]:
    """Update the topic's Work with the new title/description, creating it if
    this is the topic's first mission.

    Returns `(report line, work label)`. The label travels because the caller
    names the mission's Zulip channel and its `workrun-` topics after it
    (`work-pa-12`, `workrun-task1-pa-12`), and re-looking the Work up would
    be a second round trip for something this call already holds. It is the
    label rather than the issue because composing one needs the Plane *project* row
    too, and that row only exists in here.
    """
    config, plane_project, project_id = _prepare(project)
    key = work_key(channel, topic)
    if existing := find_issue_by_external(config, project_id, key):
        update_issue(
            config,
            project_id,
            str(existing["id"]),
            {"name": title.strip(), "description_html": description_html(description)},
        )
        label = issue_label(plane_project, existing)
        return f'updated {label} "{title}"', label
    issue, _ = ensure_issue(
        config,
        project_id,
        name=title,
        description=description,
        state=starting_state_id(config, project_id),
        external_id=key,
    )
    label = issue_label(plane_project, issue)
    return f'created {label} "{title}"', label


def cancel_sub_works(project: str, channel: str, topic: str) -> int:
    """Transition every non-cancelled Sub-Work of the topic's Work to
    Cancelled. Sub-Works are never deleted. Returns how many moved."""
    config, _, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, work_key(channel, topic))
    if not issue:
        return 0
    groups = state_groups(config, project_id)
    cancelled = state_id_for_group(config, project_id, "cancelled")
    children = sub_works(list_issues(config, project_id), str(issue["id"]), groups)
    for child in children:
        update_issue(config, project_id, str(child["id"]), {"state": cancelled})
    return len(children)


def transition_work(project: str, channel: str, topic: str, group: str) -> str:
    """Move the topic's Work to the project's state in `group`. Returns the
    Work's label."""
    config, plane_project, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, work_key(channel, topic))
    if not issue:
        raise MissionError(f"no Work is registered for {channel}/{topic}")
    update_issue(
        config, project_id, str(issue["id"]),
        {"state": state_id_for_group(config, project_id, group)},
    )
    return issue_label(plane_project, issue)


@dataclass(frozen=True)
class TaskChange:
    """What one re-plan did to one task serial, for the Zulip side to mirror.

    `document` is the task as the developer should read it — the same
    `# title\n\nbody` shape the superdirector wrote.
    """

    serial: int
    #: `created` | `updated` | `unchanged` | `cancelled` | `changed-after-done`
    action: str
    title: str
    document: str
    label: str


def reconcile_task_files(
    project: str, channel: str, topic: str, plan_dir: Path
) -> tuple[list[str], list[TaskChange]]:
    """Reconcile the topic's live Sub-Works against the `task[N].md` set.

    Matching is by **serial** — the `#<N>` tail of the external id, which old
    `@<rev>`-keyed children carry too, so no migration is needed:

    - the serial exists → title and description are updated in place and the
      **state is left alone**, which is what keeps a completed task completed
      and the `workrun-` gate meaningful;
    - the serial is new → the Sub-Work is created, keyed `<channel>/<topic>#<N>`;
    - the serial disappeared from the split → that Sub-Work is cancelled.

    A child carrying no serial was not written by a planner (a hand-made
    sub-issue); it is left alone rather than cancelled.

    Returns `(report lines, changes)`. The changes are what
    `handle_superdirector_response` mirrors onto the mission's `workrun-`
    topics, one to one.

    """
    config, plane_project, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, work_key(channel, topic))
    if not issue:
        raise MissionError(f"no Work is registered for {channel}/{topic}")
    groups = state_groups(config, project_id)
    children = sub_works(list_issues(config, project_id), str(issue["id"]), groups)

    live: dict[int, dict] = {}
    stale: list[dict] = []
    for child in children:
        serial = sub_work_serial(child.get("external_id"))
        if serial == NO_SERIAL:
            continue  # not a planner's child; not this function's business
        if serial in live:
            # Two live children on one serial can only come from an older
            # cancel+recreate generation that half survived; the newest wins.
            stale.append(live[serial])
        live[serial] = child

    state = starting_state_id(config, project_id)
    lines: list[str] = []
    changes: list[TaskChange] = []
    seen: set[int] = set()

    for number, path in task_files(plan_dir):
        seen.add(number)
        sub_title, sub_description = split_document(path.read_text(encoding="utf-8"))
        document = compose_document(sub_title, description_html(sub_description))
        existing = live.get(number)
        if existing is None:
            sub_issue, _ = ensure_issue(
                config,
                project_id,
                name=sub_title,
                description=sub_description,
                state=state,
                external_id=sub_work_key(channel, topic, number),
                parent=str(issue["id"]),
            )
            label = issue_label(plane_project, sub_issue)
            action = "created"
        else:
            label = issue_label(plane_project, existing)
            unchanged = (
                str(existing.get("name", "")) == sub_title
                and html_to_text(existing.get("description_html")) == sub_description
            )
            done = groups.get(str(existing.get("state") or "")) == "completed"
            if unchanged:
                action = "unchanged"
            else:
                update_issue(
                    config,
                    project_id,
                    str(existing["id"]),
                    {"name": sub_title, "description_html": description_html(sub_description)},
                )
                action = "changed-after-done" if done else "updated"
        lines.append(f'{action} sub-work {label} "{sub_title}"')
        changes.append(TaskChange(number, action, sub_title, document, label))

    cancelled = state_id_for_group(config, project_id, "cancelled")
    for child in [*stale, *(live[serial] for serial in sorted(set(live) - seen))]:
        update_issue(config, project_id, str(child["id"]), {"state": cancelled})
        label = issue_label(plane_project, child)
        title = str(child.get("name", ""))
        serial = sub_work_serial(child.get("external_id"))
        lines.append(f'cancelled sub-work {label} "{title}"')
        changes.append(TaskChange(serial, "cancelled", title, "", label))

    if not lines:
        lines.append("the superdirector wrote no task files; the mission has no sub-work")
    return lines, changes
