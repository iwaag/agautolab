"""Idempotent Plane/Gitea/local-workspace project initialization."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .agent_settings import PROJECT_ROOT

PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")
PLANE_ENV = PROJECT_ROOT.parent / ".local" / "plane-credentials.env"
GITEA_TOKEN = PROJECT_ROOT / ".local" / "gitea" / "autolab-agent.token"
GITEA_ASKPASS = PROJECT_ROOT / ".local" / "gitea" / "askpass.sh"
PROJECTS_ROOT = PROJECT_ROOT / ".local" / "projects"


class ProjectInitError(RuntimeError):
    """One project initialization step failed."""


@dataclass(frozen=True)
class PlaneConfig:
    url: str
    api_key: str
    workspace: str


@dataclass(frozen=True)
class GiteaConfig:
    url: str
    token: str
    org: str


def read_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ProjectInitError(f"cannot read configuration {path}: {error}") from error
    values: dict[str, str] = {}
    for line in lines:
        tokens = shlex.split(line, comments=True)
        if len(tokens) == 1 and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            values[key] = value
    return values


def load_plane_config(path: Path = PLANE_ENV) -> PlaneConfig:
    values = read_env(path)
    required = ("PLANE_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ProjectInitError(f"{path} is missing {', '.join(missing)}")
    return PlaneConfig(
        os.environ.get("PLANE_URL", values["PLANE_URL"]).rstrip("/"),
        os.environ.get("PLANE_API_KEY", values["PLANE_API_KEY"]),
        os.environ.get("PLANE_WORKSPACE_SLUG", values["PLANE_WORKSPACE_SLUG"]),
    )


def load_gitea_config(token_path: Path = GITEA_TOKEN) -> GiteaConfig:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ProjectInitError(f"cannot read Gitea token {token_path}: {error}") from error
    if not token:
        raise ProjectInitError(f"Gitea token is empty: {token_path}")
    return GiteaConfig(
        os.environ.get("GITEA_URL", "http://agstudio.local:3000").rstrip("/"),
        token,
        os.environ.get("GITEA_ORG", "autodev"),
    )


def plane_identifier(project: str) -> str:
    parts = [part for part in re.split(r"[^a-z0-9]+", project.lower()) if part]
    return "".join(part if part.isdigit() else part[0] for part in parts).upper()[:12]


def plane_project_name(project: str) -> str:
    """Plane rejects hyphens in names; keep its display form deterministic."""
    return " ".join(part.capitalize() for part in project.split("-"))


def _normalized_name(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", value.lower()))


def _rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [row for row in payload["results"] if isinstance(row, dict)]
    raise ProjectInitError("API response did not contain a result list")


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict | None = None,
    timeout: float = 30,
) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", "replace")[:200]}
        return error.code, payload
    except (OSError, TimeoutError) as error:
        raise ProjectInitError(f"{method} {url} failed: {error}") from error


def ensure_plane_project(config: PlaneConfig, project: str) -> dict:
    base = (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        "/projects"
    )
    headers = {"X-API-Key": config.api_key, "Content-Type": "application/json"}

    def list_projects() -> list[dict]:
        status, payload = _request_json("GET", f"{base}/?per_page=100", headers=headers)
        if status != 200:
            raise ProjectInitError(f"Plane project list returned HTTP {status}: {payload!r}")
        return _rows(payload)

    rows = list_projects()
    wanted = _normalized_name(project)
    if existing := next(
        (row for row in rows if _normalized_name(str(row.get("name", ""))) == wanted), None
    ):
        return existing

    used = {str(row.get("identifier", "")).upper() for row in rows}
    base_identifier = plane_identifier(project)
    for suffix in range(1, 101):
        tail = "" if suffix == 1 else str(suffix)
        identifier = f"{base_identifier[:12 - len(tail)]}{tail}"
        if identifier in used:
            continue
        status, payload = _request_json(
            "POST",
            f"{base}/",
            headers=headers,
            body={
                "name": plane_project_name(project),
                "identifier": identifier,
                "description": "",
            },
            timeout=60,
        )
        if status in {200, 201} and isinstance(payload, dict):
            return payload
        if status in {409, 422}:
            rows = list_projects()
            if existing := next(
                (row for row in rows if _normalized_name(str(row.get("name", ""))) == wanted),
                None,
            ):
                return existing
            used.update(str(row.get("identifier", "")).upper() for row in rows)
            continue
        raise ProjectInitError(f"Plane project create returned HTTP {status}: {payload!r}")
    raise ProjectInitError("Plane identifier collision retries exhausted")


def _gitea_headers(config: GiteaConfig) -> dict[str, str]:
    return {"Authorization": f"token {config.token}", "Content-Type": "application/json"}


def ensure_gitea_repo(config: GiteaConfig, name: str) -> dict:
    quoted_org = urllib.parse.quote(config.org, safe="")
    quoted_name = urllib.parse.quote(name, safe="")
    repo_url = f"{config.url}/api/v1/repos/{quoted_org}/{quoted_name}"
    status, payload = _request_json("GET", repo_url, headers=_gitea_headers(config))
    if status == 200 and isinstance(payload, dict):
        return payload
    if status != 404:
        raise ProjectInitError(f"Gitea repository lookup returned HTTP {status}: {payload!r}")
    status, payload = _request_json(
        "POST",
        f"{config.url}/api/v1/orgs/{quoted_org}/repos",
        headers=_gitea_headers(config),
        body={"name": name, "private": False, "default_branch": "main"},
    )
    if status in {200, 201} and isinstance(payload, dict):
        return payload
    if status in {409, 422}:
        status, payload = _request_json("GET", repo_url, headers=_gitea_headers(config))
        if status == 200 and isinstance(payload, dict):
            return payload
    raise ProjectInitError(f"Gitea repository create returned HTTP {status}: {payload!r}")


def ensure_clone(config: GiteaConfig, repo: str, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    clone_url = f"{config.url}/{urllib.parse.quote(config.org, safe='')}/{urllib.parse.quote(repo, safe='')}.git"
    environment = {
        **os.environ,
        "GIT_ASKPASS": str(GITEA_ASKPASS),
        "GIT_TERMINAL_PROMPT": "0",
        "AUTOLAB_GITEA_TOKEN_VALUE": config.token,
    }
    try:
        subprocess.run(
            ["git", "clone", clone_url, str(destination)],
            check=True,
            env=environment,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProjectInitError(f"git clone failed for {config.org}/{repo}: {error}") from error


def init_project(project: str) -> str:
    if not PROJECT_NAME.fullmatch(project):
        raise ProjectInitError(
            "project name must be 2-39 lowercase letters, digits, or hyphens "
            "and start with a letter or digit"
        )
    plane = load_plane_config()
    gitea = load_gitea_config()
    ensure_plane_project(plane, project)
    ensure_gitea_repo(gitea, project)
    ensure_gitea_repo(gitea, f"{project}-direction")
    project_root = PROJECTS_ROOT / project
    ensure_clone(gitea, project, project_root / "main")
    ensure_clone(gitea, f"{project}-direction", project_root / "direction")
    return "success"
