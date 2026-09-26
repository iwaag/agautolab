"""A study laid out from its setup request, before any model reads it.

`init_project` runs on every serving of a project channel. A workspace
without `README_PROJECT.md` is scaffolded the ordinary way, with `main/`,
`direction/` and `devlog/`. That is how every study opened by `agproject`
before sage p2 got a game's folders first (worldtrend, protoprey-research).
The setup request now says what it wants in a fenced `ag-setup` block
(`ag.project-setup.v1`, pyagag `agag.project`), and this module reads it
**first**:

- `README_PROJECT.md`, the marker, generated from the block. The block is
  the record the marker can be rebuilt from, and it lives in the setup
  topic, not on this disk: `autolab project establish <slug>` rebuilds it.
- `main/` on the standard internal route (`autodev/<slug>`), holding the
  research plan exactly as posted (`RESEARCHPLAN.md`), a `README.md`,
  `methods/` and `reports/INDEX.md`. It is committed and pushed. Nothing
  that already exists is overwritten.
- No `direction/`, no `devlog/` repository, no `publish/`.

The serving that answers the setup topic then says so in one line,
`study layout established: main/ = <repository> at <commit>`, which is what
`agproject status` reads as "the workspace exists". The planner still reads
the request and may refine `main/README.md`; what the study's knowledge
*is* remains its call.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agag.project import (
    ESTABLISHED_RE,
    PROJECT_CHANNEL_PREFIX,
    SETUP_TOPIC_PREFIX,
    established_line,
    parse_setup,
)
from agag.selfnote import is_selfnote
from agag.zulip import topic_history_across_resolve

from . import project_init
from .project_init import (
    PATTERN_MARKER,
    commit_all_and_push,
    ensure_clone,
    ensure_gitea_repo,
    ensure_gitignore,
    load_gitea_config,
)

STUDY = "study"
#: What `agproject` appends to a document it posts; not part of the document.
_ORIGIN_TAIL = re.compile(r"\n+---\n(?:Opened from .*|Opened by hand.*)\s*$", re.DOTALL)

__all__ = ["Established", "establish_study", "established_answer", "find_setup", "prepare_pattern", "restore"]


@dataclass(frozen=True)
class Established:
    repository: str
    revision: str
    created: tuple[str, ...]

    def line(self) -> str:
        extra = ("created " + ", ".join(self.created)) if self.created else "already in place"
        return established_line(self.repository, self.revision, extra=extra)


def _block_in(history: list[dict], project: str) -> tuple[dict, int] | None:
    for message in reversed(history or []):
        if is_selfnote(message.get("content")):
            continue
        block = parse_setup(message.get("content"))
        if block and block.get("slug") == project:
            return block, int(message.get("id", 0))
    return None


def find_setup(client, project: str, history: list[dict] | None = None) -> tuple[dict, int] | None:
    """The project's `ag-setup` block and the id of the post holding it:
    from the history already read, else from `workplan-setup-<slug>`."""
    found = _block_in(history or [], project)
    if found is not None or client is None:
        return found
    channel = f"{PROJECT_CHANNEL_PREFIX}{project}"
    return _block_in(topic_history_across_resolve(client, channel, f"{SETUP_TOPIC_PREFIX}{project}", 200), project)


def _document(client, block: dict) -> str:
    match = re.search(r"#(\d+)", block.get("document", ""))
    if client is None or not match:
        return ""
    message = client.message(int(match.group(1))) or {}
    return _ORIGIN_TAIL.sub("", str(message.get("content") or "")).strip()


def _write_new(path: Path, text: str, created: list[str], root: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    created.append(str(path.relative_to(root)))


def marker_text(project: str, block: dict, request_id: int, repository: str) -> str:
    about = block.get("about") or "(the research plan says what this study is about)"
    origin = block.get("origin") or "a request by hand"
    return f"""# {project}

Follows the **study** pattern (`autolab doc patterns`). Subject: {about}

## Where this study is discussed

- Channel `#{block.get('channel') or PROJECT_CHANNEL_PREFIX + project}`; research plan
  `{block.get('document', '')}` (copied unchanged into `main/RESEARCHPLAN.md`).
- Setup: `{SETUP_TOPIC_PREFIX}{project}`, request #{request_id} — its `ag-setup` block is the record this
  file is generated from; `autolab project establish {project}` rebuilds it.
- Grew out of {origin}.

## Folders

- `main/` — the study's knowledge, at `{repository}` (standard internal route). Written publish-ready:
  `README.md` says what the knowledge is and how its index is kept; `RESEARCHPLAN.md` is the founding
  plan; `methods/` holds how investigations are done; `reports/` holds findings, one row per
  investigation in `reports/INDEX.md`. Private host facts, downloads and logs go in an ignored
  `.local/`. Agents commit and push `main/`.
- `publish/` — none yet. A public repository is supplied by the developer when one is wanted; only the
  developer pushes it.

