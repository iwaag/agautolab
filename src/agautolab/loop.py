"""`autolab loop`: run `run_once` until the job reaches a terminal verdict.

While `run_once` returns EXIT_CONTINUE (10), sleep briefly and run again.
Any other exit code ends the loop and becomes the loop's own exit code, so
the terminal verdict (0=converged, 20=stuck, 30=error) is visible to systemd
and to shell callers alike. All state lives on disk inside the job dir, so a
crash needs no recovery code: the next loop (or run-once) just continues.

Note: `run_once` exits 0 silently when another process holds the job lock;
the loop inherits that behavior and ends with 0, leaving the job to whoever
holds the lock.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .run_once import run_once
from .state import EXIT_CONTINUE

DEFAULT_SLEEP_SECONDS = 5.0
EXIT_INTERRUPTED = 130


def loop(job_dir: Path, sleep_seconds: float = DEFAULT_SLEEP_SECONDS) -> int:
    try:
        while True:
            code = run_once(job_dir)
            if code != EXIT_CONTINUE:
                return code
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("autolab: loop interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
