#!/usr/bin/env bash
# Trivial driver: re-invoke session.sh until the agent's NOTES.md declares
# STATUS: complete/blocked, or the session budget runs out. No intelligence
# here on purpose — continuity is the agent's job (MISSION + NOTES + status).
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
max="${1:-12}"
notes=".local/agent/NOTES.md"

for ((i = 1; i <= max; i++)); do
    agent/session.sh
    rc=$?
    ((rc != 0)) && echo "drive: session exited $rc (continuing; state is on disk)" >&2
    # A NOTES.md older than MISSION.md is a leftover from a previous mission;
    # its STATUS must not stop the driver for the new one.
    if [[ -f "$notes" && ".local/agent/MISSION.md" -nt "$notes" ]]; then
        status="STATUS: (stale notes, predates mission)"
    else
        status="$(head -n1 "$notes" 2>/dev/null || echo 'STATUS: (no notes)')"
    fi
    echo "drive: after session $i: $status" >&2
    case "$status" in
        "STATUS: complete") exit 0 ;;
        "STATUS: blocked") exit 20 ;;
    esac
    sleep 5
done
echo "drive: session budget ($max) exhausted, agent still working" >&2
exit 10
