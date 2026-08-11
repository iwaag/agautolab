"""CLI entry point for gateway, director, mediator, and summarizer runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agag.agent_config import AgentConfigError
from agag.harness import run_harness, write_run_record

from .agent_settings import PROJECT_ROOT, resolve_project_role

ROLE_ALLOWED_TOOLS = {
    "front": "Read,Glob,Grep,Bash(cd:*),Bash(uv run python -m agautolab.role_run director:*)",
    "director": "Read,Glob,Grep",
    "summarizer": "Read,Glob,Grep",
    "mediator": (
        "Read,Write,Edit,Glob,Grep,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch,NotebookEdit,"
        "Bash(git:*),Bash(uv:*),Bash(uvx:*),Bash(curl:*),Bash(wget:*),Bash(node:*),"
        "Bash(npm:*),Bash(npx:*),Bash(python3:*),Bash(pip:*),Bash(jq:*),Bash(autolab:*),"
        "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*),Bash(sort:*),"
        "Bash(find:*),Bash(rg:*),Bash(sed:*),Bash(awk:*),Bash(mkdir:*),Bash(cp:*),"
        "Bash(mv:*),Bash(rm:*),Bash(chmod:*),Bash(touch:*),Bash(date:*),Bash(pwd:*),"
        "Bash(cd:*),Bash(which:*),Bash(env:*),Bash(sleep:*),Bash(kill:*),Bash(ps:*),"
        "Bash(open:*),Bash(tar:*),Bash(make:*),Bash(bash:*),Bash(sh:*)"
    ),
}


def _opencode_config(role: str) -> Path:
    if role in {"director", "summarizer"}:
        return PROJECT_ROOT / "agent" / "opencode-readonly.json"
    return PROJECT_ROOT / "agent" / f"opencode-{role}.json"


def run_role(role: str, prompt: str, *, cwd: Path, timeout: float,
             profile: str | None = None, transcript: Path | None = None,
             record: Path | None = None) -> tuple[str, dict, int]:
    agent = resolve_project_role(role, profile_override=profile)
    result = run_harness(
        agent,
        prompt,
        cwd=cwd,
        timeout=timeout,
        allowed_tools=ROLE_ALLOWED_TOOLS.get(role),
        opencode_config=_opencode_config(role) if agent.harness == "opencode" else None,
        transcript_path=transcript,
    )
    run_record = {"schema": "ag.agent-run.v1", **result.meta}
    if record:
        write_run_record(record, request_id=record.stem, meta=result.meta)
        run_record = json.loads(record.read_text(encoding="utf-8"))
    return result.output, run_record, result.exit_code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=sorted(ROLE_ALLOWED_TOOLS))
    parser.add_argument("--profile")
    parser.add_argument("--cwd", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--timeout", type=float, default=300)
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt")
    prompt_source.add_argument("--prompt-file", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    if args.prompt is not None:
        prompt = args.prompt
    elif args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    else:
        prompt = sys.stdin.read()
    try:
        output, _record, code = run_role(args.role, prompt, cwd=args.cwd,
                                         timeout=args.timeout, profile=args.profile,
                                         transcript=args.transcript, record=args.record)
    except AgentConfigError as error:
        if args.record:
            failed = {"schema": "ag.agent-run.v1", "role": args.role,
                      "outcome": "failed", "failure": str(error)}
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(failed, indent=2) + "\n", encoding="utf-8")
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    print(output)
    raise SystemExit(0 if code == 0 else 1)


if __name__ == "__main__":
    main()
