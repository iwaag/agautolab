"""Adapter interface and registry.

An adapter is one coding-agent harness. The interface is deliberately tiny so
OpenCode, Claude Code, and the test fake stay swappable:

    run(prompt, workdir, timeout) -> AdapterResult(output, exit_code)

A harness adapter is built by `from_config(config, job_dir=None)`; `job_dir` is the
job directory that contains `workdir` (`target/`), for backends that can grant
access to more than the cwd.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable, Protocol

from ..agent_config import ResolvedAgent


@dataclass
class AdapterResult:
    output: str
    exit_code: int
    # Structured metadata (token/cost fields etc.) merged into the
    # iteration's adapter_result.json evidence.
    meta: dict = dataclass_field(default_factory=dict)
    # Extra evidence files (filename -> content) written into evidence/iter-NNNN/.
    artifacts: dict = dataclass_field(default_factory=dict)


class Adapter(Protocol):
    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult: ...


class AdapterError(Exception):
    """Adapter could not be constructed or failed outside its own exit code."""


_REGISTRY: dict[str, Callable[..., Adapter]] = {}


def register(name: str, factory: Callable[..., Adapter]) -> None:
    _REGISTRY[name] = factory


def create(name: str, config: dict, job_dir: Path | None = None,
           agent: ResolvedAgent | None = None) -> Adapter:
    """Build an adapter. `job_dir` lets a harness grant access to the job
    directory alongside target/, so NOTES.md and evidence/ are reachable."""
    factory = _REGISTRY.get(name)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise AdapterError(f"unknown adapter {name!r}; known adapters: {known}")
    return factory(config, job_dir=job_dir, agent=agent)


from . import claude_code as _claude_code  # noqa: E402
from . import fake as _fake  # noqa: E402
from . import opencode as _opencode  # noqa: E402

register("fake", _fake.FakeAdapter.from_config)
register("claude_code", _claude_code.ClaudeCodeAdapter.from_config)
register("opencode", _opencode.OpenCodeAdapter.from_config)
