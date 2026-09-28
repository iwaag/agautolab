"""agent_guide p2 ex1: this agent's trial driver, dry, from the main checkout."""

from __future__ import annotations

from pathlib import Path

import pytest

from agag.agent import RECORDS_ROOT_VARIABLE

from agautolab import trial

PROBES = ["planner-other-project", "entrance-plans"]


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.is_dir() else set()


@pytest.mark.parametrize("probe", PROBES)
def test_a_dry_run_writes_its_prompt_and_nothing_in_the_checkout(probe, tmp_path, monkeypatch):
    local = trial.ROOT / ".local"
    before = {name: _files(local / name) for name in ("agent", "topics")}
    monkeypatch.setenv(RECORDS_ROOT_VARIABLE, "")  # the trial sets it for the process; restored afterwards
    trial.main([probe, "--out", str(tmp_path), "--dry-run"])
    assert (tmp_path / "prompt.md").is_file() and (tmp_path / "outcome.json").is_file()
    assert {name: _files(local / name) for name in ("agent", "topics")} == before
