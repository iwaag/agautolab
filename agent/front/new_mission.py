#!/usr/bin/env python3
"""Register a new Plane mission for the current project chat."""

import argparse

from agautolab.mission import MissionError, new_mission


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create one actionable Plane mission for the project in the latest dumped pj-* "
            "chat. Run this once after reading that chat log."
        ),
        epilog=(
            'Example: uv run new_mission.py "Add health check" '
            '"Add an HTTP health endpoint and verify a 200 response."'
        ),
    )
    parser.add_argument("mission_name", help="short, outcome-oriented Plane issue title")
    parser.add_argument(
        "mission_description",
        help="complete implementation request; quote it as one shell argument",
    )
    args = parser.parse_args()
    try:
        print(new_mission(args.mission_name, args.mission_description))
    except MissionError as error:
        parser.exit(1, f"new_mission: {error}\n")


if __name__ == "__main__":
    main()
