#!/usr/bin/env bash
# Builds artio.plugin, the zip the Claude desktop app installs, from this directory's own files.
# Never zips __pycache__ or .DS_Store; the lock file (server.py.lock) IS included, deliberately, so
# `uv run --locked --script` inside the packaged plugin has the same pinned dependency set as the
# checkout it was built from.
set -euo pipefail
cd "$(dirname "$0")"

rm -f artio.plugin
zip -r -X artio.plugin .claude-plugin .mcp.json mcp_servers skills README.md \
  -x '*/__pycache__/*' '*.DS_Store'

echo "--- artio.plugin contents ---"
unzip -l artio.plugin
