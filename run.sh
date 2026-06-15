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

export COMPOSE_PROJECT_NAME=$(printf '%s' "$JAIL_ID" | tr '[:upper:]' '[:lower:]')

claude_config_dir="$HOME/.claude-jail-$JAIL_USER"
claude_config_json="$HOME/.claude-jail-$JAIL_USER.json"
mkdir -p "$claude_config_dir"
[ -f "$claude_config_json" ] || echo '{}' > "$claude_config_json"

command -v python3 >/dev/null 2>&1 || {
    echo "Error: python3 is required to run $0" >&2
    exit 1
}

config_json="$JAIL_DIR/.claude-jail.json"

prompt_file="$script_dir/system-prompt.md"
[ -f "$prompt_file" ] || prompt_file=/dev/null
CLAUDE_APPEND_SYSTEM_PROMPT=$(
    python3 "$script_dir/src/build_prompt.py" "$JAIL_DIR" "$config_json" < "$prompt_file"
) || exit 1
export CLAUDE_APPEND_SYSTEM_PROMPT

env_exports=$(python3 "$script_dir/src/build_env.py" "$config_json") || exit 1
if [ -n "$env_exports" ]; then
    while IFS= read -r assignment; do
        export "$assignment"
    done <<< "$env_exports"
fi

override=$(python3 "$script_dir/src/build_mounts.py" "$JAIL_DIR" "$config_json") || exit 1

base=(docker compose -f "$script_dir/docker-compose.yml")

status=0
if [ -n "$override" ]; then
    "${base[@]}" -f <(printf '%s\n' "$override") "$@" || status=$?
else
    "${base[@]}" "$@" || status=$?
fi

# Side containers cleanup if this was the last instance at the time of exit.
if [ "${1:-}" = run ]; then
    others=$(docker ps -q \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
        --filter "label=com.docker.compose.service=claude-jail") || others=keep
    [ -z "$others" ] && { "${base[@]}" down --timeout 0 || true; }
fi

exit "$status"
