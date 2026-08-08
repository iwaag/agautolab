"""Tests for the gateway's run-scoped /status helpers."""

import importlib.util
import json
import os
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gateway", Path(__file__).resolve().parent.parent / "agent" / "gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def point_state_at(monkeypatch, tmp_path: Path) -> Path:
    state = tmp_path / "agent"
    (state / "sessions").mkdir(parents=True)
    (state / "serve").mkdir()
    (state / "gateway").mkdir()
    monkeypatch.setattr(gateway, "STATE", state)
    monkeypatch.setattr(gateway, "SERVE", state / "serve")
    monkeypatch.setattr(gateway, "GATEWAY", state / "gateway")
    return state


def write_session(state: Path, n: int, mtime: float) -> None:
    p = state / "sessions" / f"session-{n:04d}.json"
    p.write_text(json.dumps({"is_error": False, "num_turns": n}))
    os.utime(p, (mtime, mtime))


def test_sessions_scoped_to_run_start(monkeypatch, tmp_path):
    state = point_state_at(monkeypatch, tmp_path)
    now = time.time()
    write_session(state, 1, now - 3600)  # previous mission
    write_session(state, 2, now - 10)  # current mission

    scoped = gateway.session_summaries(since=now - 60)
    assert [r["file"] for r in scoped] == ["session-0002.json"]
    assert gateway.sessions_on_disk() == 2

    # No run start recorded (pre-scoping current file): report everything.
    assert len(gateway.session_summaries(since=None)) == 2


def test_game_info_distinguishes_stale_build(monkeypatch, tmp_path):
    state = point_state_at(monkeypatch, tmp_path)
    now = time.time()

    assert gateway.game_info(started=now)["installed"] is False

    index = state / "serve" / "index.html"
    index.write_text("<html></html>")
    os.utime(index, (now - 3600, now - 3600))  # previous mission's build

    stale = gateway.game_info(started=now - 60)
    assert stale["installed"] is True
    assert stale["installed_this_run"] is False

    os.utime(index, (now, now))  # this run just installed it
    fresh = gateway.game_info(started=now - 60)
    assert fresh["installed_this_run"] is True

    # Unknown run start: installed, but never claimed as this run's.
    assert gateway.game_info(started=None)["installed_this_run"] is False
