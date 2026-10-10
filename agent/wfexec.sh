#!/bin/sh
# Run autolab as the executor of workflow runs (agautolab.wfexec; settings: .local/wfexec.toml).
set -eu
cd "$(dirname "$0")/.."
exec uv run python -m agautolab.wfexec
