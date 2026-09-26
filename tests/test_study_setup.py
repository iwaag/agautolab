"""A study laid out from its `ag-setup` block (sage p2 step 2).

Pinned: an empty workspace whose setup request names the study pattern
gets `main/` (plan unchanged, README, methods/, reports/INDEX.md) pushed to
its repository and a marker — and no `direction/` or `devlog/`, because
`init_project` then finds the marker; a second serving changes nothing; a
failure half way leaves no marker and is retried whole; the answer carries
the `study layout established` line once, again after a crash, never twice;
a block in the setup topic is found from another topic's serving; the
marker is rebuilt from the block.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agag.project import ESTABLISHED_RE, setup_block
from agautolab import project_init, study_setup

AUTOLAB, ARCHSAGE = 11, 24
PLAN = "# Aquaculture\n\nWhich feeds…"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def gitea(monkeypatch, tmp_path):
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    config = project_init.GiteaConfig("http://gitea.example", "token", "autodev")
    made = []

    def repo(cfg, name):
        bare = remotes / f"{name}.git"
        if not bare.exists():
            git("init", "--bare", "-b", "main", str(bare))
            made.append(name)
        return {}

    def clone(cfg, name, destination):
        if not destination.exists():
            git("clone", str(remotes / f"{name}.git"), str(destination))

    monkeypatch.setattr(study_setup, "ensure_gitea_repo", repo)
    monkeypatch.setattr(study_setup, "ensure_clone", clone)
    monkeypatch.setattr(study_setup, "load_gitea_config", lambda: config)
    monkeypatch.setattr(project_init, "load_gitea_config", lambda: config)
    monkeypatch.setattr(project_init, "ensure_gitea_repo", lambda cfg, name: made.append(f"init:{name}"))
    monkeypatch.setattr(project_init, "PROJECTS_ROOT", tmp_path / "projects")
    return remotes, made


class Client:
    def __init__(self, topics):
        self.topics = topics

    def topic_history(self, channel, topic, num_before=50):
        return list(self.topics.get((channel, topic), []))

    def message(self, message_id, strict=False):
        for rows in self.topics.values():
            for row in rows:
                if row["id"] == message_id:
                    return row
        return None


def request_history(pattern="study"):
    plan = {"id": 500, "sender_id": ARCHSAGE, "content": f"{PLAN}\n\n---\nOpened from conversation **#archsage-agstudio1 › study-aqua**."}
    block = setup_block({"pattern": pattern, "slug": "aqua", "channel": "pj-aqua",
                         "document": "researchplan-aqua #500", "knowledge": "main", "about": "fish farming"})
    request = {"id": 502, "sender_id": ARCHSAGE, "content": f"Please prepare…\n\n{block}"}
    topics = {("pj-aqua", "researchplan-aqua"): [plan],
              ("pj-aqua", "workplan-setup-aqua"): [{"id": 501, "sender_id": ARCHSAGE,
                                                    "content": "[selfnote][rootchat] archsage-agstudio1/study-aqua #9"},
                                                   request]}
    return Client(topics), topics[("pj-aqua", "workplan-setup-aqua")]


def test_an_empty_workspace_becomes_a_study_and_never_an_ordinary_project(gitea, tmp_path):
    remotes, made = gitea
    root = tmp_path / "projects"
    client, history = request_history()
    done = study_setup.prepare_pattern(client, "aqua", history, projects_root=root)
    assert done is not None and done.repository == "http://gitea.example/autodev/aqua.git"
    main = root / "aqua" / "main"
    assert (main / "RESEARCHPLAN.md").read_text() == PLAN + "\n"
    assert (main / "README.md").exists() and (main / "methods" / "README.md").exists()
    assert "| date | subject | file | status |" in (main / "reports" / "INDEX.md").read_text()
    assert (main / ".gitignore").read_text().strip() == ".local/"
    assert git("rev-parse", "--short=12", "main", cwd=remotes / "aqua.git") == done.revision
    marker = (root / "aqua" / "README_PROJECT.md").read_text()
    assert "study" in marker and "request #502" in marker and "autolab project establish aqua" in marker
    assert project_init.init_project("aqua") == project_init.PATTERN_MANAGED_RESULT
    assert not (root / "aqua" / "direction").exists() and not (root / "aqua" / "devlog").exists()
    assert made == ["aqua"]
    assert ESTABLISHED_RE.search(done.line()).group("revision") == done.revision
    assert study_setup.prepare_pattern(client, "aqua", history, projects_root=root) is None


def test_a_failure_half_way_leaves_no_marker_and_is_retried_whole(gitea, tmp_path, monkeypatch):
    root = tmp_path / "projects"
    client, history = request_history()

    def broken(*args, **kwargs):
        raise project_init.ProjectInitError("push refused")

    monkeypatch.setattr(study_setup, "commit_all_and_push", broken)
    with pytest.raises(project_init.ProjectInitError):
        study_setup.prepare_pattern(client, "aqua", history, projects_root=root)
    assert not (root / "aqua" / "README_PROJECT.md").exists()
    monkeypatch.setattr(study_setup, "commit_all_and_push", project_init.commit_all_and_push)
    done = study_setup.prepare_pattern(client, "aqua", history, projects_root=root)
    assert done is not None and (root / "aqua" / "README_PROJECT.md").exists()
    assert git("log", "--format=%s", "-1", cwd=root / "aqua" / "main").startswith("[AUTO] Establish study aqua")


def test_the_block_is_found_from_another_topic_and_other_patterns_are_left_alone(gitea, tmp_path):
    root = tmp_path / "projects"
    client, _ = request_history()
    assert study_setup.find_setup(client, "aqua", [])[1] == 502
    client, history = request_history(pattern="project")
    assert study_setup.prepare_pattern(client, "aqua", history, projects_root=root) is None
    assert study_setup.prepare_pattern(Client({}), "aqua", [], projects_root=root) is None


def test_the_established_line_is_said_once_and_again_after_a_crash(gitea, tmp_path):
    root = tmp_path / "projects"
    client, history = request_history()
    done = study_setup.prepare_pattern(client, "aqua", history, projects_root=root)
    first = study_setup.established_answer("aqua", "workplan-setup-aqua", history, AUTOLAB, done=done, projects_root=root)
    assert first == done.line()
    # A serving that established the study crashed before answering: the
    # next one finds no line of its own and says it from the repository.
    again = study_setup.established_answer("aqua", "workplan-setup-aqua", history, AUTOLAB, projects_root=root)
    assert again is not None and done.revision in again
    answered = history + [{"id": 510, "sender_id": AUTOLAB, "content": f"@**archsage** ready\n\n{first}"}]
    assert study_setup.established_answer("aqua", "workplan-setup-aqua", answered, AUTOLAB, projects_root=root) is None
    assert study_setup.established_answer("aqua", "workplan-research-1", history, AUTOLAB, projects_root=root) is None


def test_a_lost_marker_is_rebuilt_from_the_block(gitea, tmp_path):
    root = tmp_path / "projects"
    client, history = request_history()
    study_setup.prepare_pattern(client, "aqua", history, projects_root=root)
    (root / "aqua" / "README_PROJECT.md").unlink()
    done = study_setup.restore("aqua", client=client, projects_root=root)
    assert done.created == ("README_PROJECT.md",)
    assert "fish farming" in (root / "aqua" / "README_PROJECT.md").read_text()
