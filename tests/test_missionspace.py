"""A mission's own working copy and the integration of accepted work, on real
git repositories (robust_workflow p3 ex1). Nothing is mocked: the hazards
these guard against live in git's own behaviour — a shared index, shared
refs, a remote that moved."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agautolab import missionspace as ms


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    """`projects/demo/{main,direction,devlog}` cloned from bare remotes, one
    commit each, plus a README_PROJECT.md that is no repository."""
    remotes = tmp_path / "remotes"
    projects = tmp_path / "projects"
    missions = tmp_path / "missions"
    for name in ("main", "direction", "devlog"):
        bare = remotes / f"{name}.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
        clone = projects / "demo" / name
        git(tmp_path, "clone", "-q", str(bare), str(clone))
        git(clone, "config", "user.email", "t@t")
        git(clone, "config", "user.name", "t")
        (clone / "wordcount.py").write_text("def count(words):\n    return len(words)\n\n\n\n\ndef other():\n    pass\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "base")
        git(clone, "push", "-q", "origin", "HEAD:main")
    (projects / "demo" / "README_PROJECT.md").write_text("# demo\n")

    class Project:
        root = projects / "demo"

        def view(self, mission_id):
            return ms.ensure_view("demo", mission_id, projects_root=projects, missions_root=missions)

        def existing(self, mission_id):
            return ms.existing_view("demo", mission_id, projects_root=projects, missions_root=missions)

        def integrate(self, mission_id, accepted, **kwargs):
            return ms.integrate("demo", mission_id, accepted, projects_root=projects, missions_root=missions, **kwargs)

        def remote_log(self, name="main"):
            return git(remotes / f"{name}.git", "log", "--format=%s", "main").splitlines()

        def remote_head(self, name="main"):
            return git(remotes / f"{name}.git", "rev-parse", "main")

    Project.remotes = remotes
    Project.missions = missions
    Project.projects = projects
    return Project()


def edit(worktree: Path, line: str, name="wordcount.py", where="end") -> None:
    path = worktree / name
    text = path.read_text() if path.exists() else ""
    path.write_text(line + "\n" + text if where == "start" else text + line + "\n")


def commit(worktree: Path, message: str) -> str:
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "-m", message)
    return git(worktree, "rev-parse", "HEAD")


# --- ownership ---------------------------------------------------------------


def test_two_missions_editing_the_same_file_never_see_each_other(project):
    """p3 E∥F: both edited wordcount.py in one tree, and E's diff held F's lines."""
    upper, lower = project.view(10311), project.view(10330)
    edit(upper.worktrees["main"], "# --upper")
    edit(lower.worktrees["main"], "# --lower")

    assert "--lower" not in git(upper.worktrees["main"], "diff")
    assert "--upper" not in git(lower.worktrees["main"], "diff")
    commit(upper.worktrees["main"], "upper")
    assert git(upper.worktrees["main"], "rev-parse", "--abbrev-ref", "HEAD") == "autolab/m10311"
    assert "--lower" not in git(upper.worktrees["main"], "show", "HEAD")
    # The project folder holds integrated work only.
    assert git(project.root / "main", "status", "--porcelain") == ""
    assert project.remote_log() == ["base"]


def test_a_copy_is_found_again_by_its_mission_with_its_work(project):
    """A restart or a later serving resumes the same copy, never a competing one."""
    first = project.view(7)
    edit(first.worktrees["main"], "# half done")
    again = project.view(7)

    assert again.path == first.path
    assert again.actions == []
    assert "# half done" in (again.worktrees["main"] / "wordcount.py").read_text()
    assert git(project.root / "main", "worktree", "list").count("autolab/m7") == 1


def test_everything_that_is_not_a_repository_is_linked(project):
    view = project.view(7)
    assert (view.path / "README_PROJECT.md").read_text() == "# demo\n"
    assert (view.path / "README_PROJECT.md").is_symlink()
    assert sorted(view.worktrees) == ["devlog", "direction", "main"]


def test_a_repository_added_to_the_project_later_is_attached(project):
    view = project.view(7)
    extra = project.root / "gentest-x"
    git(project.root, "init", "-q", "-b", "main", str(extra))
    again = project.view(7)
    assert "gentest-x" in again.worktrees
    assert (again.worktrees["gentest-x"] / ".git").is_file()
    assert view.path == again.path


def test_a_checkpoint_is_saved_and_cannot_be_pushed_from_the_copy(project):
    """A worker's commit before agreement is a checkpoint: it stays on the
    mission branch, and the copy refuses to publish it."""
    view = project.view(10427)
    edit(view.worktrees["main"], "# --capitalized")
    commit(view.worktrees["main"], "checkpoint before agreement")

    pushed = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=view.worktrees["main"],
                            capture_output=True, text=True)
    assert pushed.returncode != 0
    assert project.remote_log() == ["base"]
    # The project folder can still publish: the refusal is the copy's own.
    assert subprocess.run(["git", "push", "--dry-run", "origin", "main"], cwd=project.root / "main",
                          capture_output=True).returncode == 0


def test_snapshot_names_only_what_the_mission_holds(project):
    view = project.view(7)
    assert ms.snapshot(view) == {}
    edit(view.worktrees["main"], "# dirty")
    held = ms.snapshot(view)
    assert list(held) == ["main"]
    head, tree = held["main"]
    ms.commit_pending(view, "checkpoint")
    # Committing what was there does not change the content identity.
    assert ms.snapshot(view)["main"][1] == tree
    assert ms.snapshot(view)["main"][0] != head


# --- cancellation and release ------------------------------------------------


def test_a_cancelled_mission_s_dirty_work_stays_on_its_branch_and_nowhere_else(project):
    """p3 F → G: the cancelled mission's edits were in the tree G started in."""
    lower = project.view(10330)
    edit(lower.worktrees["main"], "# --lower, never accepted")
    line = ms.release_view(lower, "cancelled")

    assert "branch autolab/m10330 is kept" in line and "main" in line
    assert not lower.path.exists()
    assert "--lower" in git(project.root / "main", "show", "autolab/m10330:wordcount.py")
    capitalized = project.view(10427)
    assert "--lower" not in (capitalized.worktrees["main"] / "wordcount.py").read_text()
    assert git(project.root / "main", "status", "--porcelain") == ""
    assert ms.pending_changes(capitalized) == {}


