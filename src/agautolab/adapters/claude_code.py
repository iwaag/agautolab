"""Claude Code coding adapter built on the shared harness seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agag.agent_config import ResolvedAgent
from agag.harness import run_harness
from . import AdapterError, AdapterResult

OUTPUT_FILENAME = "agent_output.json"


@dataclass
class ClaudeCodeAdapter:
    agent: ResolvedAgent
    args: list[str] = field(default_factory=list)
    allowed_tools: str | None = None
    skip_permissions: bool = False
    add_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict, job_dir: Path | None = None,
                    agent: ResolvedAgent | None = None) -> "ClaudeCodeAdapter":
        if agent is None or agent.harness != "claude_code":
            raise AdapterError("claude_code adapter requires a resolved claude_code profile")
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AdapterError("claude_code adapter_config: 'args' must be a list of strings")
        if "--model" in args or "-m" in args:
            raise AdapterError("claude_code adapter_config: model selection belongs to profile")
        add_dirs = [str(d) for d in config.get("add_dirs", [])]
        if job_dir is not None and config.get("add_job_dir", True):
            add_dirs.append(str(job_dir))
        tools = config.get("allowed_tools")
        if tools is not None and not isinstance(tools, str):
            raise AdapterError("claude_code adapter_config: 'allowed_tools' must be a string")
        return cls(agent, list(args), tools, bool(config.get("skip_permissions", False)), add_dirs)

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        result = run_harness(self.agent, prompt, cwd=workdir, timeout=timeout,
                             allowed_tools=self.allowed_tools, add_dirs=self.add_dirs,
                             extra_args=self.args, skip_permissions=self.skip_permissions)
        return AdapterResult(result.output, result.exit_code, result.meta,
                             {OUTPUT_FILENAME: result.raw_output})
