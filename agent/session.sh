#!/usr/bin/env bash
# Run exactly one headless autolab-agent session (claude -p, never resumed).
# State lives in .local/agent/: MISSION.md (input), NOTES.md (agent-owned),
# done (agent-written, stops drive.sh), sessions/session-NNNN.json (full
# output + cost evidence).
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
state=".local/agent"
mkdir -p "$state/sessions"

if [[ ! -f "$state/MISSION.md" ]]; then
    echo "session.sh: no mission at $state/MISSION.md" >&2
    exit 2
fi

# Claude binary: env override > .local pointer file > PATH. Either of the
# first two may be a glob — the usual value points into a version-numbered
# editor-extension directory that goes stale on every update — so the newest
# match is resolved here, the same way agent/gateway.py's claude_bin() does.
# A plain path is used as written, so a wrong one fails loudly with the path.
resolve_bin() {
    local pattern="$1" newest="" match
    for match in $pattern; do
        [[ -e "$match" ]] || continue
        [[ -z "$newest" || "$match" -nt "$newest" ]] && newest="$match"
    done
    if [[ -n "$newest" ]]; then
        echo "$newest"
    elif [[ "$pattern" == *[*?[]* ]]; then
        echo "claude"   # a glob that matches nothing: fall through to PATH
    else
        echo "$pattern"
    fi
}

if [[ -n "${AUTOLAB_CLAUDE_BIN:-}" ]]; then
    bin="$(resolve_bin "$AUTOLAB_CLAUDE_BIN")"
elif [[ -f "$state/claude_bin" ]]; then
    bin="$(resolve_bin "$(<"$state/claude_bin")")"
else
    bin="claude"
fi

n=1
while [[ -e "$state/sessions/session-$(printf '%04d' "$n").json" ]]; do
    n=$((n + 1))
done
out="$state/sessions/session-$(printf '%04d' "$n").json"

# An explicit allowlist rather than --dangerously-skip-permissions: this node
# holds real credentials, and a headless session cannot answer a prompt. The
# list covers what the charter points at; a denial is a finding worth widening
# for, not misbehaviour to work around.
allowed="Read,Write,Edit,Glob,Grep,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch,NotebookEdit"
for c in git uv uvx curl wget node npm npx python3 pip jq autolab autolab-cagent \
         ls cat head tail wc sort uniq cut tr diff stat du df file \
         mkdir rmdir cp mv rm chmod touch ln readlink realpath basename dirname \
         find grep rg sed awk echo printf test true false date pwd cd which type \
         env export sleep kill pkill lsof ps ss nc ping open tar gzip unzip \
         yq python make bash sh xargs tee timeout nohup; do
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
