"""Fake adapter: token-free stand-in for a coding agent.

Implement-phase prompts: appends one line to a file in the target repo per
run (the classic behavior the loop tests are built on).

Plan-phase prompts (detected by the plan-prompt deliverable markers): writes
PLAN.md and proposed_gates.yaml like a planning coding agent would. When the
prompt carries reviewer feedback (a rejection), the plan gains a revision
section, so reject→replan flows are observable.

adapter_config keys:

    file: relative path inside target/ to append to (default "progress.log")
    line: text appended per run (default "fake adapter was here")
    plan_gates: gates to propose in the plan phase
                (default: ["test -s <file>"] so the implement phase
                converges after one append)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import AdapterResult

# Substrings of build_plan_prompt() output. Literals rather than imports:
# adapters must not import run_once (which imports adapters).
_PLAN_MARKER = "plan, do not implement yet"
_REJECT_MARKER = "plan REJECTED"


@dataclass
class FakeAdapter:
    file: str = "progress.log"
    line: str = "fake adapter was here"
    plan_gates: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict) -> "FakeAdapter":
        plan_gates = config.get("plan_gates", [])
        if not isinstance(plan_gates, list):
            plan_gates = [str(plan_gates)]
        return cls(
            file=str(config.get("file", cls.file)),
            line=str(config.get("line", cls.line)),
            plan_gates=[str(g) for g in plan_gates],
        )

    def run(self, prompt: str, workdir: Path, timeout: int) -> AdapterResult:
        if _PLAN_MARKER in prompt:
            return self._plan(prompt, workdir)
        return self._implement(prompt, workdir)

    def _plan(self, prompt: str, workdir: Path) -> AdapterResult:
        gates = self.plan_gates or [f"test -s {self.file}"]
        plan_path = workdir / "PLAN.md"
        rejected = _REJECT_MARKER in prompt
        if rejected and plan_path.is_file():
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
        verb = "revised" if rejected else "wrote"
        return AdapterResult(
            output=f"fake adapter {verb} PLAN.md and proposed {len(gates)} gate(s)",
            exit_code=0,
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
        return AdapterResult(output=output, exit_code=0)
