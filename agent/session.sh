#!/usr/bin/env bash
# Run exactly one headless autolab-agent session (claude -p, never resumed).
# State lives in .local/agent/: MISSION.md (input), NOTES.md (agent-owned),
# sessions/session-NNNN.json (full output + cost evidence).
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
state=".local/agent"
mkdir -p "$state/sessions"

if [[ ! -f "$state/MISSION.md" ]]; then
    echo "session.sh: no mission at $state/MISSION.md" >&2
    exit 2
fi

# Claude binary: env override > .local pointer file > PATH.
if [[ -n "${AUTOLAB_CLAUDE_BIN:-}" ]]; then
    bin="$AUTOLAB_CLAUDE_BIN"
elif [[ -f "$state/claude_bin" ]]; then
    bin="$(<"$state/claude_bin")"
else
    bin="claude"
fi

n=1
while [[ -e "$state/sessions/session-$(printf '%04d' "$n").json" ]]; do
    n=$((n + 1))
done
out="$state/sessions/session-$(printf '%04d' "$n").json"

# No --dangerously-skip-permissions on agstudio (standing rule): the agent
# gets an explicit tool allowlist instead. Judgment lives in CHARTER.md.
allowed="Read,Write,Edit,Glob,Grep,TodoWrite,BashOutput,KillShell"
for c in git uv curl node python3 npx ls cat head tail wc mkdir cp mv chmod \
         find grep sed echo printf test date pwd which sleep kill lsof; do
    allowed+=",Bash($c:*)"
done

echo "session $n starting ($(date +%H:%M:%S)) -> $out" >&2
"$bin" -p --output-format json \
    --model "${AUTOLAB_AGENT_MODEL:-claude-sonnet-5}" \
    --allowedTools "$allowed" \
    <agent/CHARTER.md >"$out"
rc=$?

python3 - "$out" "$rc" <<'PY' >&2
import json, sys
path, rc = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception as e:
    print(f"session summary: exit={rc}, output not JSON ({e})")
    sys.exit(0)
print(
    f"session summary: exit={rc}, is_error={d.get('is_error')}, "
    f"turns={d.get('num_turns')}, cost=${d.get('total_cost_usd', 0):.2f}, "
    f"duration={d.get('duration_ms', 0) / 1000:.0f}s"
)
PY
exit "$rc"
