"""A mission's own working copy of its project, and how accepted work leaves it.

`robust_workflow` p3 ex1. Until this, every planning and task run worked in
the project folder itself, `.local/projects/<slug>/`, so every mission of a
project shared one working tree, one index and one local branch per
repository. p3's trials showed what that costs: two concurrent missions
edited `wordcount.py` and the requester had to decide which lines a commit
took (E∥F); a cancelled mission's uncommitted edits were still there for the
next one (F → G); and a commit a worker made before anybody agreed was
published by whichever close-out came next (G, H, K), because the close-out
pushed everything the shared clone had.

**The isolation unit is the mission.** A mission's copy lives at
`.local/missions/<slug>/m<id>/` and holds one git worktree per repository of
the project folder, on the branch `autolab/m<id>`, started from that
repository's shared branch. It is found again by the mission's id, so a
restart, a callback or a later task resumes the same copy rather than
minting a competing one. Sequential tasks share it; independent missions
never share files or an index. It sits outside the project folder, so a
planning run reading the project never sees another mission's unfinished
work.

Worktrees share one repository's refs. A copy's own configuration therefore
refuses pushes (`remote.<name>.pushurl` per worktree): what a worker commits
stays on its mission branch. A commit there is a **checkpoint** — saved, and
no claim of acceptance, integration, publication or completion.

**The project folder holds integrated work only.** Accepted work reaches it
through `integrate`, one serialized operation per project (`project_lock`):

- the accepted content is a set of commits, one per repository the mission
  changed, bound when the requester's agreement closes a task;
- a commit already contained in the shared branch is **already integrated**
  (ancestry, not names), so a retry or a crash after the push converges on
  the same result instead of merging twice;
- a shared branch that has not moved is fast-forwarded; one that moved is
  merged only when the two sides touched **different files** — the same
  file, conflicting or not, returns to the mission for a combined review;
- a repository with a remote that autolab publishes (`main`, `direction`,
  `devlog`, or one the worker named) is integrated *by* pushing: the push is
  the compare-and-swap on the shared target, and a rejected push is fetched
  and decided again rather than forced.

A mission that ends — its last task closed, or cancelled, or replaced — is
**released**: anything uncommitted is committed to its branch and the
worktrees are removed. The branch stays, so no work is erased, and a re-plan
that gives the mission more work re-attaches it.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .instance import AGAUTOLAB_ROOT
from .project_init import GIT_AUTHOR_EMAIL, GIT_AUTHOR_NAME, PROJECTS_ROOT

MISSIONS_ROOT = AGAUTOLAB_ROOT / ".local" / "missions"
BRANCH_PREFIX = "autolab/m"
#: The repositories `init_project` creates are autolab's own to publish, as
#: the close-out always did for `main` and the devlog. Any other repository
#: of a pattern project is published only when the worker names it
#: (`publish.flag`), because its README says who pushes it.
STANDARD_REPOSITORIES = ("main", "direction", "devlog")
#: What a mission copy's pushes are sent to: a path that is no repository, so
#: git refuses with this text in its message.
NO_PUSH_URL = "/autolab-mission-copies-do-not-push/autolab-integrates-accepted-work"
VIEW_FILE = ".mission.json"
LOCK_FILE = ".lock"

__all__ = [
    "BRANCH_PREFIX",
    "Integration",
    "IntegrationError",
    "MISSIONS_ROOT",
    "MissionView",
    "RepoOutcome",
    "STANDARD_REPOSITORIES",
    "branch_name",
    "commit_paths",
    "commit_pending",
    "dirty_repositories",
    "ensure_view",
    "existing_view",
    "integrate",
    "pending_changes",
    "project_lock",
    "project_name_from_view",
    "refresh_view",
    "release_view",
    "repositories",
    "set_aside",
    "snapshot",
    "tree_of",
    "files_between",
    "view_path",
]


class IntegrationError(RuntimeError):
    """A git operation on a project or a mission copy failed."""


# --- git ---------------------------------------------------------------------

_IDENTITY = ("-c", f"user.name={GIT_AUTHOR_NAME}", "-c", f"user.email={GIT_AUTHOR_EMAIL}")


def _git(cwd: Path, *arguments: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=str(cwd), env=env, text=True, capture_output=True,
        )
    except OSError as error:
        raise IntegrationError(f"git {arguments[0]} failed: {error}") from error
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:400]
        raise IntegrationError(f"git {' '.join(arguments[:2])} in {cwd.name} failed: {detail}")
    return completed


def _out(cwd: Path, *arguments: str, env: dict | None = None) -> str:
    return _git(cwd, *arguments, env=env).stdout.strip()


def _ok(cwd: Path, *arguments: str, env: dict | None = None) -> bool:
    return _git(cwd, *arguments, env=env, check=False).returncode == 0


def _rev(cwd: Path, revision: str) -> str | None:
    completed = _git(cwd, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False)
    return (completed.stdout.strip() or None) if completed.returncode == 0 else None


def _is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    return _ok(cwd, "merge-base", "--is-ancestor", older, newer)


def _changed_files(cwd: Path, old: str, new: str) -> set[str]:
    return {line for line in _out(cwd, "diff", "--name-only", old, new).splitlines() if line}


def _status_paths(cwd: Path) -> set[str]:
    """Paths with uncommitted changes, untracked ones included (ignored ones not)."""
    paths = set()
    # Not `_out`: its strip would eat the leading space of " M path".
    for line in _git(cwd, "status", "--porcelain", "-uall", "--no-renames", "-z").stdout.split("\0"):
        if len(line) > 3:
            paths.add(line[3:])
    return paths


def _working_tree(cwd: Path) -> str:
    """The tree id of everything in the working copy that git would commit —
    the content identity of a copy, dirty or not, without touching its index."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if _rev(cwd, "HEAD"):
            _git(cwd, "read-tree", "HEAD", env=env)
        _git(cwd, "add", "-A", env=env)
        return _out(cwd, "write-tree", env=env)


