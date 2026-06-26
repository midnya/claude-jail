#!/usr/bin/env bash
set -euo pipefail

args=()

if [ -n "${CLAUDE_APPEND_SYSTEM_PROMPT:-}" ]; then
    args+=(--append-system-prompt "$CLAUDE_APPEND_SYSTEM_PROMPT")
fi

# With no claude args, resume the last session — unless the `new` command set
# CLAUDE_JAIL_NEW_SESSION, which asks for a fresh session instead.
if [ "$#" -eq 0 ] && [ -z "${CLAUDE_JAIL_NEW_SESSION:-}" ]; then
    project_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(pwd | sed 's/[^a-zA-Z0-9]/-/g')"
    if compgen -G "$project_dir/*.jsonl" > /dev/null; then
        args+=(--continue)
    fi
fi

# Apply the jail's default permission mode only when the caller didn't pass a
# --permission-mode of their own. Injecting it unconditionally would put two
# --permission-mode flags on the command line, leaving "command line wins" at
# the mercy of claude's flag-precedence; skipping it when the caller supplied
# one makes that guarantee explicit and avoids the duplicate.
if [ -n "${CLAUDE_JAIL_PERMISSION_MODE:-}" ]; then
    caller_set_mode=0
    for a in "$@"; do
        case "$a" in
            --permission-mode|--permission-mode=*) caller_set_mode=1; break ;;
        esac
    done
    [ "$caller_set_mode" = 0 ] && args+=(--permission-mode "$CLAUDE_JAIL_PERMISSION_MODE")
fi

exec claude "${args[@]}" "$@"
