"""The front role can perform conversational project-profile edits."""

import json
from pathlib import Path

from agautolab.role_run import ROLE_ALLOWED_TOOLS


ROOT = Path(__file__).resolve().parent.parent


def test_front_harnesses_can_edit_and_write():
    allowed = set(ROLE_ALLOWED_TOOLS["front"].split(","))
    assert {"Edit", "Write"} <= allowed

    config = json.loads((ROOT / "agent" / "opencode-front.json").read_text())
    assert config["permission"]["edit"] == "allow"
    assert config["permission"]["write"] == "allow"


def test_front_guide_describes_project_profile_capability():
    guide = (ROOT / "agent" / "GUIDE.md").read_text()
    assert ".local/projects/<name>/agents.toml" in guide
    assert "`coding` and `director`" in guide
    assert "valid profile names come from the root `agents.toml`" in guide
    assert "settings change into a mission" in guide