def _shared_branch(repo: Path) -> str:
    completed = _git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    if completed.returncode != 0:
        raise IntegrationError(f"{repo.name}: the project folder's checkout is not on a branch")
    return completed.stdout.strip()


def _upstream(repo: Path) -> str | None:
    """The remote this repository publishes to (`origin` when it has one)."""
    remotes = _out(repo, "remote").split()
    return "origin" if "origin" in remotes else (remotes[0] if remotes else None)


# --- where things are --------------------------------------------------------


def branch_name(mission_id: int) -> str:
    return f"{BRANCH_PREFIX}{int(mission_id)}"


def view_path(slug: str, mission_id: int, missions_root: Path | None = None) -> Path:
    return (missions_root or MISSIONS_ROOT) / slug / f"m{int(mission_id)}"


def project_name_from_view(cwd: Path, missions_root: Path | None = None) -> str | None:
    """The project whose mission copy contains `cwd`, or None."""
    try:
        relative = cwd.resolve().relative_to((missions_root or MISSIONS_ROOT).resolve())
    except (OSError, ValueError):
        return None
    return relative.parts[0] if relative.parts else None


def repositories(project_root: Path) -> list[str]:
    """The project folder's repositories: top-level folders that are clones."""
    if not project_root.is_dir():
        return []
    return sorted(
        entry.name for entry in project_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".") and (entry / ".git").is_dir()
    )


@contextmanager
def project_lock(slug: str, missions_root: Path | None = None) -> Iterator[None]:
    """One project's copies and integrations, one at a time. The listener is
    serial already; the lock is for everything else that runs git here (the
    CLI inside a run, a retried close-out after a restart)."""
    directory = (missions_root or MISSIONS_ROOT) / slug
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / LOCK_FILE, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --- a mission's copy --------------------------------------------------------


@dataclass
class MissionView:
    slug: str
    mission_id: int
    path: Path
    project_root: Path
    #: repository → its worktree in this copy
    worktrees: dict[str, Path] = field(default_factory=dict)
    #: what `ensure_view` did this time, one phrase per repository, for the log
    actions: list[str] = field(default_factory=list)

    @property
    def branch(self) -> str:
        return branch_name(self.mission_id)


def _worktree_config(repo: Path, worktree: Path) -> None:
    """Refuse pushes from the copy: its commits are checkpoints."""
    _git(repo, "config", "extensions.worktreeConfig", "true")
    remote = _upstream(repo)
    if remote:
        _git(worktree, "config", "--worktree", f"remote.{remote}.pushurl", NO_PUSH_URL)


