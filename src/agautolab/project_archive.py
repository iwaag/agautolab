"""Idempotent archival of a project's Gitea, Zulip and local surfaces.

The inverse of `project_init`, one surface at a time and in the reverse order:
`init_project` creates three Gitea repositories, and a project channel
`pj-<slug>` is created beside them. A verification project that has served its
purpose keeps costing attention in every listing, so archiving it means
retiring all of them.

Since `refactor` p1 there is no Plane project to retire: a mission's record is
the Zulip conversation it happened in, and archiving the channel is what
retires it. Archiving a project that predates the change leaves its old Plane
project alone — the boundary this phase draws is that autolab's own path no
longer touches Plane, not that Plane's contents are migrated.

Nothing here deletes. Gitea keeps the repository read-only, Zulip keeps the
messages of an archived channel, and the local workspace is moved aside rather
than removed — every step is reversible by hand, which is what makes running
this on a still-wanted project a nuisance rather than a loss.

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

from .instance import SPEC
from .project_init import (
    GiteaConfig,
    PROJECT_NAME,
    PROJECTS_ROOT,
    ProjectInitError,
    _gitea_headers,
    _request_json,
    load_gitea_config,
)

ZULIP_ENV = SPEC.zulip_env
ARCHIVE_ROOT = PROJECTS_ROOT.parent / "projects-archived"

PROJECT_CHANNEL_PREFIX = "pj-"
REPO_SUFFIXES = ("", "-direction", "-devlog")

# The four outcomes a step reports. `absent` and `already` are both success:
# the difference is whether the surface was ever created for this project.
ARCHIVED = "archived"
ALREADY = "already-archived"
ABSENT = "absent"
KEPT = "kept"

__all__ = [
    "ABSENT",
    "ALREADY",
    "ARCHIVED",
    "ARCHIVE_ROOT",
    "KEPT",
    "ProjectArchiveError",
    "archive_gitea_repo",
    "archive_project",
    "archive_workspace",
    "archive_zulip_channel",
    "archive_zulip_folder",
    "main",
    "project_channel",
]


class ProjectArchiveError(RuntimeError):
    """One project archival step failed."""


@dataclass(frozen=True)
class ArchiveReport:
    project: str
    gitea: dict[str, str]
    zulip: str
    zulip_folder: str
    workspace: str

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "gitea": self.gitea,
            "zulip": self.zulip,
            "zulip_folder": self.zulip_folder,
            "workspace": self.workspace,
        }


def project_channel(project: str) -> str:
    return f"{PROJECT_CHANNEL_PREFIX}{project}"


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


def archive_zulip_folder(client: ZulipClient, project: str) -> str:
    """Archive the project's channel folder, `pj-<slug>` like the channel.

    The listener files every project channel in a folder of its own name
    (`zulip_listener.ensure_project_folder`), so a retired project would
    otherwise leave an empty folder in every channel picker. Zulip refuses to
    archive a folder that still holds a live channel, and `work-` channels
    are retired one Work at a time, so a folder that is not yet empty is
    reported as `kept` rather than failed — running this again once the
    last work channel is gone finishes the job.

    **An archived channel keeps its folder** (`refactor` p3 ex1). It is not
    in `client.channels()`, so a folder holding nothing but retired channels
    looks empty here and Zulip still answers 400 — which is how six orphaned
    folders accumulated, one per archived project, without anybody seeing an
    error. So every channel filed in the folder is taken out of it first,
    archived ones included, and only then is the folder archived.
    """
    name = project_channel(project)
    folder = client.channel_folder_by_name(name)
    if folder is None:
        return ABSENT
    folder_id = int(folder["id"])
    filed = [row for row in client.channels(include_archived=True)
             if row.get("folder_id") == folder_id]
    if any(not row.get("is_archived") for row in filed):
        return KEPT
    try:
        for row in filed:
            client.clear_channel_folder(int(row["stream_id"]))
        client.archive_channel_folder(folder_id)
    except ZulipError as error:
        raise ProjectArchiveError(f"Zulip folder archive failed for {name}: {error}") from error
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
    gitea = load_gitea_config()
    client = ZulipClient.from_env(zulip_env or ZULIP_ENV)
    return ArchiveReport(
        project=project,
        gitea={
            f"{project}{suffix}": archive_gitea_repo(gitea, f"{project}{suffix}")
            for suffix in REPO_SUFFIXES
        },
        zulip=archive_zulip_channel(client, project),
        zulip_folder=archive_zulip_folder(client, project),
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
