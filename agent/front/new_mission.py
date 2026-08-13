#!/usr/bin/env python3
"""Register one dumped chat topic in Plane: mission.md plus tasks/*.md."""

import argparse

from agautolab.mission import MissionError, register_dump


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Register the mission written in one topic dump directory. "
            "'mission.md' becomes a Plane task and 'tasks/1.md', '2.md', ... become "
            "its sub-work. The title of each issue is the file's first heading, the "
            "rest of the file is its description. Running it again registers nothing "
            "new: the topic is keyed in Plane itself."
        ),
        epilog=(
            "Example: uv run new_mission.py .local/topics/pj-foo/mission-bar/3\n"
            "With no argument, the newest dump directory is used."
        ),
    )
    parser.add_argument(
        "dump_directory",
        nargs="?",
        help="topic dump directory (.local/topics/<channel>/<topic>/<N>)",
    )
    args = parser.parse_args()
    try:
        print(register_dump(args.dump_directory))
    except MissionError as error:
        parser.exit(1, f"new_mission: {error}\n")


if __name__ == "__main__":
    main()
