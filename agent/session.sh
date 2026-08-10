#!/usr/bin/env bash
# Run exactly one profile-selected mediator session. The Python role runner
# owns command/model resolution, protocol extraction, timeout, and metadata.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
state=".local/agent"
mkdir -p "$state/sessions"

if [[ ! -f "$state/MISSION.md" ]]; then
    echo "session.sh: no mission at $state/MISSION.md" >&2
    exit 2
fi

n=1
while [[ -e "$state/sessions/session-$(printf '%04d' "$n").run.json" || \
         -e "$state/sessions/session-$(printf '%04d' "$n").json" ]]; do
    n=$((n + 1))
done
stem="$state/sessions/session-$(printf '%04d' "$n")"

echo "session $n starting ($(date +%H:%M:%S)) -> $stem.run.json" >&2
PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m agautolab.role_run mediator \
    --cwd "$root" \
    --timeout 900 \
    --prompt-file agent/CHARTER.md \
    --transcript "$stem.agent.jsonl" \
    --record "$stem.run.json"
rc=$?

python3 - "$stem.run.json" "$rc" <<'PY' >&2
import json, sys
path, rc = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception as error:
    print(f"session summary: exit={rc}, record unreadable ({error})")
    raise SystemExit(0)
print(
    f"session summary: exit={rc}, outcome={d.get('outcome')}, "
    f"harness={d.get('harness')}, model={d.get('model')}, "
    f"turns={d.get('num_turns')}, cost=${d.get('cost_usd', 0) or 0:.2f}, "
    f"duration={d.get('duration_ms', 0) / 1000:.0f}s"
)
PY
exit "$rc"
