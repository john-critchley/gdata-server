#!/bin/sh
set -eu

cd "$HOME/py/gdata-server"

# Built-in tools are always available. Development plugins are opt-in because
# they execute Python in the MCP server process.
MISC_MCP_DEV_MODE="${MISC_MCP_DEV_MODE:-0}"
MISC_MCP_TOOL_PATH="$HOME/py/gdata-server"
if [ "$MISC_MCP_DEV_MODE" = 1 ] || [ "$MISC_MCP_DEV_MODE" = true ]; then
    MISC_MCP_TOOL_PATH="$MISC_MCP_TOOL_PATH:$HOME/py/mcp-tools"
    if [ -n "${MISC_MCP_EXTRA_TOOL_PATH:-}" ]; then
        MISC_MCP_TOOL_PATH="$MISC_MCP_TOOL_PATH:$MISC_MCP_EXTRA_TOOL_PATH"
    fi
fi
export MISC_MCP_TOOL_PATH
export MISC_MCP_DEV_MODE

exec python3 misc_mcp_server.py "$@"