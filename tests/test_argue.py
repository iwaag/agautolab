"""autolab in an argue (`argue` p1): an argue invitation is answered in
place through the shared participation; any other mention is the callback
route it always was."""

from agautolab import argue, zulip_listener


class Client:
    def __init__(self):
        self.calls = []

    def whoami(self):
        return {"user_id": 11, "full_name": "autolab-agstudio1"}


def test_an_argue_invitation_is_answered_by_the_shared_participation(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(argue, "participate", lambda client, channel, topic, **kw: seen.update(kw, topic=topic) or [1])
    monkeypatch.setattr(argue, "role_context", lambda: "ROLE")
    assert argue.handle_argue_mention(Client(), "argue", "argue-fish") is True
    assert seen["topic"] == "argue-fish" and seen["role_context"] == "ROLE" and seen["run"] is argue.run_argue
    assert seen["spec"] is argue.SPEC


def test_a_mention_elsewhere_is_not_an_argue(monkeypatch):
    monkeypatch.setattr(argue, "participate", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ran")))
    assert argue.handle_argue_mention(Client(), "work-m1", "workrun-task1-m1") is False


def test_handle_mention_tries_the_argue_route_first(monkeypatch):
    calls = []
    monkeypatch.setattr(argue, "handle_argue_mention", lambda client, channel, topic: calls.append((channel, topic)) or True)
    zulip_listener.handle_mention(Client(), "argue", "argue-fish")
    assert calls == [("argue", "argue-fish")]


def test_the_argue_role_is_read_only_plus_autolab_and_git():
    from pathlib import Path
    from agag.agent_config import load_config, resolve_role

    config, overlay = load_config(argue.SPEC.agents_config, Path("/nonexistent"))
    grant = resolve_role(config, overlay, "argue", check_available=False).allowed_tools
    assert "Write" not in grant and "Bash(autolab:*)" in grant and "Bash(git:*)" in grant
