"""Adapter interface and registry.

An adapter is one coding-agent backend. The interface is deliberately tiny so
claude/codex/opencode stay swappable:

    run(prompt, workdir, timeout) -> AdapterResult(output, exit_code)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class AdapterResult:
    output: str
    exit_code: int


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


from . import fake as _fake  # noqa: E402  (registers itself on import)

register("fake", _fake.FakeAdapter.from_config)
