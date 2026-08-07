"""Adapter interface and registry.

An adapter is one coding-agent backend. The interface is deliberately tiny so
claude/codex/opencode stay swappable:

    run(prompt, workdir, timeout) -> AdapterResult(output, exit_code)
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable, Protocol


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


_REGISTRY: dict[str, Callable[[dict], Adapter]] = {}


def register(name: str, factory: Callable[[dict], Adapter]) -> None:
    _REGISTRY[name] = factory


def create(name: str, config: dict) -> Adapter:
    factory = _REGISTRY.get(name)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise AdapterError(f"unknown adapter {name!r}; known adapters: {known}")
    return factory(config)


from . import claude_code as _claude_code  # noqa: E402
from . import fake as _fake  # noqa: E402

register("fake", _fake.FakeAdapter.from_config)
register("claude_code", _claude_code.ClaudeCodeAdapter.from_config)
