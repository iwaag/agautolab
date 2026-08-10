"""`--detach`: run an iteration in a session of its own.

`run-once` and `loop` normally hold the caller's terminal for as long as the
coding agent takes. A caller whose own lifetime is shorter than an iteration —
a headless agent session, a command window with a timeout — has no way to
watch one through. Detaching starts the same command in a new process session
(`setsid`), so it survives the caller, and returns immediately with the pid
and the log path. The verdict is read afterwards from `autolab status`.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .state import EXIT_ERROR

DETACH_LOG_NAME = "detached.log"


def run_detached(job_dir: Path, argv: list[str]) -> int:
    """Start `autolab <argv>` detached; print where it went. Returns 0 on spawn."""
    job_dir = job_dir.resolve()
    if not job_dir.is_dir():
        print(f"autolab: job dir not found: {job_dir}", file=sys.stderr)
        return EXIT_ERROR

    log_path = job_dir / DETACH_LOG_NAME
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"\n=== autolab {' '.join(argv)} detached at "
            f"{datetime.now(timezone.utc).isoformat()} ===\n"
        )
        log.flush()
        proc = subprocess.Popen(
            [sys.executable, "-m", "agautolab.cli", *argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    print(
        f"autolab: detached pid {proc.pid}; output goes to {log_path}. "
        f"The verdict is in `autolab status {job_dir}`."
    )
    return 0
