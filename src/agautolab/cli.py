"""autolab CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .run_once import run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autolab", description="Headless auto-development loop orchestrator."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run_once = sub.add_parser(
        "run-once", help="Run exactly one iteration of a job (1 iteration = 1 process)."
    )
    p_run_once.add_argument("job_dir", type=Path, help="Path to the job directory")

    args = parser.parse_args(argv)
    if args.command == "run-once":
        return run_once(args.job_dir)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
