#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [--user <name>] <directory> [docker compose args...]

  -u, --user <name>          Config namespace. Overrides the "user" key in the
                             jail's .claude-jail.json; one of the two must be
                             set, and the value must be non-empty. Claude's
                             config and credentials persist on the host in
                             ~/.claude-jail-<name>/ and ~/.claude-jail-<name>.json.
                             Use different names to keep separate
                             identities/logins.
  <directory>                Path to jail; bind-mounted read-write at
                             /workspace/<directory>.
  [docker compose args...]   Forwarded verbatim to docker compose.
EOF
}

# Leading options are run.sh's own; the first non-option is the jail directory,
# and everything after it is forwarded to docker compose untouched.
user_flag=""
dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        -u|--user)
            [ $# -ge 2 ] || { echo "Error: $1 requires a value" >&2; exit 1; }
            [ -n "$2" ] || { echo "Error: $1 requires a non-empty value" >&2; exit 1; }
            user_flag=$2
            shift 2
            ;;
        --user=*)
            user_flag=${1#--user=}
            [ -n "$user_flag" ] || { echo "Error: --user requires a non-empty value" >&2; exit 1; }
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
        *)
            break
            ;;
    esac
done
if [ $# -gt 0 ]; then
    dir=$1
    shift
fi

if [ -z "$dir" ]; then
    usage >&2
    exit 1
fi

if [ ! -d "$dir" ]; then
    echo "Error: '$dir' is not a directory" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || {
    echo "Error: python3 is required to run $0" >&2
    exit 1
}

export JAIL_DIR=$(realpath "$dir")
export JAIL_ID=$(printf '%s' "${JAIL_DIR#/}" | sed 's|[^a-zA-Z0-9]\+|-|g')
export COMPOSE_PROJECT_NAME=$(printf '%s' "$JAIL_ID" | tr '[:upper:]' '[:lower:]')
config_json="$JAIL_DIR/.claude-jail.json"

script_dir=$(dirname "$(readlink -f "$0")")

# Resolve and validate everything from the config FIRST, before touching any
# host state.
JAIL_USER=$(python3 "$script_dir/src/resolve_user.py" "$config_json" "$user_flag") || exit 1
export JAIL_USER

prompt_file="$script_dir/system-prompt.md"
[ -f "$prompt_file" ] || {
    echo "Error: base system prompt not found: $prompt_file" >&2
    exit 1
}
CLAUDE_APPEND_SYSTEM_PROMPT=$(
    python3 "$script_dir/src/build_prompt.py" "$JAIL_DIR" "$config_json" < "$prompt_file"
) || exit 1
export CLAUDE_APPEND_SYSTEM_PROMPT

env_exports=$(python3 "$script_dir/src/build_env.py" "$config_json") || exit 1
override=$(python3 "$script_dir/src/build_mounts.py" "$JAIL_DIR" "$config_json") || exit 1

# Config is valid; now apply the env exports and create host state.
if [ -n "$env_exports" ]; then
    while IFS= read -r assignment; do
        export "$assignment"
    done <<< "$env_exports"
fi

claude_config_dir="$HOME/.claude-jail-$JAIL_USER"
claude_config_json="$HOME/.claude-jail-$JAIL_USER.json"
mkdir -p "$claude_config_dir"
[ -f "$claude_config_json" ] || echo '{}' > "$claude_config_json"

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
