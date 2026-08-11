"""OpenCode-specific adapter boundary tests."""

import pytest
from agag.harness import HarnessResult

from agautolab import adapters
from agautolab.adapters import opencode
from agautolab.agent_settings import resolve_project_role


def local_agent():
    return resolve_project_role("coding", profile_override="local", check_available=False)


def test_run_pins_workdir_with_native_dir_flag(tmp_path, monkeypatch):
    seen = {}

    def capture(agent, prompt, **kwargs):
        seen.update(kwargs)
        return HarnessResult("done", 0, {"outcome": "done"})

    monkeypatch.setattr(opencode, "run_harness", capture)
    adapter = opencode.OpenCodeAdapter.from_config(
        {"args": ["--variant", "high"]}, agent=local_agent()
    )

    adapter.run("prompt", tmp_path, 5)

    assert seen["cwd"] == tmp_path
    assert seen["extra_args"] == ["--variant", "high", "--dir", str(tmp_path.resolve())]


@pytest.mark.parametrize("argument", ["--dir", "--dir=/tmp/elsewhere"])
def test_job_config_cannot_override_managed_workdir(argument):
    with pytest.raises(adapters.AdapterError, match="working directory is managed"):
        opencode.OpenCodeAdapter.from_config({"args": [argument]}, agent=local_agent())
