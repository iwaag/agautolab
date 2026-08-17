"""Idempotent archival of a project's Plane, Gitea, Zulip and local surfaces.

The inverse of `project_init`, one surface at a time and in the reverse order:
`init_project` creates a Plane project and three Gitea repositories, and a
project channel `pj-<slug>` is created beside them. A verification project that
has served its purpose keeps costing attention in four listings, so archiving
it means retiring all four.

Nothing here deletes. Plane keeps the issues, Gitea keeps the repository
read-only, Zulip keeps the messages of an archived channel, and the local
workspace is moved aside rather than removed — every step is reversible by
hand, which is what makes running this on a still-wanted project a nuisance
rather than a loss.

Every step reports its own outcome, so a partially archived project can be run
through again and the report says which surfaces were already done.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from agag.zulip import ZulipClient, ZulipError

from .project_init import (
    GiteaConfig,
    PROJECT_NAME,
    PROJECTS_ROOT,
    PlaneConfig,
    ProjectInitError,
    _gitea_headers,
    _normalized_name,
    _request_json,
    _rows,
    load_gitea_config,
    load_plane_config,
)

AGAUTOLAB_ROOT = Path(__file__).resolve().parents[2]
ZULIP_ENV = AGAUTOLAB_ROOT / ".local" / "zulip.env"
ARCHIVE_ROOT = PROJECTS_ROOT.parent / "projects-archived"

PROJECT_CHANNEL_PREFIX = "pj-"
REPO_SUFFIXES = ("", "-direction", "-devlog")

# The four outcomes a step reports. `absent` and `already` are both success:
# the difference is whether the surface was ever created for this project.
ARCHIVED = "archived"
ALREADY = "already-archived"
ABSENT = "absent"

__all__ = [
    "ABSENT",
    "ALREADY",
    "ARCHIVED",
    "ARCHIVE_ROOT",
    "ProjectArchiveError",
    "archive_gitea_repo",
    "archive_plane_project",
    "archive_project",
    "archive_workspace",
    "archive_zulip_channel",
    "main",
    "project_channel",
]


class ProjectArchiveError(RuntimeError):
    """One project archival step failed."""


@dataclass(frozen=True)
class ArchiveReport:
    project: str
    plane: str
    gitea: dict[str, str]
    zulip: str
    workspace: str

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "plane": self.plane,
            "gitea": self.gitea,
            "zulip": self.zulip,
            "workspace": self.workspace,
        }


def project_channel(project: str) -> str:
    return f"{PROJECT_CHANNEL_PREFIX}{project}"


def archive_plane_project(config: PlaneConfig, project: str) -> str:
    """Archive the project's Plane project.

    An archived project stays in the workspace listing with `archived_at`
    set, so the already-archived case is recognized rather than repeated —
    though Plane's archive endpoint is itself idempotent.
    """
    base = (
        f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"
        "/projects"
    )
    headers = {"X-API-Key": config.api_key, "Content-Type": "application/json"}
    status, payload = _request_json("GET", f"{base}/?per_page=100", headers=headers)
    if status != 200:
        raise ProjectArchiveError(f"Plane project list returned HTTP {status}: {payload!r}")
    wanted = _normalized_name(project)
    row = next(
        (r for r in _rows(payload) if _normalized_name(str(r.get("name", ""))) == wanted), None
    )
    if row is None:
        return ABSENT
    if row.get("archived_at"):
        return ALREADY
    status, payload = _request_json(
        "POST", f"{base}/{row['id']}/archive/", headers=headers, timeout=60
    )
    if status not in {200, 201, 204}:
        raise ProjectArchiveError(f"Plane project archive returned HTTP {status}: {payload!r}")
    return ARCHIVED


def archive_gitea_repo(config: GiteaConfig, name: str) -> str:
    """Make one Gitea repository read-only.

    An archived repository stays visible and clonable; pushes and new issues
    are refused. That is the whole point — a finished project's history is
    still the record of how it went.
    """
    repo_url = (
        f"{config.url}/api/v1/repos/{urllib.parse.quote(config.org, safe='')}"
        f"/{urllib.parse.quote(name, safe='')}"
    )
    status, payload = _request_json("GET", repo_url, headers=_gitea_headers(config))
    if status == 404:
        return ABSENT
    if status != 200 or not isinstance(payload, dict):
        raise ProjectArchiveError(f"Gitea repository lookup returned HTTP {status}: {payload!r}")
    if payload.get("archived"):
        return ALREADY
    status, payload = _request_json(
        "PATCH", repo_url, headers=_gitea_headers(config), body={"archived": True}
    )
    if status not in {200, 201}:
        raise ProjectArchiveError(f"Gitea repository archive returned HTTP {status}: {payload!r}")
    return ARCHIVED


def archive_zulip_channel(client: ZulipClient, project: str) -> str:
    """Archive the project channel `pj-<slug>`.

    Archiving a channel needs the right to administer it. Zulip grants that to
    the channel's creator and to organization administrators, so a bot can
    retire the channels it opened itself but not the ones a human or another
    bot opened — that case fails loudly rather than silently leaving the
    channel behind.
    """
    name = project_channel(project)
    channel = next((c for c in client.channels() if c.get("name") == name), None)
    if channel is None:
        # Archived channels leave the listing, so this covers both "never
        # existed" and "someone already archived it".
        return ABSENT
    try:
        client.archive_channel(int(channel["stream_id"]))
    except ZulipError as error:
        raise ProjectArchiveError(f"Zulip channel archive failed for {name}: {error}") from error
    return ARCHIVED


def archive_workspace(project: str, *, root: Path | None = None, archive: Path | None = None) -> str:
    """Move the local clone set aside, keeping it on disk.

    The clones are disposable — every one of them can be recreated by
    `init_project` — but they are also the only copy of anything a run left
    uncommitted, so this moves rather than removes.
    """
    source = (root or PROJECTS_ROOT) / project
    destination = (archive or ARCHIVE_ROOT) / project
    if not source.exists():
        return ALREADY if destination.exists() else ABSENT
    if destination.exists():
        raise ProjectArchiveError(
            f"both {source} and {destination} exist; resolve by hand before archiving"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return ARCHIVED


def archive_project(project: str, *, zulip_env: Path | None = None) -> dict:
    if not PROJECT_NAME.fullmatch(project):
        raise ProjectArchiveError(
            "project name must be 2-39 lowercase letters, digits, or hyphens "
            "and start with a letter or digit"
        )
    plane = load_plane_config()
    gitea = load_gitea_config()
    client = ZulipClient.from_env(zulip_env or ZULIP_ENV)
    return ArchiveReport(
        project=project,
        plane=archive_plane_project(plane, project),
        gitea={
            f"{project}{suffix}": archive_gitea_repo(gitea, f"{project}{suffix}")
            for suffix in REPO_SUFFIXES
        },
        zulip=archive_zulip_channel(client, project),
        workspace=archive_workspace(project),
    ).as_dict()


def main(argv: list[str] | None = None) -> int:
    """Archive every named project, reporting one JSON object per project.

    Credentials come from the same files `init_project` uses, except Zulip's:
    `AUTOLAB_ZULIP_ENV` overrides the node's own bot credentials, which is how
    a channel this bot did not create gets archived by a principal that may.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: python -m agautolab.project_archive <project-slug> [...]", file=sys.stderr)
        return 2
    override = os.environ.get("AUTOLAB_ZULIP_ENV")
    zulip_env = Path(override) if override else None
    failed = 0
    for project in arguments:
        try:
            report = archive_project(project, zulip_env=zulip_env)
        except (ProjectArchiveError, ProjectInitError, ZulipError) as error:
            failed += 1
            report = {"project": project, "error": str(error)}
        print(json.dumps(report, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
