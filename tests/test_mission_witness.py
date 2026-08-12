"""The mission witness proves consumption from session evidence alone."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agautolab.mission_witness import (
    CLAUDE_PROJECTS_ENV,
    FAILURE_TEXT,
    UNCONSUMED_EXIT,
    _cwd_slug,
    annotate_record,
    markers,
    witness,
)

MISSION = """Build the whack-a-mole browser game described below.

- moles pop up at random in a 3x3 grid
- clicking one scores a point
"""


@pytest.fixture
def mission(tmp_path):
    path = tmp_path / "MISSION.md"
    path.write_text(MISSION)
    return path


def opencode_event(tool_output):
    return json.dumps(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": ".local/agent/MISSION.md"},
                    "output": tool_output,
                },
            },
        }
    )


def test_markers_skip_short_lines():
    marks = markers(MISSION)
    assert "- moles pop up at random in a 3x3 grid" in marks
    assert all(len(m) >= 16 for m in marks)


def test_opencode_transcript_with_mission_content_is_consumed(tmp_path, mission):
    transcript = tmp_path / "t.agent.jsonl"
    transcript.write_text(
        opencode_event(MISSION) + "\n" + json.dumps({"type": "step_finish", "part": {}}) + "\n"
    )
    verdict = witness(mission, transcript, tmp_path)
    assert verdict == {"consumed": True, "source": "transcript"}


def test_opencode_generic_reply_is_not_consumed(tmp_path, mission):
    transcript = tmp_path / "t.agent.jsonl"
    transcript.write_text(
        json.dumps({"type": "text", "part": {"text": "Ready! What should I build today?"}})
        + "\n"
    )
    verdict = witness(mission, transcript, tmp_path)
    assert verdict == {"consumed": False, "source": "transcript"}


def test_plaintext_transcript_matches_raw_lines(tmp_path, mission):
    transcript = tmp_path / "t.agent.jsonl"
    transcript.write_text("log: read mission\n- moles pop up at   random in a 3x3 grid\n")
    assert witness(mission, transcript, tmp_path)["consumed"] is True


def test_claude_result_follows_harness_session(tmp_path, monkeypatch, mission):
    projects = tmp_path / "claude-projects"
    monkeypatch.setenv(CLAUDE_PROJECTS_ENV, str(projects))
    cwd = tmp_path / "node"
    cwd.mkdir()
    session_dir = projects / _cwd_slug(cwd)
    session_dir.mkdir(parents=True)
    (session_dir / "abc-123.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": MISSION}]},
            }
        )
        + "\n"
    )
    transcript = tmp_path / "t.agent.jsonl"
    transcript.write_text(json.dumps({"session_id": "abc-123", "result": "done."}))
    verdict = witness(mission, transcript, cwd)
    assert verdict == {"consumed": True, "source": "harness_session"}


def test_claude_result_without_session_log_is_indeterminate(tmp_path, monkeypatch, mission):
    monkeypatch.setenv(CLAUDE_PROJECTS_ENV, str(tmp_path / "empty"))
    transcript = tmp_path / "t.agent.jsonl"
    transcript.write_text(json.dumps({"session_id": "gone", "result": "done."}))
    verdict = witness(mission, transcript, tmp_path)
    assert verdict["consumed"] is None
    assert verdict["source"] == "unavailable"


def test_missing_mission_or_transcript_is_indeterminate(tmp_path, mission):
    assert witness(tmp_path / "no-mission", tmp_path / "no-t", tmp_path)["consumed"] is None
    assert witness(mission, tmp_path / "no-t", tmp_path)["consumed"] is None


def test_annotate_record_marks_unconsumed_as_failed(tmp_path):
    record = tmp_path / "run.json"
    record.write_text(json.dumps({"schema": "ag.agent-run.v1", "outcome": "done"}))
    annotate_record(record, {"consumed": False, "source": "transcript"})
    doc = json.loads(record.read_text())
    assert doc["mission_consumed"] is False
    assert doc["outcome"] == "failed"
    assert doc["failure"] == FAILURE_TEXT

    annotate_record(record, {"consumed": True, "source": "transcript"})
    doc = json.loads(record.read_text())
    assert doc["mission_consumed"] is True
    # A witnessed pass does not resurrect the previous failure text.
    assert doc["outcome"] == "failed"


def test_cli_exit_codes_and_record(tmp_path, mission):
    transcript = tmp_path / "t.agent.jsonl"
    transcript.write_text(json.dumps({"type": "text", "part": {"text": "generic"}}) + "\n")
    record = tmp_path / "run.json"
    record.write_text(json.dumps({"outcome": "done"}))
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable, "-m", "agautolab.mission_witness",
            str(mission), str(transcript), "--cwd", str(tmp_path),
            "--record", str(record),
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == UNCONSUMED_EXIT
    assert json.loads(record.read_text())["mission_consumed"] is False

    transcript.write_text(opencode_event(MISSION) + "\n")
    proc = subprocess.run(
        [
            sys.executable, "-m", "agautolab.mission_witness",
            str(mission), str(transcript), "--cwd", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0
