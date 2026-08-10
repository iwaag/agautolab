"""autolab CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .detach import run_detached
from .loop import DEFAULT_SLEEP_SECONDS, loop
from .review import approve, reject
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
    p_run_once.add_argument(
        "--detach",
        action="store_true",
        help="Start in a session of its own and return immediately; the run "
        "outlives this shell. Output goes to <job-dir>/detached.log.",
    )

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
    p_loop.add_argument(
        "--detach",
        action="store_true",
        help="Start in a session of its own and return immediately; the run "
        "outlives this shell. Output goes to <job-dir>/detached.log.",
    )

    p_status = sub.add_parser(
        "status", help="Print compact job status (read-only, lock-free)."
    )
    p_status.add_argument("job_dir", type=Path, help="Path to the job directory")
    p_status.add_argument("--json", action="store_true", help="Emit structured JSON")

    p_approve = sub.add_parser(
        "approve",
        help="Accept the proposed plan+gates; the job moves to the implement phase.",
    )
    p_approve.add_argument("job_dir", type=Path, help="Path to the job directory")
    p_approve.add_argument(
        "--gates",
        type=Path,
        metavar="FILE",
        help="YAML file of gate commands (bare list, or under a `gates:` key). "
        "Default: target/proposed_gates.yaml when it holds one.",
    )
    p_approve.add_argument(
        "--gate",
        action="append",
        metavar="COMMAND",
        help="A single gate command; repeatable.",
    )

    p_reject = sub.add_parser(
        "reject",
        help="Reject the proposed plan; feedback goes to NOTES.md and planning resumes.",
    )
    p_reject.add_argument("job_dir", type=Path, help="Path to the job directory")
    p_reject.add_argument(
        "--feedback",
        required=True,
        metavar="FILE_OR_TEXT",
        help="Reviewer feedback: inline text, or a path to a text file",
    )

    args = parser.parse_args(argv)
    if args.command == "run-once":
        if args.detach:
            return run_detached(args.job_dir, ["run-once", str(args.job_dir)])
        return run_once(args.job_dir)
    if args.command == "loop":
        if args.detach:
            return run_detached(
                args.job_dir, ["loop", str(args.job_dir), "--sleep", str(args.sleep)]
            )
        return loop(args.job_dir, sleep_seconds=args.sleep)
    if args.command == "status":
        return status(args.job_dir, as_json=args.json)
    if args.command == "approve":
        return approve(args.job_dir, gates_file=args.gates, gate=args.gate)
    if args.command == "reject":
        return reject(args.job_dir, args.feedback)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
