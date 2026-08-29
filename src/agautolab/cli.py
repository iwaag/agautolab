"""The `autolab` command: the documentation and repository tools a serving gets.

A role run reaches this by name — `chat_environment` puts the interpreter's
bin directory (`.venv/bin`, where this console script is installed) on the
run's PATH, so nothing about the deployment path is written down anywhere.

Two things live here, and they are both Tool Giving:

- `autolab doc patterns` prints `agent/project_pattern.md`, so the guide can
  name a document instead of carrying the folder conventions itself.
- `autolab project init-repo <folder>` creates and clones the standard
  repository for one workspace folder. It exists as a subcommand precisely so
  that the Gitea token stays inside this process: the agent composes no argv
  carrying it, and nothing here prints it.

`autolab --help` is the usage information that comes with the tool.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from . import project_init
from .instance import AGAUTOLAB_ROOT
from .project_init import GiteaConfig, ProjectInitError
from .project_settings import project_name_from_workspace

#: The documents `autolab doc <name>` can print, by the name the agent types.
DOCUMENTS = {
    "patterns": AGAUTOLAB_ROOT / "agent" / "project_pattern.md",
}

MAIN_FOLDER = "main"
LOCALTEST_PREFIX = "localtest-"
LOCALTEST_STATES = (
    "prepared",
    "waiting_external",
    "running",
    "verified",
    "failed",
    "adoption_pending",
    "complete",
)


class CliError(Exception):
    """Something the caller asked for cannot be done; one clear line explains it."""


def document_text(name: str) -> str:
    path = DOCUMENTS.get(name)
    if path is None:
        known = ", ".join(sorted(DOCUMENTS))
        raise CliError(f"unknown document {name!r}; known documents: {known}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CliError(f"cannot read document {name!r}: {error}") from error


def repository_name(project: str, folder: str) -> str:
    """The standard repository name for one workspace folder.

    `project_init` named the three original repositories this way, and the
    pattern document promises the same shape for any folder a pattern adds.
    """
    return project if folder == MAIN_FOLDER else f"{project}-{folder}"


def localtest_folder_name(paper_id: str) -> str:
    """Return the portable workspace name for one arXiv identifier.

    Modern arXiv IDs retain their dot; old-style IDs contain a slash which is
    unsafe as a single workspace-folder name, so only that separator changes.
    """
    normalized = paper_id.strip().replace("/", "-")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", normalized):
        raise CliError(f"invalid arXiv paper ID: {paper_id!r}")
    return f"{LOCALTEST_PREFIX}{normalized}"


def remote_url(config: GiteaConfig, repo: str) -> str:
    org = urllib.parse.quote(config.org, safe="")
    return f"{config.url}/{org}/{urllib.parse.quote(repo, safe='')}.git"


def existing_remote(destination: Path) -> str | None:
    """`origin` of an existing clone, or None when the folder is not one."""
    if not (destination / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(destination),
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def resolve_project(folder_argument: str | None, cwd: Path) -> str:
    """The project this run is working on: the argument, else the workspace in cwd."""
    if folder_argument:
        return folder_argument
    project = project_name_from_workspace(cwd, project_init.PROJECTS_ROOT)
    if project is None:
        raise CliError(
            "cannot tell which project this is: run inside a project workspace, "
            "or pass --project <slug>"
        )
    return project


def init_repo(folder: str, *, project: str | None = None, cwd: Path | None = None) -> tuple[Path, str]:
    """Create the standard repository for `folder` and clone it into the workspace.

    Returns the clone path and its remote URL. Idempotent: a folder that is
    already the clone of exactly that repository is reported and left alone.
    A folder that is anything else is refused rather than written into.
    """
    if Path(folder).name != folder or folder in {"", ".", ".."}:
        raise CliError(f"invalid folder name: {folder!r}")
    cwd = Path.cwd() if cwd is None else cwd
    slug = resolve_project(project, cwd)
    workspace = project_init.PROJECTS_ROOT / slug
    destination = workspace / folder
    config = project_init.load_gitea_config()
    repo = repository_name(slug, folder)
    wanted = remote_url(config, repo)

    if destination.exists():
        found = existing_remote(destination)
        if found is None:
            raise CliError(
                f"{destination} already exists and is not a git clone; "
                "refusing to touch it"
            )
        if found.rstrip("/") != wanted.rstrip("/"):
            raise CliError(
                f"{destination} is already a clone of {found}, not {wanted}; "
                "refusing to touch it"
            )
        return destination, wanted

    project_init.ensure_gitea_repo(config, repo)
    project_init.ensure_clone(config, repo, destination)
    return destination, wanted


def _localtest_files(paper_id: str) -> dict[str, str]:
    return {
        "README.md": (
            f"# Local test: arXiv {paper_id}\n\n"
            f"Paper: https://arxiv.org/abs/{paper_id}\n\n"
            "This repository records a bounded local reproduction attempt. Read "
            "`localtest.yaml` and `report.md` before resuming it. Add the paper's "
            "official code repository and revision to `report.md` before running it. "
            "When the test reaches a result, write the level reached (L1-L4, see "
            "Autolab's project patterns) to `main/papers/<paper-id>/test.md` and "
            "the `localtest` column of `main/papers/INDEX.md`.\n"
        ),
        "localtest.yaml": (
            "# Resumable state for this one local reproduction attempt.\n"
            f"paper_id: {paper_id}\n"
            "state: prepared\n"
            f"# State values: {', '.join(LOCALTEST_STATES)}.\n"
        ),
        "report.md": (
            "# Local test report\n\n"
            "## Plan\n\n"
            "- Paper and claim to test:\n"
            "- Official code and revision:\n"
            "- Smallest meaningful command:\n"
            "- Expected evidence:\n\n"
            "## Result\n\n"
            "Record commands, installed/downloaded components, actual evidence, "
            "cleanup, and any upper-actor handoff here. Keep credentials, host "
            "facts, and large artifacts under ignored `.local/`.\n"
        ),
    }


def init_localtest(
    paper_id: str, *, project: str | None = None, cwd: Path | None = None
) -> tuple[Path, str, bool]:
    """Create a standard localtest clone with small, committed resume records."""
    folder = localtest_folder_name(paper_id)
    destination, url = init_repo(folder, project=project, cwd=cwd)
    config = project_init.load_gitea_config()
    changed = False
    for name, content in _localtest_files(paper_id).items():
        path = destination / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            changed = True
    # `init-repo` intentionally makes an empty clone; establish main and make
    # the private-work boundary explicit before adding the reusable records.
    changed = project_init.ensure_gitignore(config, destination) or changed
    if changed:
        project_init.commit_all_and_push(
            config, destination, f"Initialize local test for arXiv {paper_id}"
        )
    return destination, url, changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autolab",
        description=(
            "autolab's own tools for the project workspace you are working in."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  autolab doc patterns            print how project folders are\n"
            "                                  organised by pattern\n"
            "  autolab project init-repo publish\n"
            "                                  create autodev/<project>-publish on\n"
            "                                  the local Gitea and clone it into\n"
            "                                  ./publish\n"
            "  autolab project init-localtest 2401.12345\n"
            "                                  create a resumable paper-test repository\n"
        ),
    )
    subcommands = parser.add_subparsers(dest="command")

    doc = subcommands.add_parser(
        "doc",
        help="print one of autolab's documents",
        description="Print one of autolab's documents to standard output.",
    )
    doc.add_argument(
        "name",
        nargs="?",
        help=f"document to print; known documents: {', '.join(sorted(DOCUMENTS))}",
    )

    project = subcommands.add_parser(
        "project",
        help="work on the project workspace",
        description="Commands that act on the project workspace you are inside.",
    )
    project_commands = project.add_subparsers(dest="project_command")
    init = project_commands.add_parser(
        "init-repo",
        help="create the standard repository for a workspace folder and clone it",
        description=(
            "Create the standard repository for one workspace folder on the local "
            "Gitea and clone it into the workspace. The repository is named "
            "<project> for main/ and <project>-<folder> for anything else. "
            "Nothing is pushed. A folder that already is that clone is reported "
            "and left alone; a folder that is anything else is refused."
        ),
    )
    init.add_argument("folder", nargs="?", help="workspace folder, for example publish")
    init.add_argument(
        "--project",
        dest="project",
        help="project slug; by default the workspace the working directory is in",
    )
    localtest = project_commands.add_parser(
        "init-localtest",
        help="create and initialize a repository-backed paper local test",
        description=(
            "Create the standard localtest-<paper-id> repository and initialize "
            "its portable README, resumable state, report skeleton, and .gitignore. "
            "Old-style arXiv IDs replace / with - in the folder and repository name."
        ),
    )
    localtest.add_argument("paper_id", nargs="?", help="arXiv ID, for example 2401.12345")
    localtest.add_argument("--project", help="project slug; defaults to the current workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "doc":
            if not args.name:
                known = ", ".join(sorted(DOCUMENTS))
                print(f"autolab doc: name a document; known documents: {known}", file=sys.stderr)
                return 2
            sys.stdout.write(document_text(args.name))
            return 0
        if args.command == "project":
            if args.project_command is None:
                parser.parse_args(["project", "--help"])
                return 0
            if args.project_command == "init-localtest":
                if not args.paper_id:
                    print("autolab project init-localtest: name an arXiv paper ID", file=sys.stderr)
                    return 2
                destination, url, changed = init_localtest(args.paper_id, project=args.project)
                print(f"path: {destination}")
                print(f"remote: {url}")
                print(f"initialized: {'yes' if changed else 'already present'}")
                return 0
            if not args.folder:
                print(
                    "autolab project init-repo: name the workspace folder, "
                    "for example `autolab project init-repo publish`",
                    file=sys.stderr,
                )
                return 2
            destination, url = init_repo(args.folder, project=args.project)
            print(f"path: {destination}")
            print(f"remote: {url}")
            return 0
    except (CliError, ProjectInitError) as error:
        print(f"autolab: {error}", file=sys.stderr)
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
