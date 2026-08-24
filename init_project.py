#!/usr/bin/env python3
"""Initialize one autolab project across Plane, Gitea, and local clones."""

import argparse

from agautolab.project_init import ProjectInitError, init_project


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Idempotently create an autolab project: Plane project, Gitea repositories, local clones."
    )
    parser.add_argument("project", help="lowercase project name, for example whack-a-mole")
    parser.add_argument(
        "--main-only",
        action="store_true",
        help="one Gitea repository (main); devlog/ is a plain local folder, no direction/",
    )
    args = parser.parse_args()
    try:
        print(init_project(args.project, main_only=args.main_only or None))
    except ProjectInitError as error:
        parser.exit(1, f"init_project: {error}\n")


if __name__ == "__main__":
    main()
