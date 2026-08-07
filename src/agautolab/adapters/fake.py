"""Fake adapter: appends one line to a file in the target repo per run.

Exists so the whole run-once loop is testable locally without any model
tokens. adapter_config keys:

    file: relative path inside target/ to append to (default "progress.log")
    line: text appended per run (default "fake adapter was here")
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import AdapterResult


@dataclass
class FakeAdapter:
    file: str = "progress.log"
    line: str = "fake adapter was here"

    @classmethod
    def from_config(cls, config: dict) -> "FakeAdapter":
        return cls(
            file=str(config.get("file", cls.file)),
            line=str(config.get("line", cls.line)),
        )

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        target = workdir / self.file
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(self.line + "\n")
        count = sum(1 for _ in target.open(encoding="utf-8"))
        output = (
            f"fake adapter appended line {count} to {self.file} "
            f"(prompt was {len(prompt)} chars)"
        )
        return AdapterResult(output=output, exit_code=0)