def test_a_released_mission_is_re_attached_at_its_branch(project):
    view = project.view(7)
    edit(view.worktrees["main"], "# kept")
    ms.release_view(view, "finished")
    again = project.view(7)
    assert "# kept" in (again.worktrees["main"] / "wordcount.py").read_text()
    assert project.existing(8) is None


# --- integration -------------------------------------------------------------


def test_accepted_work_is_fast_forwarded_and_published_once(project):
    view = project.view(7)
    edit(view.worktrees["main"], "# feature")
    accepted = commit(view.worktrees["main"], "feature")

    first = project.integrate(7, {"main": accepted})
    assert first.ok
    assert [(o.status, o.published) for o in first.outcomes] == [("fast-forward", "pushed")]
    assert project.remote_head() == accepted
    assert git(project.root / "main", "rev-parse", "HEAD") == accepted
    assert "# feature" in (project.root / "main" / "wordcount.py").read_text()

    # A retry — after a crash between the push and the completion record —
    # recognises the work by ancestry and moves nothing.
    again = project.integrate(7, {"main": accepted})
    assert [(o.status, o.published) for o in again.outcomes] == [("already", "level")]
    assert project.remote_log() == ["feature", "base"]


def test_work_on_different_files_is_merged_when_the_target_moved(project):
    one, two = project.view(1), project.view(2)
    edit(one.worktrees["main"], "# one", name="one.py")
    edit(two.worktrees["main"], "# two", name="two.py")
    first = commit(one.worktrees["main"], "one")
    second = commit(two.worktrees["main"], "two")

    assert project.integrate(1, {"main": first}).ok
    result = project.integrate(2, {"main": second})

    assert [o.status for o in result.outcomes] == ["merged"]
    head = project.remote_head()
    assert git(project.root / "main", "rev-parse", "HEAD") == head
    assert sorted(p for p in (project.root / "main").iterdir() if p.suffix == ".py") == [
        project.root / "main" / "one.py", project.root / "main" / "two.py", project.root / "main" / "wordcount.py"]
    # Both accepted commits are in the result; nothing else is.
    assert git(project.root / "main", "merge-base", "--is-ancestor", first, head) == ""
    assert git(project.root / "main", "merge-base", "--is-ancestor", second, head) == ""
    assert project.integrate(2, {"main": second}).outcomes[0].status == "already"


