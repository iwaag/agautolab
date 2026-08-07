"""Acceptance gates: shell commands from job.yaml run inside target/."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

OUTPUT_TAIL_CHARS = 4000


@dataclass
class GateResult:
    command: str
    exit_code: int
    output_tail: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def run_gates(gates: list[str], target: Path, timeout: int) -> list[GateResult]:
    results = []
    for command in gates:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=target,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            results.append(
                GateResult(
                    command=command,
                    exit_code=proc.returncode,
                    output_tail=output[-OUTPUT_TAIL_CHARS:],
                    timed_out=False,
                )
            )
        except subprocess.TimeoutExpired as exc:
            output = ""
            for stream in (exc.stdout, exc.stderr):
                if stream:
                    output += stream if isinstance(stream, str) else stream.decode(errors="replace")
            results.append(
                GateResult(
                    command=command,
                    exit_code=-1,
                    output_tail=output[-OUTPUT_TAIL_CHARS:],
                    timed_out=True,
                )
            )
    return results


def summarize(results: list[GateResult]) -> dict:
    return {
        "total": len(results),
        "passed": all(r.passed for r in results),
        "failing": [r.command for r in results if not r.passed],
    }
