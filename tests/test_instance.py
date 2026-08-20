import pytest

from agautolab.instance import FALLBACK_NAME, instance_name


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("AUTOLAB_INSTANCE_NAME", raising=False)


def test_reads_the_name_from_the_file(tmp_path):
    path = tmp_path / "instance.toml"
    path.write_text('name = "autolab-somewhere2"\n', encoding="utf-8")
    assert instance_name(path) == "autolab-somewhere2"


def test_falls_back_to_the_plain_agent_name_without_a_file(tmp_path):
    assert instance_name(tmp_path / "absent.toml") == FALLBACK_NAME


def test_env_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "instance.toml"
    path.write_text('name = "autolab-fromfile1"\n', encoding="utf-8")
    monkeypatch.setenv("AUTOLAB_INSTANCE_NAME", "autolab-fromenv1")
    assert instance_name(path) == "autolab-fromenv1"
