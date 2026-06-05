#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <directory> <user> [docker compose args...]" >&2
    exit 1
fi

dir=$1
user=$2
shift 2

if [ ! -d "$dir" ]; then
    echo "Error: '$dir' is not a directory" >&2
    exit 1
fi

export JAIL_DIR=$(realpath "$dir")
export JAIL_ID=$(printf '%s' "${JAIL_DIR#/}" | sed 's|[^a-zA-Z0-9]\+|-|g')
export JAIL_USER=$user
script_dir=$(dirname "$(readlink -f "$0")")

prompt_file="$script_dir/system-prompt.md"
if [ -z "${CLAUDE_APPEND_SYSTEM_PROMPT:-}" ] && [ -f "$prompt_file" ]; then
    CLAUDE_APPEND_SYSTEM_PROMPT=$(cat "$prompt_file")
fi
export CLAUDE_APPEND_SYSTEM_PROMPT="${CLAUDE_APPEND_SYSTEM_PROMPT:-}"

claude_config_dir="$HOME/.claude-jail-$JAIL_USER"
claude_config_json="$HOME/.claude-jail-$JAIL_USER.json"
mkdir -p "$claude_config_dir"
[ -f "$claude_config_json" ] || echo '{}' > "$claude_config_json"

command -v python3 >/dev/null 2>&1 || {
    echo "Error: python3 is required to run $0" >&2
    exit 1
}
override=$(python3 "$script_dir/build-mounts.py" "$JAIL_DIR" "$JAIL_DIR/.claude-jail.json") || exit 1

if [ -n "$override" ]; then
    exec docker compose -f "$script_dir/docker-compose.yml" -f <(printf '%s\n' "$override") "$@"
else
    exec docker compose -f "$script_dir/docker-compose.yml" "$@"
fi
