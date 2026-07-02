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

# git-review isolation: attach the throwaway .git to the live working tree
# before the session, then bundle the session's commits to the spool after it.
# Can't exec here — the export has to run once claude exits — so run it inline and
# propagate claude's exit status.
if [ -n "${JAIL_REVIEW:-}" ]; then
    # init must succeed before the session runs — a broken shadow .git would leave
    # git unusable — so fail with a clear message rather than a bare set -e abort.
    if ! jail-workspace init; then
        echo "claude-jail: git-review workspace init failed; aborting." >&2
        exit 1
    fi
    # Bundle the session's commits on the way out via an EXIT trap, not a
    # straight-line call, so the export still runs however claude ends. claude
    # runs in the *background* under `wait`, not as a synchronous foreground
    # command: bash defers a trap until a foreground command returns, and
    # `docker stop` signals PID 1 (this shell) only, never claude — so a
    # foreground claude would keep running while the queued TERM trap never fired,
    # and docker's follow-up SIGKILL (untrappable) would take the shell down with
    # the EXIT trap unrun, losing the session's work. Backgrounded, `wait` wakes
    # on the trapped signal; review_forward relays it to claude so it shuts down,
    # and the loop re-waits until claude has actually exited before export runs.
    review_exported=
    review_export() {
        [ -n "$review_exported" ] && return 0
        review_exported=1
        # export writes its own stderr diagnostic on a real failure, so `|| true`
        # keeps it from masking claude's status without hiding a problem.
        jail-workspace export || true
    }
    review_child=
    review_signalled=
    review_forward() {
        # Relay the stop signal to claude, then let the wait loop reap it, so the
        # export runs *after* claude exits rather than racing a still-live session.
        # The flag tells the loop this wait was interrupted by us (claude may still
        # be running) rather than by claude actually exiting.
        review_signalled=1
        [ -n "$review_child" ] && kill "-$1" "$review_child" 2>/dev/null || true
    }
    trap review_export EXIT
    trap 'review_forward TERM' TERM
    trap 'review_forward INT' INT
    set +e
    # A non-interactive shell with no job control redirects an *asynchronous*
    # command's stdin to /dev/null "in the absence of any explicit redirections"
    # — so a bare `claude ... &` would starve the interactive TUI (and any piped
    # input) of the tty/pipe `docker compose run` attaches to PID 1, feeding it an
    # immediate EOF. Save the real stdin to fd 3 and hand it to claude explicitly
    # (`<&3`) so fd 0 stays the container's stdin; `exec claude` above needs no
    # such care because exec keeps the shell's own descriptors.
    exec 3<&0
    claude "${args[@]}" "$@" <&3 &
    review_child=$!
    # `wait` returns >128 when a trapped signal interrupts it *before* claude has
    # exited; review_forward relayed the signal, so wait again until claude is
    # truly gone, keeping its real exit status. Distinguish the two via the
    # review_signalled flag rather than a post-wait `kill -0`, which races bash's
    # async reaper: if claude is reaped between `status=$?` and the check, kill -0
    # fails and we'd break carrying the >128 signal status instead of claude's
    # real one. When our trap interrupted the wait and claude has since exited, one
    # more `wait` returns its remembered exit status.
    while :; do
        wait "$review_child"
        status=$?
        [ -z "$review_signalled" ] && break   # claude exited on its own; done
        review_signalled=
        if kill -0 "$review_child" 2>/dev/null; then
            continue                          # still shutting down; wait again
        fi
        wait "$review_child" 2>/dev/null && status=0 || status=$?
        break
    done
    set -e
    exit "$status"
fi

exec claude "${args[@]}" "$@"
