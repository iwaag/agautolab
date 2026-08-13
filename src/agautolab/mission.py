"""Register one dumped chat topic as a Plane task with its sub-work.

The unit of registration is a topic dump directory
(`.local/topics/<channel>/<topic>/<N>/`) written by `agag.zulip.topic_dump`:

    mission.md   -> one Plane issue
    tasks/1.md   -> sub-work of that issue
    tasks/2.md   -> sub-work of that issue

Both sides are keyed on Plane's own `external_source`/`external_id` pair, so
re-running the same topic — including a later dump version of it — adds
nothing. That key lives in Plane, not on this disk, which is what makes it
survive a wiped `.local/` and the ever-incrementing dump version number.
"""

from __future__ import annotations

import html
import os
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
TASK_FILE = re.compile(r"^(?P<number>\d+)\.md$")


class MissionError(RuntimeError):
    """The mission could not be registered."""


# --- locating the dump directory -----------------------------------------


def latest_dump_directory(cwd: Path | None = None) -> Path:
    """The newest `<N>/` version directory under `.local/topics/pj-*/`."""
    root = (cwd or Path.cwd()) / ".local" / "topics"
    logs = list(root.glob("pj-*/*/*/chatlog.txt"))
    if not logs:
        raise MissionError(
            "no project chat context found; run this from the front workspace after topic_dump"
        )
    latest = max(logs, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))
    return latest.parent


def resolve_dump_directory(directory: Path | str | None, *, cwd: Path | None = None) -> Path:
    if directory is None:
        return latest_dump_directory(cwd)
    path = Path(directory)
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    if not path.is_dir():
        raise MissionError(f"not a dump directory: {path}")
    return path


def topic_key(directory: Path) -> tuple[str, str]:
    """Return `(channel, topic)` for `…/topics/<channel>/<topic>/<N>`."""
    parts = directory.resolve().parts
    if len(parts) < 3:
        raise MissionError(f"dump directory has no channel/topic context: {directory}")
    channel, topic = parts[-3], parts[-2]
    if not channel.startswith("pj-"):
        raise MissionError(
            f"dump directory is not under a pj-* channel directory: {directory}"
        )
    return channel, topic


def current_project(directory: Path) -> str:
    """Project name from the `pj-*` segment, or the `AUTOLAB_PROJECT` override."""
    override = os.environ.get("AUTOLAB_PROJECT", "").strip()
    if override:
        if not PROJECT_NAME.fullmatch(override):
            raise MissionError("AUTOLAB_PROJECT is not a valid project name")
        return override
    channel, _ = topic_key(directory)
    project = channel.removeprefix("pj-")
    if not PROJECT_NAME.fullmatch(project):
        raise MissionError(f"chat channel does not name a valid project: {channel}")
    return project


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


def task_files(directory: Path) -> tuple[list[tuple[int, Path]], list[str]]:
    """Numbered task files in numeric order, plus the names that were skipped."""
    tasks: list[tuple[int, Path]] = []
    ignored: list[str] = []
    if not directory.is_dir():
        return tasks, ignored
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            ignored.append(path.name)
            continue
        match = TASK_FILE.fullmatch(path.name)
        if match:
            tasks.append((int(match.group("number")), path))
        else:
            ignored.append(path.name)
    tasks.sort(key=lambda item: item[0])
    return tasks, ignored


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


def issue_label(plane_project: dict, issue: dict) -> str:
    identifier = str(plane_project.get("identifier", "")).strip()
    sequence = issue.get("sequence_id")
    if identifier and sequence is not None:
        return f"{identifier}-{sequence}"
    return str(issue.get("id", "?"))


# --- the whole registration ------------------------------------------------


def register_dump(directory: Path | str | None = None, *, cwd: Path | None = None) -> str:
    """Register `mission.md` and `tasks/*.md` from one dump directory.

    Returns one report line per action. A dump with no `mission.md` is the
    normal "the chat was not a request" outcome, not an error.
    """
    dump = resolve_dump_directory(directory, cwd=cwd)
    channel, topic = topic_key(dump)
    project = current_project(dump)
    mission_file = dump / "mission.md"
    if not mission_file.is_file():
        return "no mission"

    try:
        config = load_plane_config()
    except ProjectInitError as error:
        raise MissionError(str(error)) from error
    plane_project = find_plane_project(config, project)
    project_id = str(plane_project["id"])
    state = starting_state_id(config, project_id)

    lines: list[str] = []
    title, description = split_document(mission_file.read_text(encoding="utf-8"))
    issue, created = ensure_issue(
        config,
        project_id,
        name=title,
        description=description,
        state=state,
        external_id=f"{channel}/{topic}",
    )
    label = issue_label(plane_project, issue)
    lines.append(f'{"created" if created else "already registered"} {label} "{title}"')

    tasks, ignored = task_files(dump / "tasks")
    for name in ignored:
        lines.append(f"ignored non-task file in tasks/: {name}")
    for number, path in tasks:
        sub_title, sub_description = split_document(path.read_text(encoding="utf-8"))
        sub_issue, sub_created = ensure_issue(
            config,
            project_id,
            name=sub_title,
            description=sub_description,
            state=state,
            external_id=f"{channel}/{topic}#{number}",
            parent=str(issue["id"]),
        )
        verb = "created sub-work" if sub_created else "already registered sub-work"
        lines.append(f'{verb} {issue_label(plane_project, sub_issue)} "{sub_title}"')
    if not tasks:
        lines.append("no tasks/ directory content; the mission has no sub-work")
    return "\n".join(lines)