The knowledge a sage reads for this study is `main/` unless a publication repository is attached.
"""


def establish_study(project: str, block: dict, request_id: int, *, client=None,
                    projects_root: Path | None = None, gitea=None) -> Established:
    """Lay the study out: `main/` first (created, cloned, seeded, pushed),
    then the marker, so a failure half way is retried from the start and
    never leaves a marker over a missing repository."""
    projects_root = project_init.PROJECTS_ROOT if projects_root is None else projects_root
    root = projects_root / project
    gitea = gitea or load_gitea_config()
    ensure_gitea_repo(gitea, project)
    main = root / "main"
    ensure_clone(gitea, project, main)
    ensure_gitignore(gitea, main)
    created: list[str] = []
    plan = _document(client, block)
    if plan:
        _write_new(main / "RESEARCHPLAN.md", plan, created, root)
    title = project.replace("-", " ")
    _write_new(main / "README.md", f"""# {title}

{block.get('about') or 'A study.'}

- `RESEARCHPLAN.md` — the research plan this study was opened with.
- `methods/` — how investigations are carried out, reusable across rounds.
- `reports/` — findings with their sources; `reports/INDEX.md` lists every investigation, one row each.
""", created, root)
    _write_new(main / "methods" / "README.md", "# Methods\n\nHow investigations in this study are carried out.",
               created, root)
    _write_new(main / "reports" / "INDEX.md",
               "# Reports\n\nOne row per investigation: date, subject, file, status.\n\n"
               "| date | subject | file | status |\n|---|---|---|---|", created, root)
    commit_all_and_push(gitea, main, f"[AUTO] Establish study {project} ({block.get('schema')})")
    repository = f"{gitea.url}/{gitea.org}/{project}.git"
    marker = root / PATTERN_MARKER
    if not marker.exists():
        marker.write_text(marker_text(project, block, request_id, repository), encoding="utf-8")
        created.append(PATTERN_MARKER)
    revision = subprocess.run(["git", "-C", str(main), "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    return Established(repository, revision, tuple(created))


def prepare_pattern(client, project: str, history: list[dict] | None = None, *,
                    projects_root: Path | None = None) -> Established | None:
    """Before `init_project`: a workspace with no marker whose setup request
    names the study pattern is laid out as a study. None when there is
    nothing to do (a marker exists, no block, or another pattern).

    The setup topic is read only for a workspace that does not exist yet —
    a serving of another topic arriving first — so an older project
    without a marker costs no extra read on every serving."""
    projects_root = project_init.PROJECTS_ROOT if projects_root is None else projects_root
    if (projects_root / project / PATTERN_MARKER).exists():
        return None
    found = _block_in(history or [], project)
    if found is None and not (projects_root / project).exists():
        found = find_setup(client, project)
    if found is None:
        return None
    block, request_id = found
    if block.get("pattern") != STUDY:
        return None
    return establish_study(project, block, request_id, client=client, projects_root=projects_root)


def established_answer(project: str, topic: str, history: list[dict], self_id: int, *,
                       done: Established | None = None, projects_root: Path | None = None) -> str | None:
    """The `study layout established` line owed in the setup topic: once,
    in the serving that answers the request, and again after a serving that
    established the study crashed before answering."""
    if not topic.removeprefix("✔ ").startswith(SETUP_TOPIC_PREFIX):
        return None
    projects_root = project_init.PROJECTS_ROOT if projects_root is None else projects_root
    if done is not None:
        # The commit at answer time: the planner may have refined the layout
        # (and `commit_planning_notes` pushed it) since it was established.
        head = subprocess.run(["git", "-C", str(projects_root / project / "main"), "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True, check=False).stdout.strip()
        return Established(done.repository, head or done.revision, done.created).line()
    if _block_in(history, project) is None or (_block_in(history, project)[0].get("pattern") != STUDY):
        return None
    if any(m.get("sender_id") == self_id and ESTABLISHED_RE.search(str(m.get("content") or "")) for m in history):
        return None
    main = projects_root / project / "main"
    if not (projects_root / project / PATTERN_MARKER).exists() or not (main / ".git").exists():
        return None
    head = subprocess.run(["git", "-C", str(main), "rev-parse", "--short=12", "HEAD"],
                          capture_output=True, text=True, check=False)
    remote = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=False)
    if head.returncode != 0 or not head.stdout.strip():
        return None
    return Established(remote.stdout.strip(), head.stdout.strip(), ()).line()


def restore(project: str, *, rewrite_marker: bool = False, client=None,
            projects_root: Path | None = None) -> Established:
    """`autolab project establish <slug>`: the layout (and marker) from the
    setup request as it is recorded in Zulip."""
    from agag.zulip import ZulipClient

    from .instance import SPEC
    from .project_init import ProjectInitError

    client = client or ZulipClient.from_env(SPEC.zulip_env)
    found = find_setup(client, project)
    if found is None:
        raise ProjectInitError(f"no ag-setup block for {project!r} in #pj-{project} > {SETUP_TOPIC_PREFIX}{project}")
    block, request_id = found
    if block.get("pattern") != STUDY:
        raise ProjectInitError(f"{project!r} was set up as a {block.get('pattern')!r}, not a study")
    projects_root = project_init.PROJECTS_ROOT if projects_root is None else projects_root
    marker = projects_root / project / PATTERN_MARKER
    if rewrite_marker and marker.exists():
        marker.unlink()
    return establish_study(project, block, request_id, client=client, projects_root=projects_root)
