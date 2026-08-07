"""autolab CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .loop import DEFAULT_SLEEP_SECONDS, loop
from .run_once import run_once
from .status import status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autolab", description="Headless auto-development loop orchestrator."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run_once = sub.add_parser(
        "run-once", help="Run exactly one iteration of a job (1 iteration = 1 process)."
    )
    p_run_once.add_argument("job_dir", type=Path, help="Path to the job directory")

    p_loop = sub.add_parser(
        "loop", help="Run iterations until the job converges, gets stuck, or errors."
    )
    p_loop.add_argument("job_dir", type=Path, help="Path to the job directory")
    p_loop.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        metavar="SECONDS",
        help=f"Sleep between iterations (default {DEFAULT_SLEEP_SECONDS:g})",
    )

    p_status = sub.add_parser(
        "status", help="Print compact job status (read-only, lock-free)."
    )
    p_status.add_argument("job_dir", type=Path, help="Path to the job directory")
    p_status.add_argument("--json", action="store_true", help="Emit structured JSON")

    args = parser.parse_args(argv)
    if args.command == "run-once":
        return run_once(args.job_dir)
    if args.command == "loop":
        return loop(args.job_dir, sleep_seconds=args.sleep)
    if args.command == "status":
        return status(args.job_dir, as_json=args.json)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
