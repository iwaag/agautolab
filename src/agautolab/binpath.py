"""Resolve an adapter command that may be a glob.

`.local/agent/claude_bin` holds a *glob* on purpose: the usual value is an
absolute path into a version-numbered editor-extension directory that goes
stale on every update. `agent/gateway.py` and `agent/session.sh` have resolved
it since turn1; `autolab` did not, so a job configured from that pointer — as
the charter tells the mediator to do — failed to launch with an infra-looking
`No such file or directory`.

Three implementations of the same nine lines is the honest cost of a shell
script, a stdlib-only service and a package that must each work alone.
"""

from __future__ import annotations

import glob
import os

GLOB_CHARS = "*?["


def resolve_command(command: str) -> str:
    """Newest existing match for a glob; anything else as written.

    A plain path is returned untouched, so a wrong one still fails loudly with
    the path in the message. A glob that matches nothing is also returned as
    written, for the same reason.
    """
    if not any(ch in command for ch in GLOB_CHARS):
        return command
    matches = [p for p in glob.glob(command) if os.path.isfile(p)]
    if not matches:
        return command
    return max(matches, key=lambda p: os.stat(p).st_mtime)
