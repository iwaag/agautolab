"""claude_code adapter tests against a stub CLI (no tokens, no real model)."""

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from agautolab.adapters import AdapterError, create
from agautolab.adapters.claude_code import ClaudeCodeAdapter
from agautolab.run_once import run_once
from agautolab.state import EXIT_CONVERGED


def write_stub(path: Path, body: str) -> Path:
    """Create an executable stub 'claude' whose python body is `body`."""
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


RESULT_JSON = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "done: wrote fizzbuzz",
    "session_id": "s-123",
    "num_turns": 4,
    "duration_ms": 1234,
    "total_cost_usd": 0.05,
    "usage": {"input_tokens": 10, "output_tokens": 20},
    "permission_denials": [],
}


def test_parses_json_result_and_meta(tmp_path):
    stub = write_stub(tmp_path / "claude", f"""
        import json, sys
        sys.stdin.read()
        print(json.dumps({RESULT_JSON!r}))
    """)
    adapter = ClaudeCodeAdapter(command=str(stub))
    result = adapter.run("prompt", tmp_path, timeout=10)
    assert result.exit_code == 0
    assert result.output == "done: wrote fizzbuzz"
    assert result.meta["total_cost_usd"] == 0.05
    assert result.meta["usage"]["output_tokens"] == 20
    assert json.loads(result.artifacts["claude_output.json"])["session_id"] == "s-123"


def test_is_error_json_maps_to_nonzero_exit(tmp_path):
    payload = dict(RESULT_JSON, is_error=True, result="something failed")
    stub = write_stub(tmp_path / "claude", f"""
        import json, sys
        sys.stdin.read()
        print(json.dumps({payload!r}))
    """)
    result = ClaudeCodeAdapter(command=str(stub)).run("p", tmp_path, timeout=10)
    assert result.exit_code == 1
    assert result.output == "something failed"


def test_non_json_output_is_error_with_raw_output(tmp_path):
    stub = write_stub(tmp_path / "claude", """
        import sys
        sys.stdin.read()
        print("plain text, not json")
    """)
    result = ClaudeCodeAdapter(command=str(stub)).run("p", tmp_path, timeout=10)
    assert result.exit_code != 0
    assert "plain text" in result.output
    assert result.meta.get("json_parse_error") is True


def test_timeout_returns_exit_minus_one(tmp_path):
    stub = write_stub(tmp_path / "claude", """
        import sys, time
        sys.stdin.read()
        time.sleep(30)
    """)
    result = ClaudeCodeAdapter(command=str(stub)).run("p", tmp_path, timeout=1)
    assert result.exit_code == -1
    assert result.meta.get("timed_out") is True


def test_missing_binary_is_error_result(tmp_path):
    result = ClaudeCodeAdapter(command=str(tmp_path / "nope")).run("p", tmp_path, timeout=5)
    assert result.exit_code == -1
    assert "failed to launch" in result.output


def test_skip_permissions_flag_passthrough(tmp_path):
    stub = write_stub(tmp_path / "claude", f"""
        import json, sys
        sys.stdin.read()
        payload = dict({RESULT_JSON!r})
        payload["result"] = " ".join(sys.argv[1:])
        print(json.dumps(payload))
    """)
    result = ClaudeCodeAdapter(command=str(stub), skip_permissions=True).run("p", tmp_path, 10)
    assert "--dangerously-skip-permissions" in result.output
    result = ClaudeCodeAdapter(command=str(stub)).run("p", tmp_path, 10)
    assert "--dangerously-skip-permissions" not in result.output


def test_bad_args_config_raises_adapter_error():
    with pytest.raises(AdapterError):
        create("claude_code", {"args": "not-a-list"})


def test_run_once_integration_with_stub(tmp_path):
    """Full run-once pass: stub 'claude' writes a file in target/, gate checks it,
    and the cost/usage meta plus raw JSON land in the iteration evidence."""
    stub = write_stub(tmp_path / "claude", f"""
        import json, pathlib, sys
        sys.stdin.read()
        pathlib.Path("fizzbuzz.py").write_text("print('fizzbuzz')\\n")
        print(json.dumps({RESULT_JSON!r}))
    """)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "job.yaml").write_text(textwrap.dedent(f"""
        goal: |
          Write fizzbuzz.py.
        adapter: claude_code
        adapter_config:
          command: "{stub}"
        gates:
          - "test -f fizzbuzz.py"
    """), encoding="utf-8")

    assert run_once(job_dir) == EXIT_CONVERGED
    ev = job_dir / "evidence" / "iter-0001"
    adapter_result = json.loads((ev / "adapter_result.json").read_text())
    assert adapter_result["total_cost_usd"] == 0.05
    assert adapter_result["usage"]["input_tokens"] == 10
    assert json.loads((ev / "claude_output.json").read_text())["num_turns"] == 4
    assert "done: wrote fizzbuzz" in (ev / "adapter_output.txt").read_text()
