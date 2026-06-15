#!/usr/bin/env bash
set -euo pipefail

args=()

if [ -n "${CLAUDE_APPEND_SYSTEM_PROMPT:-}" ]; then
    args+=(--append-system-prompt "$CLAUDE_APPEND_SYSTEM_PROMPT")
fi
if [ -n "${CLAUDE_JAIL_PERMISSION_MODE:-}" ]; then
    args+=(--permission-mode "$CLAUDE_JAIL_PERMISSION_MODE")
fi

exec claude "${args[@]}" "$@"
