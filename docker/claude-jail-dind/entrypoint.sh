#!/usr/bin/env bash
# Bring up a rootless Docker daemon, then hand off to the prod entrypoint. Runs as
# the unprivileged `claude` user inside a --privileged container: the daemon itself
# is rootless (a nested-container breakout lands as `claude`, not root), while
# privileged covers the mount/user-namespace syscalls rootlesskit performs when
# nested. The inner claude-jail stack images are built by the nested daemon itself
# on the first `claude-jail run` (cached in the dind-data volume). See README.md.
set -euo pipefail

# The rootless daemon refuses to run as root, so a uid-0 image (a `"uid": 0`
# config) can't work under --dind. Fail here with a clear message — not only in
# the launcher — so this image aborts loudly however it's started, rather than
# opaquely deep inside dockerd-rootless.sh. (uid 0 is only for the throwaway repo
# *inside* a --dind session; see README.md.)
if [ "$(id -u)" = 0 ]; then
    echo "claude-jail-dind: the rootless Docker daemon cannot run as root (uid 0);" \
         "use a non-zero uid for the --dind session." >&2
    exit 1
fi

mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# The daemon log. Under XDG_RUNTIME_DIR (per-container, on the image rootfs), NOT
# /tmp — /tmp is the persistent JAIL_ID-scoped `jail-tmp` volume the base compose
# mounts, shared across same-config sessions, so a log there would be clobbered by
# a concurrent session and outlive this run.
dockerd_log="$XDG_RUNTIME_DIR/dockerd.log"

# fuse-overlayfs so overlay-on-overlay works without a kernel-privileged mount;
# dockerd-rootless.sh forwards trailing args to dockerd. Log to a file so a
# failed start is inspectable below rather than lost to the background.
dockerd-rootless.sh --storage-driver=fuse-overlayfs >"$dockerd_log" 2>&1 &
# $! is the wrapper, which execs rootlesskit — the supervisor that runs the real
# dockerd inside its own user namespace. dockerd's pid lives in a namespace we
# can't see from here, but rootlesskit exits when its dockerd child does, so this
# is the best liveness proxy available; the narrow window where dockerd is gone
# but rootlesskit is still tearing down just falls through to the timeout below.
dockerd_pid=$!

# Wait up to ~30s for the daemon. Probe with `docker info`, not just `docker
# version`: info answers only once the daemon's storage/network init is done, so
# the handoff doesn't race the first nested build. Bail early if the rootless
# supervisor exits (e.g. a missing rootless dep → exit 127) instead of waiting the
# budget out. Each probe is `timeout`-bounded: `docker info` has no client-side
# deadline, so a daemon that opens its socket then wedges mid-init would otherwise
# block the probe forever and the `$SECONDS < deadline` check (only read at the
# loop head) would never be re-reached — the cap keeps the wall-clock budget real.
# Fail loudly with the log rather than let the first `docker` call die opaquely.
ready=
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
    if timeout 5 docker info >/dev/null 2>&1; then ready=1; break; fi
    kill -0 "$dockerd_pid" 2>/dev/null || break
    sleep 0.5
done
if [ -z "$ready" ]; then
    echo "claude-jail-dind: rootless dockerd did not become ready within 30s; log follows:" >&2
    cat "$dockerd_log" >&2 || true
    exit 1
fi

# Give nested builds/runs an egress route. A nested container gets its own network
# namespace, so the compose-DNS name `squid` in $HTTP_PROXY doesn't resolve inside
# it and the jail's egress proxy is unreachable by name — every nested `docker
# build`/`docker run` that touches the network then fails (e.g. apt while building
# the inner claude-jail stack). The daemon itself pulls base images fine (dockerd
# runs in THIS container's netns, where `squid` resolves), but that path can't
# cover a build's RUN steps. Resolve squid to its IP — reachable from a nested
# netns via the rootless NAT — and record it as the docker CLI's default proxy,
# which injects http(s)_proxy into every nested build and run. Best-effort: a
# failure here only costs nested egress, so warn rather than abort the daemon.
# ~/.docker is image-local (only .claude is bind-mounted), so no config.json
# pre-exists this fresh container to clobber.
if [ -n "${HTTP_PROXY:-}" ]; then
    proxy_authority=${HTTP_PROXY#*://}; proxy_authority=${proxy_authority%%/*}
    proxy_host=${proxy_authority%%:*}
    proxy_port=${proxy_authority##*:}
    [ "$proxy_port" = "$proxy_authority" ] && proxy_port=3128
    proxy_ip=$(getent ahostsv4 "$proxy_host" 2>/dev/null | awk '{print $1; exit}' || true)
    if [ -n "$proxy_ip" ]; then
        docker_config_dir=${DOCKER_CONFIG:-$HOME/.docker}
        mkdir -p "$docker_config_dir"
        cat > "$docker_config_dir/config.json" <<JSON
{
  "proxies": {
    "default": {
      "httpProxy": "http://$proxy_ip:$proxy_port",
      "httpsProxy": "http://$proxy_ip:$proxy_port",
      "noProxy": "localhost,127.0.0.1,::1"
    }
  }
}
JSON
    else
        echo "claude-jail-dind: could not resolve proxy host '$proxy_host';" \
             "nested builds will lack egress" >&2
    fi
fi

# Hand off to the production entrypoint (on PATH from the prod image), now with a
# working Docker underneath. Named explicitly rather than relying on the image CMD,
# so a stale CMD can't misdirect it and args after the service name still reach it.
exec claude-jail-entrypoint "$@"
