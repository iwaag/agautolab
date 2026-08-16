"""Plane mirror of one chat topic: its Work, Sub-Works, and their files.

The Plane client itself is `agag.plane`, shared with agforge. What lives here
is autolab's own policy on top of it:

- the `AUTO` label, which is what makes an issue eligible for automatic
  execution (`next_work`), and the `[AUTO]` project marker that decides which
  projects are scanned at all
- Sub-Work generation keys (`<channel>/<topic>@<rev>#<N>`), because cancelled
  generations keep their keys in Plane forever
- which Work to execute next, and how a finished one is reported back

Both directions still live here. Reading, a topic's Work becomes `mission.md`
and its live Sub-Works `task1.md`, `task2.md`, … in the topic's front
workspace (`write_mission_workspace`). Writing, the front's `new_mission.md`
updates or creates the Work (`upsert_work`), obsolete Sub-Works are cancelled
— never deleted (`cancel_sub_works`) — and the coding split's `task[N].md`
files are registered as a new generation of Sub-Works (`register_task_files`).

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
# A Sub-Work that needs a media asset before it can be coded. The marker is
# the superdirector's, written at the very end of a `task[N].md`; the label is
# what `handle_run` reads back off the issue, because Plane — not the file —
# is the ledger from registration onwards.
ASSET_LABEL = "asset"
ASSET_MARKER = "[Asset]"

# agforge's side of the asset ledger. Its Works are keyed on the requesting
# `<channel>/<topic>` under its own external source, so one lookup answers
# "has this asset been ordered, and is it finished" without any local state.
#
# One topic per asset work is load-bearing: agforge keys one Plane Work per
# `<channel>/<topic>`, so reusing a topic would overwrite the ledger entry and
# mix two specs into one chatlog. The underscore keeps the name clear of
# agforge's own `create-YYYYMMDD-HHMMSS-<id>` hyphenation, and agforge's
# listener matches only the `create-` prefix — the rest is free.
AGFORGE_SOURCE = "agforge"
ASSET_TOPIC_PREFIX = "create-asset_"

TASK_FILE = re.compile(r"^task(?P<number>\d+)\.md$")
SUB_WORK_SERIAL = re.compile(r"#(?P<number>\d+)\s*$")

_LABEL_CACHE: dict[tuple[str, str], str] = {}

__all__ = [
    "AGFORGE_SOURCE",
    "ASSET_LABEL",
    "ASSET_MARKER",
    "ASSET_TOPIC_PREFIX",
    "AUTO_LABEL",
    "EXTERNAL_SOURCE",
    "TITLE_LIMIT",
    "MissionError",
    "PlaneConfig",
    "Work",
    "add_comment",
    "asset_answer_context",
    "asset_order",
    "asset_order_key",
    "asset_topic",
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
    "register_task_files",
    "report_work",
    "split_document",
    "starting_state_id",
    "state_groups",
    "state_id_for_group",
    "strip_asset_marker",
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


def sub_work_key(channel: str, topic: str, rev: int, number: int) -> str:
    """The generation marker keeps new keys clear of cancelled generations',
    whose external ids live in Plane forever."""
    return f"{channel}/{topic}@{rev}#{number}"


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
    not ids: creating them on first use is `ensure_label`'s job, and the
    caller that knows a Sub-Work is an asset should not have to know how a
    Plane label comes into being.
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
    the copied `new_mission.md` included — is simply not a task."""
    tasks: list[tuple[int, Path]] = []
    if not directory.is_dir():
        return tasks
    for path in directory.iterdir():
        match = TASK_FILE.fullmatch(path.name)
        if match and path.is_file():
            tasks.append((int(match.group("number")), path))
    tasks.sort(key=lambda item: item[0])
    return tasks


def strip_asset_marker(text: str) -> tuple[str, bool]:
    """`(text without a trailing `[Asset]` marker, whether it carried one)`.

    The marker is a signal to this registration, not part of the task the
    coding run will read, so it never reaches Plane. Matched
    case-insensitively and only at the very end of the file, which is where
    the superdirector guide puts it.
    """
    body = text.rstrip()
    if not body.upper().endswith(ASSET_MARKER.upper()):
        return text, False
    return body[: -len(ASSET_MARKER)].rstrip() + "\n", True


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
    return int(match.group("number")) if match else 1 << 30


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
    """One chosen Work, with everything a `run-` serving needs of it.

    `is_asset` is read off the issue's labels, not off any file: the
    `task[N].md` that carried the `[Asset]` marker was deleted the moment
    Plane accepted it (Step 2), so the label is the only surviving record.
    """

    slug: str
    name: str
    description: str
    project_id: str
    issue_id: str
    is_asset: bool = False


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
        asset_id = labels.get(ASSET_LABEL.lower())
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
                    asset_id,
                    issue,
                )
            )
    if not candidates:
        return None
    _, slug, project_id, asset_id, issue = min(candidates, key=lambda item: item[0])
    return Work(
        slug,
        str(issue.get("name", "")),
        html_to_text(issue.get("description_html")),
        project_id,
        str(issue["id"]),
        bool(asset_id) and asset_id in {str(value) for value in (issue.get("labels") or [])},
    )


# --- the asset ledger, which is agforge's Work -----------------------------


