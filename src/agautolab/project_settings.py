"""Developer-owned agent selections for one local project workspace."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .instance import AGAUTOLAB_ROOT as PROJECT_ROOT

PROJECTS_ROOT = PROJECT_ROOT / ".local" / "projects"
PROJECT_AGENT_ROLES = frozenset({"coding", "director"})


class ProjectSettingsError(Exception):
    """A project's local agent selection is unreadable or invalid."""


def project_name_from_direction(cwd: Path) -> str | None:
    """Return the project owning a direction workspace, if ``cwd`` is in one."""
    try:
        relative = cwd.resolve().relative_to(PROJECTS_ROOT.resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) < 2 or parts[1] != "direction":
        return None
    return parts[0]


def load_project_roles(project: str | None) -> dict[str, str]:
    """Load ``.local/projects/<project>/agents.toml``.

    A missing file means that the shared role defaults apply. Present files
    fail loudly when unreadable, malformed, or when they name anything other
    than the project-selectable ``coding`` and ``director`` roles.
    """
    if project is None:
        return {}
    if not isinstance(project, str) or not project.strip():
        raise ProjectSettingsError("project must be a non-empty string")
    project = project.strip()
    if Path(project).name != project or project in {".", ".."}:
        raise ProjectSettingsError(f"invalid project name: {project!r}")

    path = PROJECTS_ROOT / project / "agents.toml"
    if not path.exists():
        return {}
    if not path.is_file():
        raise ProjectSettingsError(f"project agent settings is not a file: {path}")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectSettingsError(f"cannot read project agent settings {path}: {exc}") from exc

    roles = document.get("roles")
    if not isinstance(roles, dict):
        raise ProjectSettingsError(f"project agent settings {path}: [roles] must be a table")
    unknown = sorted(set(roles) - PROJECT_AGENT_ROLES)
    if unknown:
        raise ProjectSettingsError(
            f"project agent settings {path}: unknown role(s): {', '.join(unknown)}"
        )
    for role, profile in roles.items():
        if not isinstance(profile, str) or not profile.strip():
            raise ProjectSettingsError(
                f"project agent settings {path}: roles.{role} must be a non-empty string"
            )
    return {role: profile.strip() for role, profile in roles.items()}