@pytest.mark.parametrize("first_line, second_line, status", [
    ("# upper", "# lower", "conflict"),           # the same place in the same file
    ("# upper", "# lower at the top", "overlap"),  # the same file, merging cleanly
])
def test_the_same_file_changed_on_both_sides_returns_and_moves_nothing(project, first_line, second_line, status):
    upper, lower = project.view(10311), project.view(10330)
    edit(upper.worktrees["main"], first_line)
    edit(lower.worktrees["main"], second_line, where="start" if status == "overlap" else "end")
    first = commit(upper.worktrees["main"], "upper")
    second = commit(lower.worktrees["main"], "lower")
    assert project.integrate(10311, {"main": first}).ok
    before = project.remote_head()

    result = project.integrate(10330, {"main": second})

    assert not result.ok
    assert result.outcomes[0].status == status
    assert result.outcomes[0].files == ["wordcount.py"]
    assert project.remote_head() == before
    assert git(project.root / "main", "rev-parse", "HEAD") == before


def test_after_the_mission_combines_the_work_it_fast_forwards(project):
    """The returned mission merges the shared branch into its own copy (its
    review is of the combined result), and then integrates as a fast-forward."""
    upper, lower = project.view(10311), project.view(10330)
    edit(upper.worktrees["main"], "# upper")
    edit(lower.worktrees["main"], "# lower")
    assert project.integrate(10311, {"main": commit(upper.worktrees["main"], "upper")}).ok
    commit(lower.worktrees["main"], "lower")
    worktree = lower.worktrees["main"]
    subprocess.run(["git", "merge", "main"], cwd=worktree, capture_output=True)
    (worktree / "wordcount.py").write_text((worktree / "wordcount.py").read_text()
                                          .replace("<<<<<<< HEAD\n", "").replace("=======\n", "")
                                          .split(">>>>>>>")[0])
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "--no-edit")
    combined = git(worktree, "rev-parse", "HEAD")

    result = project.integrate(10330, {"main": combined})
    assert [o.status for o in result.outcomes] == ["fast-forward"]
    text = (project.root / "main" / "wordcount.py").read_text()
    assert "# upper" in text and "# lower" in text