def asset_topic(issue_id: str) -> str:
    """The `create-` topic one asset Work is ordered and answered in."""
    return f"{ASSET_TOPIC_PREFIX}{issue_id}"


def asset_order_key(channel: str, issue_id: str) -> str:
    """agforge keys its Work on the requesting `<channel>/<topic>`."""
    return f"{channel}/{asset_topic(issue_id)}"


def asset_order(project_id: str, channel: str, issue_id: str) -> tuple[str, dict | None]:
    """Where one asset order stands, as `(state, agforge issue)`.

    `"absent"` — never ordered; `"working"` — ordered, not finished;
    `"done"` — agforge moved its Work to a `completed` state. There is no
    local marker file anywhere in this: Plane is the ledger, so a wiped
    `.local/` cannot make autolab order the same asset twice.
    """
    config = load_plane_config()
    key = asset_order_key(channel, issue_id)
    issue = shared_find_issue_by_external(config, project_id, AGFORGE_SOURCE, key)
    if not issue:
        return "absent", None
    # The external lookup may answer with a thin object; the list row wins,
    # the same rule `write_mission_workspace` follows.
    issues = list_issues(config, project_id)
    issue = next((row for row in issues if str(row.get("id")) == str(issue["id"])), issue)
    groups = state_groups(config, project_id)
    state = groups.get(str(issue.get("state") or ""))
    return ("done" if state == "completed" else "working"), issue


def asset_answer_context(project: str, work_id: str) -> tuple[str, str]:
    """`(plan.md text, task.md text)` for one asset Sub-Work, read from Plane.

    Plane is the only source. Step 2 deletes `plan.md` and `task[N].md` from
    the project folder as soon as Plane accepts them, precisely so that there
    is one canonical copy and it is this one.

    The parent Work's description *is* `plan.md` — the whole file, heading
    included — so it is recovered verbatim rather than recomposed. The
    Sub-Work is recomposed into a document, which gives back the `task[N].md`
    the superdirector wrote minus the `[Asset]` marker that was addressed to
    the registration.
    """
    config, _, project_id = _prepare(project)
    issues = list_issues(config, project_id)
    task = next((row for row in issues if str(row.get("id")) == str(work_id)), None)
    if not task:
        raise MissionError(f"no Work {work_id!r} in project {project!r}")
    parent_id = str(task.get("parent") or "")
    parent = next((row for row in issues if str(row.get("id")) == parent_id), None)
    if not parent:
        raise MissionError(f"Work {work_id!r} has no parent to read the plan from")
    return (
        html_to_text(parent.get("description_html")),
        compose_document(str(task.get("name", "")), task.get("description_html")),
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


# --- writing the front's decisions back to Plane ---------------------------


def _prepare(project: str) -> tuple[PlaneConfig, dict, str]:
    config = load_plane_config()
    plane_project = find_plane_project(config, project)
    return config, plane_project, str(plane_project["id"])


def upsert_work(project: str, channel: str, topic: str, title: str, description: str) -> str:
    """Update the topic's Work with the new title/description, creating it if
    this is the topic's first mission. Returns one report line."""
    config, plane_project, project_id = _prepare(project)
    key = work_key(channel, topic)
    if existing := find_issue_by_external(config, project_id, key):
        update_issue(
            config,
            project_id,
            str(existing["id"]),
            {"name": title.strip(), "description_html": description_html(description)},
        )
        return f'updated {issue_label(plane_project, existing)} "{title}"'
    issue, _ = ensure_issue(
        config,
        project_id,
        name=title,
        description=description,
        state=starting_state_id(config, project_id),
        external_id=key,
    )
    return f'created {issue_label(plane_project, issue)} "{title}"'


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


def register_task_files(
    project: str, channel: str, topic: str, plan_dir: Path, rev: int
) -> list[str]:
    """Register every `task[N].md` in `plan_dir` as a Sub-Work of the topic's
    Work, keyed `<channel>/<topic>@<rev>#<N>`.

    A task file ending in `[Asset]` needs a media asset before it can be
    coded. The marker is stripped from the description and becomes the
    `asset` label on the issue, which is what `handle_run` dispatches on.
    """
    config, plane_project, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, work_key(channel, topic))
    if not issue:
        raise MissionError(f"no Work is registered for {channel}/{topic}")
    state = starting_state_id(config, project_id)
    tasks = task_files(plan_dir)
    lines: list[str] = []
    for number, path in tasks:
        text, is_asset = strip_asset_marker(path.read_text(encoding="utf-8"))
        sub_title, sub_description = split_document(text)
        sub_issue, sub_created = ensure_issue(
            config,
            project_id,
            name=sub_title,
            description=sub_description,
            state=state,
            external_id=sub_work_key(channel, topic, rev, number),
            parent=str(issue["id"]),
            labels=[ASSET_LABEL] if is_asset else None,
        )
        verb = "created sub-work" if sub_created else "already registered sub-work"
        suffix = " [asset]" if is_asset else ""
        lines.append(f'{verb} {issue_label(plane_project, sub_issue)} "{sub_title}"{suffix}')
    if not tasks:
        lines.append("the superdirector wrote no task files; the mission has no sub-work")
    return lines
