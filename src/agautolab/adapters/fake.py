"""Fake adapter: token-free stand-in for a coding agent.

Implement phase: appends one line to a file in the target repo per run (the
classic behavior the loop tests are built on). Plan phase: writes PLAN.md and
proposed_gates.yaml like a planning coding agent would, adding a revision
section when a plan is already there, so reject→replan flows are observable.

Which phase it is in is read from the job's state.json — a fact recorded on
disk — rather than sniffed out of the prompt text.

adapter_config keys:

    file: relative path inside target/ to append to (default "progress.log")
    line: text appended per run (default "fake adapter was here")
    plan_gates: gates to propose in the plan phase
                (default: ["test -s <file>"] so the implement phase
                converges after one append)
    phase: force "plan" or "implement" instead of reading state.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_config import ResolvedAgent
from ..harness import identity
from . import AdapterResult


@dataclass
class FakeAdapter:
    file: str = "progress.log"
    line: str = "fake adapter was here"
    plan_gates: list[str] = field(default_factory=list)
    phase: str | None = None
    job_dir: Path | None = None
    agent: ResolvedAgent | None = None

    @classmethod
    def from_config(cls, config: dict, job_dir: Path | None = None,
                    agent: ResolvedAgent | None = None) -> "FakeAdapter":
        plan_gates = config.get("plan_gates", [])
        if not isinstance(plan_gates, list):
            plan_gates = [str(plan_gates)]
        return cls(
            file=str(config.get("file", cls.file)),
            line=str(config.get("line", cls.line)),
            plan_gates=[str(g) for g in plan_gates],
            phase=config.get("phase"),
            job_dir=job_dir,
            agent=agent,
        )

    def _current_phase(self, workdir: Path) -> str:
        if self.phase:
            return self.phase
        job_dir = self.job_dir or workdir.parent
        try:
            state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "implement"
        return state.get("phase") or "implement"

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        if self._current_phase(workdir) == "plan":
            return self._plan(workdir)
        return self._implement(prompt, workdir)

    def _plan(self, workdir: Path) -> AdapterResult:
        gates = self.plan_gates or [f"test -s {self.file}"]
        plan_path = workdir / "PLAN.md"
        revising = plan_path.is_file()
        if revising:
            revisions = plan_path.read_text(encoding="utf-8").count("## Revision") + 1
            with plan_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Revision {revisions}\n\nAddressed reviewer feedback.\n")
        else:
            plan_path.write_text(
                f"# Plan\n\nAppend lines to `{self.file}` until the gates pass.\n",
                encoding="utf-8",
            )
        (workdir / "proposed_gates.yaml").write_text(
            "gates:\n" + "".join(f'  - "{g}"\n' for g in gates), encoding="utf-8"
        )
        verb = "revised" if revising else "wrote"
        meta = identity(self.agent) if self.agent else {}
        meta["outcome"] = "done"
        return AdapterResult(
            output=f"fake adapter {verb} PLAN.md and proposed {len(gates)} gate(s)",
            exit_code=0,
            meta=meta,
        )

    def _implement(self, prompt: str, workdir: Path) -> AdapterResult:
        target = workdir / self.file
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(self.line + "\n")
        count = sum(1 for _ in target.open(encoding="utf-8"))
        output = (
            f"fake adapter appended line {count} to {self.file} "
            f"(prompt was {len(prompt)} chars)"
        )
        meta = identity(self.agent) if self.agent else {}
        meta["outcome"] = "done"
        return AdapterResult(output=output, exit_code=0, meta=meta)
