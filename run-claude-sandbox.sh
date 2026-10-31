#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
workspace="$root/workspaces/myopenclaw"
settings="$root/claude-sandbox-settings.json"
if [ ! -d "$workspace" ] || [ ! -f "$settings" ]; then
  echo 'Sandbox workspace or settings missing.' >&2
  exit 1
fi
cd "$workspace"
export OPENCLAW_STATE_DIR="$workspace/.sandbox-state"
export OPENCLAW_CONFIG_PATH="$workspace/.sandbox-state/openclaw.json"
exec claude --safe-mode --tools Bash --setting-sources user --settings "$settings" --strict-mcp-config "$@"
