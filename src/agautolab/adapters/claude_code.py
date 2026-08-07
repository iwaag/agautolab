"""Claude Code headless adapter.

Runs `claude -p --output-format json` one-shot with cwd=target/, feeding the
prompt on stdin and capturing the stdout JSON as evidence. adapter_config:

    command: claude binary name or path (default "claude")
    args: extra CLI args, e.g. ["--model", "...", "--allowedTools", "..."]
    skip_permissions: add --dangerously-skip-permissions (default false).
        Policy: only ever true on experimental nodes/VMs, never on a machine
        holding real credentials beyond what the job needs.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import AdapterError, AdapterResult

# Keys copied from the result JSON into adapter_result.json evidence.
META_KEYS = (
    "total_cost_usd",
    "usage",
    "modelUsage",
    "num_turns",
    "duration_ms",
    "duration_api_ms",
    "is_error",
    "subtype",
    "terminal_reason",
    "session_id",
    "permission_denials",
)

OUTPUT_JSON_FILENAME = "claude_output.json"


@dataclass
class ClaudeCodeAdapter:
    command: str = "claude"
    args: list[str] = field(default_factory=list)
    skip_permissions: bool = False

    @classmethod
    def from_config(cls, config: dict) -> "ClaudeCodeAdapter":
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AdapterError("claude_code adapter_config: 'args' must be a list of strings")
        return cls(
            command=str(config.get("command", cls.command)),
            args=list(args),
            skip_permissions=bool(config.get("skip_permissions", False)),
        )

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        argv = [self.command, "-p", "--output-format", "json", *self.args]
        if self.skip_permissions:
            argv.append("--dangerously-skip-permissions")
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial = ""
            for stream in (exc.stdout, exc.stderr):
                if stream:
                    partial += stream if isinstance(stream, str) else stream.decode(errors="replace")
            return AdapterResult(
                output=f"claude timed out after {timeout}s",
                exit_code=-1,
                meta={"timed_out": True},
                artifacts={OUTPUT_JSON_FILENAME: partial},
            )
        except OSError as exc:
            return AdapterResult(
                output=f"failed to launch {self.command!r}: {exc}",
                exit_code=-1,
                meta={"launch_error": str(exc)},
            )

        stdout = proc.stdout or ""
        artifacts = {OUTPUT_JSON_FILENAME: stdout}
        try:
            result_json = json.loads(stdout)
        except json.JSONDecodeError:
            output = stdout or proc.stderr or "(no output)"
            return AdapterResult(
                output=output,
                exit_code=proc.returncode if proc.returncode != 0 else 1,
                meta={"json_parse_error": True, "stderr_tail": (proc.stderr or "")[-2000:]},
                artifacts=artifacts,
            )

        meta = {k: result_json[k] for k in META_KEYS if k in result_json}
        output = result_json.get("result") or "(no result text)"
        exit_code = proc.returncode
        if exit_code == 0 and result_json.get("is_error"):
            exit_code = 1
        return AdapterResult(output=output, exit_code=exit_code, meta=meta, artifacts=artifacts)
