"""autolab in an argue (`argue` p1): a contribution when named.

autolab's mention route serves the task that delegated (`handle_mention` in
`zulip_listener`). An argue invitation (`agag.argue`) is the one other
thing a mention means: autolab, named in `#argue`, answers there from what
it knows about projects and how development is done here, and names nobody.
The run is autolab's own `role_run` (its permission bypass under claude_code,
its record), in a workspace under `.local/topics/`.
"""

from __future__ import annotations

from pathlib import Path

from agag.argue import ROLE as ARGUE_ROLE, is_argue_topic, participate, role_context_path
from agag.topics import next_record_path
from agag.zulip import ZulipClient, log

from .instance import AGAUTOLAB_ROOT, SPEC
from .project_init import PROJECTS_ROOT
from .role_run import run_role

ARGUE_TIMEOUT_SECONDS = 600

__all__ = ["ARGUE_ROLE", "ARGUE_TIMEOUT_SECONDS", "handle_argue_mention", "role_context", "run_argue"]


def role_context() -> str:
    """The role's own context, with the projects root named at runtime so
    the committed file carries no path."""
    text = role_context_path(AGAUTOLAB_ROOT, ARGUE_ROLE).read_text(encoding="utf-8")
    return f"{text.rstrip()}\n\nThe project workspaces on this host are under {PROJECTS_ROOT} (read-only for you here)."


def run_argue(prompt: str, cwd: Path, invitation) -> str:
    output, _, exit_code = run_role(
        ARGUE_ROLE, prompt, cwd=cwd, timeout=ARGUE_TIMEOUT_SECONDS,
        record=next_record_path(SPEC.records_root / ARGUE_ROLE), transcript=cwd / "transcript.jsonl",
        stream=True, extra_meta={"invitation": invitation.message_id},
    )
    if exit_code != 0:
        raise RuntimeError(f"{ARGUE_ROLE} run exited {exit_code}: {output.strip()[:500]}")
    return output.strip()


def handle_argue_mention(client: ZulipClient, channel: str, topic: str) -> bool:
    """Answer an argue invitation; False when this mention is not one."""
    if not is_argue_topic(channel, topic):
        return False
    from agag.agent import is_ack

    log(f"argue invitation in {channel!r}/{topic!r}")
    participate(client, channel, topic, spec=SPEC, role_context=role_context(), run=run_argue,
                drop=is_ack, log=log)
    return True
