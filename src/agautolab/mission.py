"""Create a Plane issue for the project attached to the current chat."""

from __future__ import annotations

import html
import os
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


class MissionError(RuntimeError):
    """The mission could not be registered."""


def current_project(cwd: Path | None = None) -> str:
    override = os.environ.get("AUTOLAB_PROJECT", "").strip()
    if override:
        if not PROJECT_NAME.fullmatch(override):
            raise MissionError("AUTOLAB_PROJECT is not a valid project name")
        return override
    root = (cwd or Path.cwd()) / ".local" / "topics"
    logs = list(root.glob("pj-*/*/*/chatlog.txt"))
    if not logs:
        raise MissionError(
            "no project chat context found; run this from the front workspace after topic_dump"
        )
    latest = max(logs, key=lambda path: (path.stat().st_mtime_ns, path.as_posix()))
    channel = latest.relative_to(root).parts[0]
    project = channel.removeprefix("pj-")
    if not PROJECT_NAME.fullmatch(project):
        raise MissionError(f"latest chat channel does not name a valid project: {channel}")
    return project


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


def state_id(config: PlaneConfig, project_id: str, name: str) -> str:
    base = (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        f"/projects/{urllib.parse.quote(project_id, safe='')}"
    )
    headers = {"X-API-Key": config.api_key, "Content-Type": "application/json"}
    status, payload = _request_json("GET", f"{base}/states/", headers=headers, timeout=60)
    if status != 200:
        raise MissionError(f"Plane state list returned HTTP {status}: {payload!r}")
    state = next(
        (row for row in _rows(payload) if str(row.get("name", "")).lower() == name.lower()),
        None,
    )
    if not state or not state.get("id"):
        raise MissionError(f"Plane project has no state named {name!r}")
    return str(state["id"])


def starting_state_id(config: PlaneConfig, project_id: str) -> str:
    """Choose the project's actionable initial state from its live vocabulary."""
    base = (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        f"/projects/{urllib.parse.quote(project_id, safe='')}"
    )
    headers = {"X-API-Key": config.api_key, "Content-Type": "application/json"}
    status, payload = _request_json("GET", f"{base}/states/", headers=headers, timeout=60)
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


def create_mission(project: str, name: str, description: str) -> dict:
    if not name.strip():
        raise MissionError("mission name must not be empty")
    if not description.strip():
        raise MissionError("mission description must not be empty")
    try:
        config = load_plane_config()
        plane_project = find_plane_project(config, project)
        starting_state = starting_state_id(config, str(plane_project["id"]))
    except ProjectInitError as error:
        raise MissionError(str(error)) from error
    base = (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        f"/projects/{urllib.parse.quote(str(plane_project['id']), safe='')}/issues/"
    )
    body = {
        "name": name.strip(),
        "description_html": f"<p>{html.escape(description.strip()).replace(chr(10), '<br>')}</p>",
        "state": starting_state,
    }
    status, payload = _request_json(
        "POST",
        base,
        headers={"X-API-Key": config.api_key, "Content-Type": "application/json"},
        body=body,
        timeout=60,
    )
    if status not in {200, 201} or not isinstance(payload, dict):
        raise MissionError(f"Plane issue create returned HTTP {status}: {payload!r}")
    return payload


def new_mission(name: str, description: str, *, cwd: Path | None = None) -> str:
    create_mission(current_project(cwd), name, description)
    return "success"
