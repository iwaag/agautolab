"""Plane mirror of one chat topic: its Work, Sub-Works, and their files.

Both directions live here. Reading, a topic's Work becomes `mission.md` and
its live Sub-Works `task1.md`, `task2.md`, … in the topic's front workspace
(`write_mission_workspace`). Writing, the front's `new_mission.md` updates or
creates the Work (`upsert_work`), obsolete Sub-Works are cancelled — never
deleted (`cancel_sub_works`) — and the coding split's `task[N].md` files are
registered as a new generation of Sub-Works (`register_task_files`).

Works are keyed on Plane's own `external_source`/`external_id` pair
(`<channel>/<topic>`), so the key survives a wiped `.local/`. Sub-Works carry
a generation marker (`<channel>/<topic>@<rev>#<N>`) because cancelled
generations keep their keys forever.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path

from .project_init import (
    PROJECT_NAME,
    PlaneConfig,
    ProjectInitError,
    _normalized_name,
    _request_json,
    _rows,
    load_plane_config,
)

EXTERNAL_SOURCE = "agautolab"
TITLE_LIMIT = 255
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
TASK_FILE = re.compile(r"^task(?P<number>\d+)\.md$")


class MissionError(RuntimeError):
    """One Plane mirror operation could not complete."""


# --- file to issue --------------------------------------------------------


def split_document(text: str) -> tuple[str, str]:
    """Split one Markdown file into a Plane issue title and description.

    Title is the first heading line; without one, the first non-empty line.
    Everything else, in file order, is the description.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if HEADING.match(line):
            title = HEADING.match(line).group("title")
            break
        if line.strip():
            title = line.strip()
            break
    else:
        raise MissionError("the file is empty")
    description = "\n".join(lines[:index] + lines[index + 1 :]).strip()
    return title[:TITLE_LIMIT], description


def description_html(description: str) -> str:
    return f"<p>{html.escape(description).replace(chr(10), '<br>')}</p>"


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


# --- Plane -----------------------------------------------------------------


def find_plane_project(config: PlaneConfig, project: str) -> dict:
    base = (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        "/projects"
    )
    headers = {"X-API-Key": config.api_key, "Content-Type": "application/json"}
    status, payload = _request_json("GET", f"{base}/?per_page=100", headers=headers)
    if status != 200:
        raise MissionError(f"Plane project list returned HTTP {status}: {payload!r}")
    wanted = _normalized_name(project)
    match = next(
        (row for row in _rows(payload) if _normalized_name(str(row.get("name", ""))) == wanted),
        None,
    )
    if not match or not match.get("id"):
        raise MissionError(f"Plane project does not exist: {project}")
    return match


def _project_base(config: PlaneConfig, project_id: str) -> str:
    return (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        f"/projects/{urllib.parse.quote(project_id, safe='')}"
    )


def _headers(config: PlaneConfig) -> dict[str, str]:
    return {"X-API-Key": config.api_key, "Content-Type": "application/json"}


def starting_state_id(config: PlaneConfig, project_id: str) -> str:
    """Choose the project's actionable initial state from its live vocabulary."""
    status, payload = _request_json(
        "GET", f"{_project_base(config, project_id)}/states/", headers=_headers(config), timeout=60
    )
    if status != 200:
        raise MissionError(f"Plane state list returned HTTP {status}: {payload!r}")
    rows = _rows(payload)
    by_name = {str(row.get("name", "")).lower(): row for row in rows}
    state = by_name.get("ready") or by_name.get("todo")
    if state is None:
        state = next((row for row in rows if row.get("group") == "unstarted"), None)
    if state is None:
        state = by_name.get("backlog")
    if not state or not state.get("id"):
        raise MissionError("Plane project has no usable starting state")
    return str(state["id"])