def test_a_remote_moved_by_somebody_else_is_taken_in_before_deciding(project, tmp_path):
    """Two integrators: somebody pushed to the remote directly. Integration
    fetches, and the push is a compare-and-swap it never forces."""
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(project.remotes / "main.git"), str(other))
    edit(other, "# pushed elsewhere", name="elsewhere.py")
    git(other, "add", "-A")
    git(other, "-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "elsewhere")
    git(other, "push", "-q", "origin", "HEAD:main")

    view = project.view(7)
    edit(view.worktrees["main"], "# mine", name="mine.py")
    result = project.integrate(7, {"main": commit(view.worktrees["main"], "mine")})

    assert [o.status for o in result.outcomes] == ["merged"]
    assert project.remote_log()[0].startswith("[AUTO] integrate m7")
    assert set(project.remote_log()[1:]) == {"mine", "elsewhere", "base"}
    assert (project.root / "main" / "elsewhere.py").exists() and (project.root / "main" / "mine.py").exists()


def test_uncommitted_changes_nobody_owns_block_only_where_they_would_be_overwritten(project):
    view = project.view(7)
    edit(view.worktrees["main"], "# mine")
    accepted = commit(view.worktrees["main"], "mine")
    edit(project.root / "main", "# stray")

    result = project.integrate(7, {"main": accepted})
    assert result.outcomes[0].status == "blocked"
    assert result.outcomes[0].files == ["wordcount.py"]
    assert project.remote_log() == ["base"]


def test_one_refused_repository_moves_no_repository(project):
    one, two = project.view(1), project.view(2)
    edit(one.worktrees["main"], "# one")
    assert project.integrate(1, {"main": commit(one.worktrees["main"], "one")}).ok
    edit(two.worktrees["main"], "# two")
    edit(two.worktrees["direction"], "# decision", name="DECISIONS.md")
    accepted = {"main": commit(two.worktrees["main"], "two"),
                "direction": commit(two.worktrees["direction"], "decision")}

    result = project.integrate(2, accepted)
    assert not result.ok
    assert project.remote_log("direction") == ["base"]


def test_a_repository_autolab_does_not_publish_is_integrated_locally(project):
    extra = project.root / "publish"
    git(project.root, "clone", "-q", str(project.remotes / "main.git"), str(extra))
    view = project.view(7)
    edit(view.worktrees["publish"], "# reviewed copy", name="copy.md")
    accepted = commit(view.worktrees["publish"], "copy")

    result = project.integrate(7, {"publish": accepted})
    assert [(o.status, o.published) for o in result.outcomes] == [("fast-forward", "local")]
    assert project.remote_log() == ["base"]
    named = project.view(8)
    edit(named.worktrees["publish"], "# named", name="named.md")
    result = project.integrate(8, {"publish": commit(named.worktrees["publish"], "named")}, publish={"publish"})
    assert result.outcomes[0].published == "pushed"


def test_refresh_brings_the_copy_to_the_combined_result(project):
    one, two = project.view(1), project.view(2)
    edit(one.worktrees["main"], "# one", name="one.py")
    edit(two.worktrees["main"], "# two", name="two.py")
    assert project.integrate(1, {"main": commit(one.worktrees["main"], "one")}).ok
    assert project.integrate(2, {"main": commit(two.worktrees["main"], "two")}).ok

    assert ms.refresh_view(two) == ["main"]
    assert (two.worktrees["main"] / "one.py").exists()
    assert ms.pending_changes(two) == {}


# --- records and rollout -----------------------------------------------------


def test_a_record_commit_takes_only_its_own_paths(project):
    """p3's devlog record staged whatever was dirty in the shared devlog."""
    devlog = project.root / "devlog"
    (devlog / "stray.md").write_text("draft\n")
    (devlog / "m1").mkdir()
    (devlog / "m1" / "report.md").write_text("report\n")

    assert ms.commit_paths(devlog, ["m1"], "[AUTO] record", publish=True, slug="demo",
                           missions_root=project.missions) == "pushed"
    assert git(devlog, "show", "--name-only", "--format=", "HEAD").splitlines() == ["m1/report.md"]
    assert (devlog / "stray.md").exists()
    assert ms.commit_paths(devlog, ["m1"], "[AUTO] record", publish=True, slug="demo",
                           missions_root=project.missions) == "nothing"


def test_set_aside_moves_unattributed_changes_onto_a_branch(project):
    main = project.root / "main"
    edit(main, "# nobody's")
    (main / "untracked.txt").write_text("x\n")

    commit_id = ms.set_aside(main, "autolab/set-aside-test", "set aside")

    assert git(main, "status", "--porcelain") == ""
    assert "# nobody's" in git(main, "show", "autolab/set-aside-test:wordcount.py")
    assert git(main, "show", "autolab/set-aside-test:untracked.txt") == "x"
    assert git(main, "rev-parse", "autolab/set-aside-test") == commit_id
    assert ms.set_aside(main, "autolab/set-aside-test", "again") is None
