"""Claude Code headless adapter.

Runs `claude -p --output-format json` one-shot with cwd=target/, feeding the
prompt on stdin and capturing stdout as evidence. The whole result JSON goes
into the iteration's adapter_result.json when stdout parses; when it does not,
stdout is the output as written. The process's own exit code is the exit code.

adapter_config:

    command: claude binary name, path, or glob (default "claude"). A glob
        resolves to its newest match at launch, the same way
        agent/gateway.py and agent/session.sh resolve .local/agent/claude_bin.
    args: extra CLI args, e.g. ["--model", "...", "--allowedTools", "..."]
    add_job_dir: also grant the job directory (NOTES.md, evidence/) via
        --add-dir, default true
    skip_permissions: add --dangerously-skip-permissions (default false).
        Only ever true on experimental nodes/VMs.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..binpath import resolve_command
from . import AdapterError, AdapterResult

OUTPUT_JSON_FILENAME = "claude_output.json"


@dataclass
class ClaudeCodeAdapter:
    command: str = "claude"
    args: list[str] = field(default_factory=list)
    skip_permissions: bool = False
    add_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict, job_dir: Path | None = None) -> "ClaudeCodeAdapter":
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AdapterError("claude_code adapter_config: 'args' must be a list of strings")
        add_dirs = [str(d) for d in config.get("add_dirs", [])]
        if job_dir is not None and config.get("add_job_dir", True):
            add_dirs.append(str(job_dir))
        return cls(
            command=str(config.get("command", cls.command)),
            args=list(args),
            skip_permissions=bool(config.get("skip_permissions", False)),
            add_dirs=add_dirs,
        )

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        # Resolved per launch, not at config time: the newest match can change
        # between iterations of a long job.
        command = resolve_command(self.command)
        argv = [command, "-p", "--output-format", "json"]
        for directory in self.add_dirs:
            argv += ["--add-dir", directory]
        argv += self.args
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
                output=f"failed to launch {command!r}: {exc}",
                exit_code=-1,
                meta={"launch_error": str(exc), "command": command},
            )

        stdout = proc.stdout or ""
        artifacts = {OUTPUT_JSON_FILENAME: stdout}
        try:
            result_json = json.loads(stdout)
        except json.JSONDecodeError:
            # Prose is a legitimate answer; it is not a failed iteration.
            return AdapterResult(
                output=stdout or proc.stderr or "(no output)",
                exit_code=proc.returncode,
                meta={"stderr_tail": (proc.stderr or "")[-2000:]},
                artifacts=artifacts,
            )

        meta = dict(result_json) if isinstance(result_json, dict) else {"result": result_json}
        output = meta.pop("result", None) or stdout
        return AdapterResult(
            output=str(output),
            exit_code=proc.returncode,
            meta=meta,
            artifacts=artifacts,
        )