def find_issue_by_external(config: PlaneConfig, project_id: str, external_id: str) -> dict | None:
    """Look one issue up by the `(external_source, external_id)` pair.

    Plane answers with the issue object itself, and 404 when the pair is
    unknown. This is the duplicate guard: no local marker file is involved.
    """
    query = urllib.parse.urlencode(
        {"external_id": external_id, "external_source": EXTERNAL_SOURCE}
    )
    status, payload = _request_json(
        "GET", f"{_project_base(config, project_id)}/issues/?{query}", headers=_headers(config)
    )
    if status == 404:
        return None
    if status != 200:
        raise MissionError(f"Plane issue lookup returned HTTP {status}: {payload!r}")
    if isinstance(payload, dict) and payload.get("id"):
        return payload
    rows = _rows(payload) if isinstance(payload, (list, dict)) else []
    return rows[0] if rows else None


def ensure_issue(
    config: PlaneConfig,
    project_id: str,
    *,
    name: str,
    description: str,
    state: str,
    external_id: str,
    parent: str | None = None,
) -> tuple[dict, bool]:
    """Return `(issue, created)` for one external key, creating at most one."""
    if not name.strip():
        raise MissionError("issue title must not be empty")
    existing = find_issue_by_external(config, project_id, external_id)
    if existing:
        return existing, False
    body = {
        "name": name.strip(),
        "description_html": description_html(description),
        "state": state,
        "external_source": EXTERNAL_SOURCE,
        "external_id": external_id,
    }
    if parent:
        body["parent"] = parent
    status, payload = _request_json(
        "POST",
        f"{_project_base(config, project_id)}/issues/",
        headers=_headers(config),
        body=body,
        timeout=60,
    )
    if status in {200, 201} and isinstance(payload, dict):
        return payload, True
    if status == 409:
        # Plane reports the winner of a race in the error body; a re-read is
        # still needed to get its human-readable sequence id.
        existing = find_issue_by_external(config, project_id, external_id)
        if existing:
            return existing, False
        if isinstance(payload, dict) and payload.get("id"):
            return {"id": payload["id"]}, False
    raise MissionError(f"Plane issue create returned HTTP {status}: {payload!r}")


def list_issues(config: PlaneConfig, project_id: str) -> list[dict]:
    """Every issue in the project, following Plane's cursor pagination.

    Plane CE v1.4.1 ignores a `?parent=` filter (it returns the full list)
    and 404s the `sub-issues` endpoint, so children are found by filtering
    this list on the `parent` field client-side. List rows carry
    `description_html`, so no per-issue detail reads are needed.
    """
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        query = "per_page=100" + (f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "")
        status, payload = _request_json(
            "GET", f"{_project_base(config, project_id)}/issues/?{query}", headers=_headers(config)
        )
        if status != 200:
            raise MissionError(f"Plane issue list returned HTTP {status}: {payload!r}")
        rows.extend(_rows(payload))
        if not (isinstance(payload, dict) and payload.get("next_page_results")):
            return rows
        cursor = str(payload.get("next_cursor", ""))
        if not cursor:
            return rows


def state_groups(config: PlaneConfig, project_id: str) -> dict[str, str]:
    """State id -> state group (`backlog`/`unstarted`/`started`/`completed`/`cancelled`)."""
    status, payload = _request_json(
        "GET", f"{_project_base(config, project_id)}/states/", headers=_headers(config), timeout=60
    )
    if status != 200:
        raise MissionError(f"Plane state list returned HTTP {status}: {payload!r}")
    return {str(row["id"]): str(row.get("group", "")) for row in _rows(payload) if row.get("id")}


