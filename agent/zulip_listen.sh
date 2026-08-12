#!/bin/sh
# Run the agautolab Zulip listener (credentials: .local/zulip.env).
set -eu
cd "$(dirname "$0")/.."
exec uv run python -m agautolab.zulip_listener
