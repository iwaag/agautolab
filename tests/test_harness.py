"""Deterministic protocol and failure tests for the common process seam."""

import json
import stat
import textwrap
from pathlib import Path

import pytest

from agautolab.agent_config import ResolvedAgent
from agautolab.harness import build_argv, run_harness


def stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def agent(command: Path, harness: str) -> ResolvedAgent:
    model = ("anthropic/claude-sonnet-5" if harness == "claude_code"
             else "ollama/qwen3.6:35b-a3b-coding-nvfp4")
    return ResolvedAgent("coding", "test-profile", harness, model.split("/", 1)[0],
                         model, {}, str(command), "http://127.0.0.1:11434")


def test_claude_json_extraction_and_identity(tmp_path):
    command = stub(tmp_path / "claude", """
        import json, sys
        sys.stdin.read()
        print(json.dumps({"result": "done", "is_error": False, "duration_ms": 12,
                          "num_turns": 3, "total_cost_usd": 0.04,
                          "usage": {"input_tokens": 7}}))
    """)
    result = run_harness(agent(command, "claude_code"), "prompt", cwd=tmp_path, timeout=5)
    assert result.output == "done"
    assert result.exit_code == 0
    assert result.meta == {
        "role": "coding", "profile": "test-profile", "harness": "claude_code",
        "provider": "anthropic", "model": "anthropic/claude-sonnet-5",
        "duration_ms": 12, "num_turns": 3, "is_error": False,
        "cost_usd": 0.04, "usage": {"input_tokens": 7}, "outcome": "done",
    }


def test_opencode_jsonl_extraction_and_aggregation(tmp_path):
    command = stub(tmp_path / "opencode", """
        import json, sys
        sys.stdin.read()
        print(json.dumps({"type": "text", "part": {"text": "worked"}}))
        print(json.dumps({"type": "step_finish", "part": {"cost": 0.02,
              "tokens": {"input": 3, "output": 4, "reasoning": 1,
                         "cache": {"read": 2, "write": 1}}}}))
    """)
    result = run_harness(agent(command, "opencode"), "prompt", cwd=tmp_path, timeout=5)
    assert result.output == "worked"
    assert result.meta["harness"] == "opencode"
    assert result.meta["cost_usd"] == 0.02
    assert result.meta["num_turns"] == 1
    assert result.meta["usage"] == {
        "input": 3, "output": 4, "reasoning": 1, "cache_read": 2, "cache_write": 1
    }


def test_plain_text_is_a_legitimate_answer(tmp_path):
    command = stub(tmp_path / "claude", "import sys; sys.stdin.read(); print('plain')\n")
    result = run_harness(agent(command, "claude_code"), "p", cwd=tmp_path, timeout=5)
    assert result.output == "plain\n"
    assert result.meta["outcome"] == "done"


@pytest.mark.parametrize("kind", ["launch", "timeout", "empty", "is_error"])
def test_failure_paths_are_normalized(tmp_path, kind):
    if kind == "launch":
        command = tmp_path / "missing"
    elif kind == "timeout":
        command = stub(tmp_path / "claude", "import time; time.sleep(5)\n")
    elif kind == "empty":
        command = stub(tmp_path / "claude", "pass\n")
    else:
        command = stub(tmp_path / "claude", """
            import json
            print(json.dumps({"result": "refused", "is_error": True,
                              "subtype": "permission"}))
        """)
    result = run_harness(agent(command, "claude_code"), "p", cwd=tmp_path,
                         timeout=0.05 if kind == "timeout" else 5)
    assert result.exit_code != 0
    assert result.meta["outcome"] in {"failed", "aborted"}
    assert result.meta["failure"]
    assert result.meta["role"] == "coding"


def test_model_argv_mapping_and_smuggling_rejected(tmp_path):
    command = tmp_path / "agent"
    claude = build_argv(agent(command, "claude_code"))
    opencode = build_argv(agent(command, "opencode"))
    assert claude[claude.index("--model") + 1] == "claude-sonnet-5"
    assert opencode[opencode.index("-m") + 1].startswith("ollama/")
    with pytest.raises(ValueError, match="resolved profile"):
        build_argv(agent(command, "opencode"), extra_args=["--model", "wrong"])


def test_raw_output_is_retained_on_failure(tmp_path):
    command = stub(tmp_path / "opencode", "print('diagnostic'); raise SystemExit(2)\n")
    transcript = tmp_path / "raw.jsonl"
    result = run_harness(agent(command, "opencode"), "p", cwd=tmp_path, timeout=5,
                         transcript_path=transcript)
    assert result.meta["outcome"] == "failed"
    assert transcript.read_text() == "diagnostic\n"
