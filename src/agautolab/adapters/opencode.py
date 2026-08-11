"""OpenCode coding adapter built on the shared harness seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agag.agent_config import ResolvedAgent
from agag.harness import run_harness

from ..agent_settings import PROJECT_ROOT
from . import AdapterError, AdapterResult

OUTPUT_FILENAME = "agent_output.jsonl"


@dataclass
class OpenCodeAdapter:
    agent: ResolvedAgent
    args: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict, job_dir: Path | None = None,
                    agent: ResolvedAgent | None = None) -> "OpenCodeAdapter":
        del job_dir
        if agent is None or agent.harness != "opencode":
            raise AdapterError("opencode adapter requires a resolved opencode profile")
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AdapterError("opencode adapter_config: 'args' must be a list of strings")
        if "--model" in args or "-m" in args:
            raise AdapterError("opencode adapter_config: model selection belongs to profile")
        if "--dir" in args or any(arg.startswith("--dir=") for arg in args):
            raise AdapterError("opencode adapter_config: working directory is managed by adapter")
        return cls(agent, list(args))

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        # Working-directory defense 2/2: pyagag already synchronizes PWD with
        # subprocess cwd. Pass OpenCode's native --dir too so its project/tool
        # context stays pinned even if its cwd discovery semantics change.
        result = run_harness(self.agent, prompt, cwd=workdir, timeout=timeout,
                             extra_args=[*self.args, "--dir", str(workdir.resolve())],
                             opencode_config=PROJECT_ROOT / "agent" / "opencode-coding.json")
        return AdapterResult(result.output, result.exit_code, result.meta,
                             {OUTPUT_FILENAME: result.raw_output})
