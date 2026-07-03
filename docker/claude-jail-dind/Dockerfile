# claude-jail-dind: the production jail image PLUS a rootless Docker daemon. Used
# as the `claude` service by `claude-jail --dind` (via a compose overlay), so the
# jailed session has a working Docker to launch nested containers — the dogfooding
# environment, and where the e2e round-trip runs.
#
# Built FROM the production image, so `claude`, git, python3 and the jail's whole
# toolchain are present and identical. Two build rules (`--dind build` handles both):
#   1. build `claude-jail:latest` FIRST — this image derives from it.
#   2. build THIS image with `docker build`, not `docker compose build`: compose
#      can route through a buildx container-driver that doesn't read the local
#      image store, turning `FROM claude-jail:latest` into a failed *registry
#      pull* (`pull access denied ... claude-jail:latest`).
#
# NOT for production: it runs privileged and can spawn containers, which relaxes
# the isolation the normal jail enforces. Use only for testing/dev.
ARG BASE_IMAGE=claude-jail:latest
FROM ${BASE_IMAGE}

# Prod's final USER is `claude`; drop to root to install.
USER root

# Docker Engine + rootless extras from Docker's apt repo (prod is Debian
# bookworm). buildx/compose because claude-jail drives `docker compose` v2. The
# rest are the rootless daemon's runtime deps, each shelled out to during
# startup/networking on this minimal base — an absent one is an opaque
# `exit status 127`:
#   iproute2  `ip` (slirp4netns tap + dockerd bridge)   procps   `sysctl`
#   kmod      `modprobe` (bridge/netfilter modules)      iptables per-container NAT
#   uidmap    newuidmap/newgidmap (userns mapping)        fuse-overlayfs/slirp4netns
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin \
        docker-ce-rootless-extras \
        iproute2 procps kmod iptables uidmap fuse-overlayfs slirp4netns \
    && rm -rf /var/lib/apt/lists/*

# Subordinate uid/gid ranges the rootless daemon maps nested container users into
# (newuidmap/newgidmap read these). The range must NOT contain claude's own uid/gid
# — rootlesskit maps container uid 0 to claude's id and 1..65536 to this range, and
# newuidmap rejects the map (EINVAL, daemon won't start) if the two overlap. Start
# at the conventional 100000, but when a high (e.g. directory-provisioned) uid/gid
# baked into the base image lands inside that block, start just past it instead.
RUN uid="$(id -u claude)"; gid="$(id -g claude)"; \
    su=100000; if [ "$uid" -ge 100000 ]; then su=$((uid + 1)); fi; \
    sg=100000; if [ "$gid" -ge 100000 ]; then sg=$((gid + 1)); fi; \
    echo "claude:${su}:65536" > /etc/subuid \
    && echo "claude:${sg}:65536" > /etc/subgid

# Keep the rootless runtime + daemon storage under claude's home, so nothing here
# needs a root-owned /run/user/<uid> created at container start.
ENV XDG_RUNTIME_DIR=/home/claude/.dind \
    DOCKER_HOST=unix:///home/claude/.dind/docker.sock
RUN mkdir -p /home/claude/.dind /home/claude/.local/share/docker \
    && chmod 700 /home/claude/.dind \
    && chown -R claude /home/claude/.dind /home/claude/.local

COPY docker/claude-jail-dind/entrypoint.sh /usr/local/bin/claude-jail-dind-entrypoint
RUN chmod +x /usr/local/bin/claude-jail-dind-entrypoint

USER claude
WORKDIR /workspace
# Bring up the rootless daemon, then hand off to the prod entrypoint (the wrapper
# names claude-jail-entrypoint itself). Empty CMD so a bare `run` passes no extra
# args; any the launcher appends flow through to it.
ENTRYPOINT ["claude-jail-dind-entrypoint"]
CMD []
