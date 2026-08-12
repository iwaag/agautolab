"""Witness that a mediator session actually consumed MISSION.md.

S5 died with two mediator sessions answering "what should I build?" without
ever reading the dispatched mission — invisible until the whole session
budget was gone. This module makes mission consumption observable from the
session's own evidence: the mission's content appearing anywhere in the
session transcript proves it entered the model's context (the standing
prompt only names the file, never its content).

Transcript shapes handled:

- opencode event streams (``run --format json``): tool inputs/outputs are in
  the events, so a Read/cat of MISSION.md leaves the content in the file.
- claude_code result documents (``-p --output-format json``): the file holds
  only the final reply, so the witness follows ``session_id`` to the harness's
  own full session log under ``~/.claude/projects/<cwd-slug>/``. When that log
  is unavailable the verdict is indeterminate (None), never a false failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CLAUDE_PROJECTS_ENV = "AGAUTOLAB_CLAUDE_PROJECTS_DIR"
MIN_MARKER_LEN = 16
UNCONSUMED_EXIT = 3
FAILURE_TEXT = "mediator session ended without the mission's content entering its context"

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def markers(mission_text: str) -> list[str]:
    """Distinctive, whitespace-normalized lines of the mission text."""
    lines = [_norm(line) for line in mission_text.splitlines()]
    out = [line for line in lines if len(line) >= MIN_MARKER_LEN]
    if not out:
        whole = _norm(mission_text)
        if whole:
            out = [whole]
    return out


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _haystack(text: str) -> str:
    """Every string value in a JSONL transcript (plus raw non-JSON lines),
    whitespace-normalized into one searchable blob."""
    parts = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                parts.extend(_strings(json.loads(stripped)))
                continue
            except ValueError:
                pass
        parts.append(line)
    return _norm("\n".join(parts))


def _cwd_slug(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", str(Path(cwd).resolve()))


def _harness_session_file(cwd: Path, session_id: str) -> Path | None:
    base = os.environ.get(CLAUDE_PROJECTS_ENV)
    base_dir = Path(base) if base else Path.home() / ".claude" / "projects"
    path = base_dir / _cwd_slug(cwd) / f"{session_id}.jsonl"
    return path if path.is_file() else None


def witness(mission_path: Path, transcript_path: Path, cwd: Path) -> dict:
    """Return {"consumed": True|False|None, "source": str|None}.

    None means the evidence cannot answer either way; only an explicit False
    is a failed session.
    """
    verdict = {"consumed": None, "source": None}
    try:
        mission = Path(mission_path).read_text(encoding="utf-8")
    except OSError:
        return verdict
    marks = markers(mission)
    if not marks:
        return verdict
    try:
        text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return verdict
    result_doc = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "type" not in parsed:
            result_doc = parsed
    except ValueError:
        pass
    if result_doc is not None:
        # claude_code result document: final reply only. Follow the harness's
        # own session log; without it the transcript cannot prove absence.
        session_id = result_doc.get("session_id")
        session_file = (
            _harness_session_file(cwd, session_id)
            if isinstance(session_id, str) and session_id
            else None
        )
        if session_file is None:
            return {"consumed": None, "source": "unavailable"}
        hay = _haystack(session_file.read_text(encoding="utf-8", errors="replace"))
        return {
            "consumed": any(mark in hay for mark in marks),
            "source": "harness_session",
        }
    hay = _haystack(text)
    return {"consumed": any(mark in hay for mark in marks), "source": "transcript"}


def annotate_record(record_path: Path, verdict: dict) -> None:
    """Merge the verdict into a session run record; an explicit False also
    turns the record's outcome into a failure the accounting can see."""
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {"schema": "ag.agent-run.v1"}
    record["mission_consumed"] = verdict["consumed"]
    record["mission_witness_source"] = verdict["source"]
    if verdict["consumed"] is False:
        record["outcome"] = "failed"
        record["failure"] = FAILURE_TEXT
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    verdict = witness(args.mission, args.transcript, args.cwd)
    if args.record:
        annotate_record(args.record, verdict)
    print(
        f"mission witness: consumed={verdict['consumed']} source={verdict['source']}",
        file=sys.stderr,
    )
    raise SystemExit(UNCONSUMED_EXIT if verdict["consumed"] is False else 0)


if __name__ == "__main__":
    main()