def _attach(repo: Path, worktree: Path, branch: str) -> str:
    """Put the mission's branch of `repo` at `worktree`: resume the branch if
    it exists, else start it from the shared branch."""
    _git(repo, "worktree", "prune")
    shared = _shared_branch(repo)
    target = _rev(repo, shared)
    if _rev(repo, f"refs/heads/{branch}"):
        tip = _rev(repo, branch)
        if target and tip != target and _is_ancestor(repo, tip, target):
            # Nothing of its own that the shared branch lacks: re-attach at
            # the newest shared state rather than an old base.
            _git(repo, "update-ref", f"refs/heads/{branch}", target, tip)
        _git(repo, "worktree", "add", str(worktree), branch)
        return f"{repo.name}: resumed {branch}"
    if target is None:
        _git(repo, "worktree", "add", "--orphan", "-b", branch, str(worktree))
        return f"{repo.name}: started {branch} (empty repository)"
    _git(repo, "worktree", "add", "-b", branch, str(worktree), shared)
    return f"{repo.name}: started {branch} at {shared} {target[:12]}"


def ensure_view(
    slug: str,
    mission_id: int,
    *,
    projects_root: Path | None = None,
    missions_root: Path | None = None,
) -> MissionView:
    """The mission's copy, made or resumed. Idempotent: an existing worktree
    is kept as it is, a missing one is attached, and a repository added to
    the project since is attached too. Everything that is not a repository
    (README_PROJECT.md, a plain devlog folder) is linked to the project
    folder's own."""
    project_root = (projects_root or PROJECTS_ROOT) / slug
    path = view_path(slug, mission_id, missions_root)
    branch = branch_name(mission_id)
    view = MissionView(slug, int(mission_id), path, project_root)
    with project_lock(slug, missions_root):
        path.mkdir(parents=True, exist_ok=True)
        record_file = path / VIEW_FILE
        record = json.loads(record_file.read_text()) if record_file.is_file() else {
            "mission": int(mission_id), "project": slug, "branch": branch, "base": {},
        }
        for name in repositories(project_root):
            repo = project_root / name
            worktree = path / name
            if (worktree / ".git").is_file():
                head = _git(worktree, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
                if head != branch:
                    raise IntegrationError(f"{worktree} is on {head or 'a detached HEAD'}, not {branch}")
            else:
                if worktree.exists() or worktree.is_symlink():
                    raise IntegrationError(f"{worktree} exists and is not this mission's worktree")
                view.actions.append(_attach(repo, worktree, branch))
                _worktree_config(repo, worktree)
                record["base"].setdefault(name, _rev(worktree, "HEAD"))
            view.worktrees[name] = worktree
        if project_root.is_dir():
            for entry in project_root.iterdir():
                if entry.name.startswith(".") or entry.name in view.worktrees:
                    continue
                link = path / entry.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(entry)
        record_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return view


def existing_view(
    slug: str, mission_id: int, *, projects_root: Path | None = None, missions_root: Path | None = None,
) -> MissionView | None:
    """The mission's copy as it is on disk, or None — never creates one."""
    path = view_path(slug, mission_id, missions_root)
    if not path.is_dir():
        return None
    project_root = (projects_root or PROJECTS_ROOT) / slug
    view = MissionView(slug, int(mission_id), path, project_root)
    for name in repositories(project_root):
        if (path / name / ".git").is_file():
            view.worktrees[name] = path / name
    return view


def tree_of(worktree: Path, commit: str) -> str:
    return _out(worktree, "rev-parse", f"{commit}^{{tree}}")


def files_between(worktree: Path, old: str | None, new: str | None) -> list[str]:
    """Files that differ between two trees or commits (either may be absent)."""
    empty = _out(worktree, "hash-object", "-t", "tree", "/dev/null")
    return sorted(_changed_files(worktree, old or empty, new or empty))


def snapshot(view: MissionView) -> dict[str, tuple[str, str]]:
    """`repository → (head, working tree id)` for each repository this
    mission holds something in: commits the shared branch lacks, or
    uncommitted changes. The content identity of a checkpoint."""
    held = {}
    for name, worktree in sorted(view.worktrees.items()):
        head = _rev(worktree, "HEAD") or ""
        shared = _rev(view.project_root / name, _shared_branch(view.project_root / name))
        ahead = bool(head) and (shared is None or not _is_ancestor(worktree, head, shared))
        if ahead or _status_paths(worktree):
            held[name] = (head, _working_tree(worktree))
    return held


def commit_pending(view: MissionView, message: str) -> dict[str, str]:
    """Commit whatever is uncommitted in each worktree of the copy, onto the
    mission's branch. Returns the repositories committed and their commits."""
    committed = {}
    for name, worktree in sorted(view.worktrees.items()):
        if not _status_paths(worktree):
            continue
        _git(worktree, "add", "-A")
        _git(worktree, *_IDENTITY, "commit", "--no-verify", "-q", "-m", message)
        committed[name] = _out(worktree, "rev-parse", "HEAD")
    return committed


def pending_changes(view: MissionView) -> dict[str, str]:
    """`repository → commit` for each repository whose mission branch holds
    commits its shared branch does not: what an acceptance would bind."""
    changes = {}
    for name, worktree in sorted(view.worktrees.items()):
        head = _rev(worktree, "HEAD")
        if head is None:
            continue
        shared = _rev(view.project_root / name, _shared_branch(view.project_root / name))
        if shared is None or not _is_ancestor(worktree, head, shared):
            changes[name] = head
    return changes


def release_view(view: MissionView, reason: str) -> str:
    """End a mission's copy: keep anything uncommitted on its branch, remove
    the worktrees and the folder. One line saying what was kept."""
    if not view.path.exists():
        return f"{view.path.name}: nothing to release"
    kept = []
    with project_lock(view.slug, view.path.parent.parent):
        for name, worktree in sorted(view.worktrees.items()):
            if not (worktree / ".git").exists():
                continue
            committed = commit_pending(
                MissionView(view.slug, view.mission_id, view.path, view.project_root, {name: worktree}),
                f"[AUTO] m{view.mission_id} {reason}: uncommitted work kept on its branch",
            )
            if committed:
                kept.append(name)
            _git(view.project_root / name, "worktree", "remove", "--force", str(worktree))
        for entry in view.path.iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
        try:
            view.path.rmdir()
        except OSError:
            pass
    held = f"; uncommitted work in {', '.join(kept)} was committed to it" if kept else ""
    return f"m{view.mission_id}'s working copy is released ({reason}); its branch {view.branch} is kept{held}"


# --- integration -------------------------------------------------------------


@dataclass
class RepoOutcome:
    repo: str
    commit: str
    #: `already`, `fast-forward`, `merged`, `conflict`, `overlap`, `blocked`
    status: str
    branch: str = ""
    #: the shared branch's commit after integration (or before, if refused)
    target: str = ""
    files: list[str] = field(default_factory=list)
    detail: str = ""
    #: `pushed`, `level`, `local` (not published by autolab), or ``""``
    published: str = ""
    #: the merged tree, for `merged`
    tree: str = ""

    @property
    def refused(self) -> bool:
        return self.status in ("conflict", "overlap", "blocked")


@dataclass
class Integration:
    outcomes: list[RepoOutcome]

    @property
    def refused(self) -> list[RepoOutcome]:
        return [outcome for outcome in self.outcomes if outcome.refused]

    @property
    def ok(self) -> bool:
        return not self.refused


def _publishes(repo: str, publish: Iterable[str]) -> bool:
    return repo in STANDARD_REPOSITORIES or repo in set(publish)


def _sync_with_remote(repo: Path, branch: str, remote: str, env: dict) -> str | None:
    """Fetch the remote branch and bring the project folder level with it.
    Returns a refusal reason, or None. A remote without the branch yet (an
    empty repository) is level by definition."""
    completed = _git(repo, "fetch", "--quiet", remote, branch, env=env, check=False)
    if completed.returncode != 0:
        if "couldn't find remote ref" in (completed.stderr or ""):
            return None
        raise IntegrationError(f"{repo.name}: fetch {remote} failed: {(completed.stderr or '').strip()[:300]}")
    theirs = _rev(repo, "FETCH_HEAD")
    ours = _rev(repo, branch)
    if theirs is None or theirs == ours:
        return None
    if ours is None or _is_ancestor(repo, ours, theirs):
        _merge_into_checkout(repo, theirs)
        return None
    if _is_ancestor(repo, theirs, ours):
        # Committed here and not pushed yet — a planning note, a devlog
        # record, a push that failed last time. It is ours to publish.
        _git(repo, "push", "--quiet", remote, f"{branch}:{branch}", env=env)
        return None
    return f"{repo.name}'s {branch} has diverged from {remote}/{branch}"


def _merge_into_checkout(repo: Path, commit: str) -> None:
    """Fast-forward the project folder's checkout (its branch and files)."""
    _git(repo, "merge", "--ff-only", "--quiet", commit)


def _decide(repo: Path, name: str, commit: str, branch: str) -> RepoOutcome:
    target = _rev(repo, branch)
    outcome = RepoOutcome(name, commit, "", branch, target or "")
    if _rev(repo, commit) is None:
        return RepoOutcome(name, commit, "blocked", branch, target or "", detail=f"commit {commit[:12]} is unknown")
    if target and _is_ancestor(repo, commit, target):
        outcome.status = "already"
        return outcome
    if target is None or _is_ancestor(repo, target, commit):
        changed = _changed_files(repo, target, commit) if target else set()
        outcome.status = "fast-forward"
    else:
        base = _out(repo, "merge-base", target, commit)
        theirs = _changed_files(repo, base, target)
        ours = _changed_files(repo, base, commit)
        merged = _git(repo, "merge-tree", "--write-tree", "--name-only", target, commit, check=False)
        if merged.returncode == 1:
            files = []
            for line in merged.stdout.splitlines()[1:]:
                if not line.strip():
                    break
                files.append(line.strip())
            return RepoOutcome(name, commit, "conflict", branch, target, sorted(set(files) or ours & theirs),
                               f"{branch} moved since this mission began and the changes conflict")
        if merged.returncode != 0:
            raise IntegrationError(f"{name}: merge-tree failed: {merged.stderr.strip()[:300]}")
        if ours & theirs:
            return RepoOutcome(name, commit, "overlap", branch, target, sorted(ours & theirs),
                               f"{branch} moved since this mission began and changed the same files")
        changed = ours
        outcome.status = "merged"
        outcome.tree = merged.stdout.splitlines()[0].strip()
    dirty = _status_paths(repo) & changed
    if dirty:
        return RepoOutcome(name, commit, "blocked", branch, target or "", sorted(dirty),
                           "the project folder has uncommitted changes to these files that nobody owns")
    return outcome


def integrate(
    slug: str,
    mission_id: int,
    accepted: dict[str, str],
    *,
    publish: Iterable[str] = (),
    label: str = "",
    env: dict | None = None,
    projects_root: Path | None = None,
    missions_root: Path | None = None,
    attempts: int = 3,
) -> Integration:
    """Bring accepted commits into the project's shared branches, and publish
    the ones autolab publishes. All or nothing: every repository is decided
    before anything moves, and a refusal moves nothing. Idempotent."""
    project_root = (projects_root or PROJECTS_ROOT) / slug
    env = env or dict(os.environ)
    publish = set(publish)
    with project_lock(slug, missions_root):
        for attempt in range(attempts):
            outcomes = []
            for name, commit in sorted(accepted.items()):
                repo = project_root / name
                branch = _shared_branch(repo)
                remote = _upstream(repo) if _publishes(name, publish) else None
                if remote:
                    refusal = _sync_with_remote(repo, branch, remote, env)
                    if refusal:
                        outcomes.append(RepoOutcome(name, commit, "blocked", branch, detail=refusal))
                        continue
                outcome = _decide(repo, name, commit, branch)
                outcome.published = "pending" if remote else "local"
                outcomes.append(outcome)
            if any(outcome.refused for outcome in outcomes):
                return Integration(outcomes)
            rejected = False
            for outcome in outcomes:
                repo = project_root / outcome.repo
                if outcome.status == "already":
                    result = _rev(repo, outcome.branch)
                elif outcome.status == "merged":
                    result = _out(
                        repo, *_IDENTITY, "commit-tree", outcome.tree,
                        "-p", outcome.target, "-p", outcome.commit,
                        "-m", f"[AUTO] integrate m{mission_id}{label} into {outcome.branch}",
                    )
                else:
                    result = outcome.commit
                if outcome.published == "pending" and outcome.status == "already":
                    # The remote was fetched and levelled above, and it holds the commit.
                    outcome.published = "level"
                elif outcome.published == "pending":
                    remote = _upstream(repo)
                    pushed = _git(repo, "push", "--quiet", remote, f"{result}:refs/heads/{outcome.branch}",
                                  env=env, check=False)
                    if pushed.returncode != 0:
                        if "rejected" in pushed.stderr or "fetch first" in pushed.stderr:
                            rejected = True  # somebody else moved it: decide again
                            break
                        raise IntegrationError(f"{outcome.repo}: push failed: {pushed.stderr.strip()[:300]}")
                    outcome.published = "pushed"
                if _rev(repo, outcome.branch) != result:
                    _merge_into_checkout(repo, result)
                outcome.target = result
            if rejected:
                continue
            return Integration(outcomes)
        raise IntegrationError(f"{slug}: the shared branch kept moving during {attempts} integration attempts")


def refresh_view(view: MissionView) -> list[str]:
    """Bring each clean worktree whose branch holds nothing of its own up to
    its shared branch — after an integration, so the next task continues on
    the combined result. Never moves a worktree that has work."""
    moved = []
    for name, worktree in sorted(view.worktrees.items()):
        repo = view.project_root / name
        target = _rev(repo, _shared_branch(repo))
        head = _rev(worktree, "HEAD")
        if not target or head == target or _status_paths(worktree):
            continue
        if head is None or _is_ancestor(worktree, head, target):
            _git(worktree, "merge", "--ff-only", "--quiet", target)
            moved.append(name)
    return moved


# --- records written straight into the project folder -----------------------


def commit_paths(
    repo: Path, paths: list[str], message: str, *, publish: bool, env: dict | None = None,
    slug: str | None = None, missions_root: Path | None = None,
) -> str:
    """Commit exactly `paths` in a project-folder repository and publish it —
    autolab's own records (the devlog, a plan's notes), never a worker's
    files. Returns `pushed`, `committed` or `nothing`."""
    env = env or dict(os.environ)
    with project_lock(slug or repo.parent.name, missions_root):
        _git(repo, "add", "-A", "--", *paths)
        if not _git(repo, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode:
            return "nothing"
        _git(repo, *_IDENTITY, "commit", "--no-verify", "-q", "-m", message, "--", *paths)
        remote = _upstream(repo) if publish else None
        if remote is None:
            return "committed"
        branch = _shared_branch(repo)
        for _ in range(3):
            pushed = _git(repo, "push", "--quiet", remote, f"{branch}:{branch}", env=env, check=False)
            if pushed.returncode == 0:
                return "pushed"
            if "rejected" not in pushed.stderr and "fetch first" not in pushed.stderr:
                raise IntegrationError(f"{repo.name}: push failed: {pushed.stderr.strip()[:300]}")
            _git(repo, "fetch", "--quiet", remote, branch, env=env)
            _git(repo, *_IDENTITY, "rebase", "--quiet", "--autostash", "FETCH_HEAD")
        raise IntegrationError(f"{repo.name}: the remote kept moving while publishing a record")


def dirty_repositories(project_root: Path) -> dict[str, set[str]]:
    """`repository → uncommitted paths` in the project folder."""
    return {name: paths for name in repositories(project_root)
            if (paths := _status_paths(project_root / name))}


def set_aside(repo: Path, branch: str, message: str) -> str | None:
    """Move uncommitted changes nobody can attribute out of a project-folder
    checkout onto `branch` (a commit whose parent is the checkout's HEAD),
    then clean the checkout. Returns the commit, or None when it was clean.
    Never hands the changes to anybody: they wait on the branch for a person."""
    if not _status_paths(repo):
        return None
    tree = _working_tree(repo)
    head = _rev(repo, "HEAD")
    parents = ["-p", head] if head else []
    commit = _out(repo, *_IDENTITY, "commit-tree", tree, *parents, "-m", message)
    _git(repo, "update-ref", f"refs/heads/{branch}", commit)
    _git(repo, "reset", "--hard", "--quiet")
    _git(repo, "clean", "-fdq")
    return commit