def html_to_text(value: str | None) -> str:
    """Recover plain text from `description_html`.

    `description_stripped` is unreliable on this Plane (observed None on
    issues whose html has content), so this inverts `description_html()` —
    and strips any other tags a hand-edited issue may carry.
    """
    text = re.sub(r"<br\s*/?>", "\n", value or "")
    text = re.sub(r"</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def compose_document(name: str, description_html_value: str | None) -> str:
    """Invert `split_document`: title as `# heading`, then the description."""
    body = html_to_text(description_html_value)
    return f"# {name}\n\n{body}\n" if body else f"# {name}\n"


def sub_works(issues: list[dict], parent_id: str, groups: dict[str, str]) -> list[dict]:
    """Non-cancelled children of one issue, in sequence order."""
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
    try:
        config = load_plane_config()
    except ProjectInitError as error:
        raise MissionError(str(error)) from error
    plane_project = find_plane_project(config, project)
    project_id = str(plane_project["id"])
    issue = find_issue_by_external(config, project_id, f"{channel}/{topic}")
    if not issue:
        return False
    issues = list_issues(config, project_id)
    issue = next(
        (row for row in issues if str(row.get("id")) == str(issue["id"])), issue
    )
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


def issue_label(plane_project: dict, issue: dict) -> str:
    identifier = str(plane_project.get("identifier", "")).strip()
    sequence = issue.get("sequence_id")
    if identifier and sequence is not None:
        return f"{identifier}-{sequence}"
    return str(issue.get("id", "?"))


# --- writing the front's decisions back to Plane ---------------------------


def _prepare(project: str) -> tuple[PlaneConfig, dict, str]:
    try:
        config = load_plane_config()
    except ProjectInitError as error:
        raise MissionError(str(error)) from error
    plane_project = find_plane_project(config, project)
    return config, plane_project, str(plane_project["id"])


def state_id_for_group(config: PlaneConfig, project_id: str, group: str) -> str:
    """The project's state id for one group (`started`, `cancelled`, …)."""
    for state_id, state_group in state_groups(config, project_id).items():
        if state_group == group:
            return state_id
    raise MissionError(f"Plane project has no state in group {group!r}")


def update_issue(config: PlaneConfig, project_id: str, issue_id: str, body: dict) -> dict:
    status, payload = _request_json(
        "PATCH",
        f"{_project_base(config, project_id)}/issues/{urllib.parse.quote(issue_id, safe='')}/",
        headers=_headers(config),
        body=body,
        timeout=60,
    )
    if status != 200 or not isinstance(payload, dict):
        raise MissionError(f"Plane issue update returned HTTP {status}: {payload!r}")
    return payload


def upsert_work(project: str, channel: str, topic: str, title: str, description: str) -> str:
    """Update the topic's Work with the new title/description, creating it if
    this is the topic's first mission. Returns one report line."""
    config, plane_project, project_id = _prepare(project)
    existing = find_issue_by_external(config, project_id, f"{channel}/{topic}")
    if existing:
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
        external_id=f"{channel}/{topic}",
    )
    return f'created {issue_label(plane_project, issue)} "{title}"'


def cancel_sub_works(project: str, channel: str, topic: str) -> int:
    """Transition every non-cancelled Sub-Work of the topic's Work to
    Cancelled. Sub-Works are never deleted. Returns how many moved."""
    config, _, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, f"{channel}/{topic}")
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
    issue = find_issue_by_external(config, project_id, f"{channel}/{topic}")
    if not issue:
        raise MissionError(f"no Work is registered for {channel}/{topic}")
    update_issue(
        config, project_id, str(issue["id"]),
        {"state": state_id_for_group(config, project_id, group)},
    )
    return issue_label(plane_project, issue)


def register_task_files(
    project: str, channel: str, topic: str, coding_dir: Path, rev: int
) -> list[str]:
    """Register every `task[N].md` in `coding_dir` as a Sub-Work of the
    topic's Work, keyed `<channel>/<topic>@<rev>#<N>`.

    The generation marker keeps the keys clear of earlier, now-cancelled
    generations, whose external ids live in Plane forever.
    """
    config, plane_project, project_id = _prepare(project)
    issue = find_issue_by_external(config, project_id, f"{channel}/{topic}")
    if not issue:
        raise MissionError(f"no Work is registered for {channel}/{topic}")
    state = starting_state_id(config, project_id)
    tasks = task_files(coding_dir)
    lines: list[str] = []
    for number, path in tasks:
        sub_title, sub_description = split_document(path.read_text(encoding="utf-8"))
        sub_issue, sub_created = ensure_issue(
            config,
            project_id,
            name=sub_title,
            description=sub_description,
            state=state,
            external_id=f"{channel}/{topic}@{rev}#{number}",
            parent=str(issue["id"]),
        )
        verb = "created sub-work" if sub_created else "already registered sub-work"
        lines.append(f'{verb} {issue_label(plane_project, sub_issue)} "{sub_title}"')
    if not tasks:
        lines.append("the coding run wrote no task files; the mission has no sub-work")
    return lines
