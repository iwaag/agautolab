"""agautolab paths and convenience wrapper around the shared config contract."""

from pathlib import Path

from agag.agent_config import ResolvedAgent, load_config, resolve_role

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENTS_CONFIG = PROJECT_ROOT / "agents.toml"
AGENTS_LOCAL_CONFIG = PROJECT_ROOT / ".local" / "agents.local.toml"


def resolve_project_role(
    role: str,
    *,
    profile_override: str | None = None,
    check_available: bool = True,
) -> ResolvedAgent:
    config, overlay = load_config(AGENTS_CONFIG, AGENTS_LOCAL_CONFIG)
    return resolve_role(
        config,
        overlay,
        role,
        profile_override=profile_override,
        check_available=check_available,
    )
