#!/usr/bin/env bash
set -euo pipefail

# Build the claude invocation, appending the system prompt when one is provided.
# CLAUDE_APPEND_SYSTEM_PROMPT is passed through from the host by docker-compose.yml.
args=()
if [ -n "${CLAUDE_APPEND_SYSTEM_PROMPT:-}" ]; then
    args+=(--append-system-prompt "$CLAUDE_APPEND_SYSTEM_PROMPT")
fi

exec claude "${args[@]}" "$@"
