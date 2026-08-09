#!/usr/bin/env bash
# Trivial driver: re-invoke session.sh until the agent says it is finished, or
# the session budget runs out. The agent says so by writing .local/agent/done;
# the file's content is never read here — it is the agent's own words, carried
# to the monitor as written. Continuity is the agent's job (MISSION + NOTES).
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
max="${1:-12}"
done_file=".local/agent/done"

for ((i = 1; i <= max; i++)); do
    agent/session.sh
    rc=$?
    ((rc != 0)) && echo "drive: session exited $rc (continuing; state is on disk)" >&2
    if [[ -e "$done_file" ]]; then
        echo "drive: after session $i: $done_file exists, stopping" >&2
        exit 0
    fi
    echo "drive: after session $i: still working" >&2
    sleep 5
done
echo "drive: session budget ($max) exhausted, agent still working" >&2
exit 10
