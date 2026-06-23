"""Host-side glue for the optional `--ide` bridge (the `ide-relay`/`ide-host`
side-containers).

The launcher's whole knowledge of this feature lives here — `claude-jail` only
ever calls `environment()`, `prepare()` and `cleanup()`. The bridge itself runs
entirely in docker (the `ide-relay` proxy and the `ide-host` loopback forwarder,
under the `ide` compose profile); this module just supplies their env, prepares
the lockfile directories the binds need, and clears the mirror on teardown — no
long-running host process. Deleting `src/ide/`, `docker/ide/` and
`tests/test_ide/` plus the small `--ide` call sites removes the feature; with the
`ide` profile inactive the default jail is unchanged — the claude service's
`CLAUDE_CODE_IDE_HOST_OVERRIDE` is a pass-through that compose omits entirely
when the launcher hasn't set it (a default run), so the CLI sees no key at all
rather than relying on an empty string reading as unset. The side-containers and
the squid bypass stay off.

The bridge lets the in-jail `/ide` reach a code editor on the host. The editor
advertises an MCP WebSocket server on host `127.0.0.1:<port>` via a lockfile in
`~/.claude/ide/`; `ide-relay` mirrors those lockfiles into the jail (rewriting
workspace paths) and proxies the WebSocket, and on Linux `ide-host` (sharing the
host network namespace) republishes the editor's loopback port on the docker
bridge gateway the relay reaches.
"""
import ipaddress
import os
import subprocess
import sys
from pathlib import Path

from jail_config import container_path, user_config_dir

# The hostname the in-jail CLI is pointed at (CLAUDE_CODE_IDE_HOST_OVERRIDE) —
# it must equal the relay's compose service name so jail-internal DNS resolves it.
SERVICE = "ide-relay"
# The compose profile that gates the bridge services, and the services it gates.
PROFILE = "ide"
SERVICES = (SERVICE, "ide-host")
# What the relay dials to reach the host editor. host.docker.internal resolves
# natively on Docker Desktop and, on Linux, via the relay's
# `extra_hosts: host.docker.internal:host-gateway` — the bridge gateway IP that
# ide-host binds. One name for both, so the Desktop branch is a later addition.
TARGET = "host.docker.internal"


def environment() -> "dict[str, str]":
    """The env the launcher merges in for an `--ide` run.

    CLAUDE_CODE_IDE_HOST_OVERRIDE redirects the in-jail CLI to the `ide-relay`
    service; it is the real Claude Code variable (set directly, not via a
    JAIL_-prefixed indirection) so compose can pass it through only when present
    and omit it entirely on a default run — an absent key is unambiguously unset
    for the CLI, where an empty string's meaning is undocumented. JAIL_IDE_TARGET
    tells the relay where the host editor is; JAIL_IDE_WORKSPACE is the container
    mount prefix (from jail_config.container_path) the relay rewrites paths
    against, so it can't drift from the actual bind layout; JAIL_IDE_GATEWAY is
    the specific bridge gateway IP ide-host binds (omitted when it can't be
    determined, so ide-host refuses to bind every interface); JAIL_IDE_NO_PROXY
    exempts the relay host from squid — only set here, so a default jail keeps no
    proxy bypass. COMPOSE_PROFILES is pinned by the caller (main), not here, so
    there is a single owner for it.
    """
    env = {
        "CLAUDE_CODE_IDE_HOST_OVERRIDE": SERVICE,
        "JAIL_IDE_TARGET": TARGET,
        "JAIL_IDE_WORKSPACE": container_path(""),
        "JAIL_IDE_NO_PROXY": f",{SERVICE}",
    }
    gateway = _host_gateway_ip()
    if gateway:
        env["JAIL_IDE_GATEWAY"] = gateway
    return env


def profile_args() -> "list[str]":
    """Compose global flags that activate the `ide` profile.

    `docker compose down` ignores services whose profile is inactive (it does not
    even treat them as orphans), so a plain teardown would leave ide-relay/
    ide-host running after the jail is gone. The launcher prepends these to every
    `down` — the explicit command and the post-run reap — so the profiled side
    containers are torn down whether or not this run enabled --ide.
    """
    return ["--profile", PROFILE]


def _unreachable_reason() -> "str | None":
    """Why the bridge can't reach the host editor this run, or None if it can.

    The single viability predicate: a Linux host (ide-host's network_mode: host
    only shares the real host network there) and a determined docker bridge
    gateway (ide-host needs it to bind, and forward.py exits at once without it).
    bridge_viable() and prepare()'s warning both derive from this, so the
    conditions live in one place and can't drift apart.
    """
    if sys.platform != "linux":
        return (f"--ide is Linux-only for now (this host is {sys.platform}); "
                f"the bridge may not connect.")
    if not os.environ.get("JAIL_IDE_GATEWAY"):
        return ("could not determine the docker bridge gateway; the IDE bridge "
                "will not reach the editor this run.")
    return None


def bridge_viable() -> bool:
    """Whether the bridge can actually reach the host editor this run.

    When False the launcher skips start_services() rather than bringing up an
    ide-host that immediately exits — which `up -d` still reports as success,
    leaving a dead container behind and the run looking healthy.
    """
    return _unreachable_reason() is None


def start_services(base_cmd: "list[str]") -> None:
    # `compose run claude` only starts claude + its depends_on, so the profiled
    # bridge services must be brought up explicitly. `--build` because plain
    # `claude-jail build` runs with no profile and so never (re)builds the ide
    # image, and `up` rebuilds only a *missing* image — without it, edits to
    # relay.py/forward.py would silently keep running the stale image (the layer
    # cache keeps the rebuild cheap when nothing changed). A failed bring-up
    # leaves the in-jail /ide pointed at a relay that isn't there, so say so
    # rather than letting the run look healthy.
    if subprocess.run([*base_cmd, "up", "-d", "--build",
                       *SERVICES]).returncode != 0:
        print("claude-jail: could not start the IDE bridge services; /ide will "
              "not connect this run.", file=sys.stderr)


def prepare(home: Path, jail_user: str) -> Path:
    """Prepare host state for an `--ide` run; return the mirror dir for cleanup().

    Ensures the source and mirror lockfile dirs exist (the relay's binds need
    them, with create_host_path off) and clears stale mirrors from a prior run.
    Surfaces the trust change, and warns when the bridge can't actually connect
    (non-Linux host, or an undetermined gateway) — never aborts the session.

    The mirror dir is shared per user+project, so two concurrent `--ide` runs of
    the same project contend on it: this clear (and cleanup()'s) can momentarily
    wipe a sibling session's mirrored lockfiles. That self-heals — the shared
    relay's sync_mirror restores a missing mirror on its next (<=1s) poll (it
    checks the mirror's existence, not just the source mtime) — so it is accepted
    rather than reference-counted like the side-container teardown.
    """
    source = _source_dir(home)
    mirror = _mirror_dir(home, jail_user)
    for d in (source, mirror):
        d.mkdir(parents=True, exist_ok=True)
    _clear_mirror(mirror)

    print("claude-jail: --ide enabled — the sandbox can reach your host editor "
          "(open files, diffs, notebook execution). Off by default; this run "
          "opted in.", file=sys.stderr)
    reason = _unreachable_reason()
    if reason:
        print(f"claude-jail: {reason}", file=sys.stderr)
    return mirror


def cleanup(mirror: Path) -> None:
    """Clear the mirrored lockfiles on teardown (a `run`).

    Mirrors that outlived the run would point a later `/ide` at a dead relay, so
    they are removed. The launcher only calls this on a teardown; an
    `up`/`start`/`watch` deliberately leaves the bridge (and its mirror) running
    alongside claude, like the other side services — wiping the mirror there would
    just blind the live in-jail CLI until the relay's next poll re-mirrors. The
    `ide-relay`/`ide-host` containers are reaped by cleanup_side_containers' `down`,
    which activates the `ide` profile (see profile_args) — a plain `down` skips
    them, since docker compose ignores services whose profile is inactive.
    """
    _clear_mirror(mirror)


def _source_dir(home: Path) -> Path:
    """The host's real IDE lockfile directory (where editors advertise)."""
    return home / ".claude" / "ide"


def _mirror_dir(home: Path, jail_user: str) -> Path:
    """The jail's IDE lockfile directory on the host.

    The whole per-user dir is bind-mounted to the container's /home/claude/.claude,
    so this `ide` subdir is exactly what the in-jail CLI scans at
    /home/claude/.claude/ide — the relay writes rewritten lockfiles here.
    """
    return user_config_dir(home, jail_user) / "ide"


def _host_gateway_ip() -> "str | None":
    """The IP `host.docker.internal:host-gateway` resolves to on this host.

    docker maps host-gateway to the default bridge's gateway, so the relay
    reaches the host there and ide-host must bind that same address. Returns None
    if it can't be determined (no docker0, odd daemon config) — environment()
    then omits it and ide-host refuses to bind, rather than exposing the LAN.

    A dual-stack bridge has one IPAM config per family, so the template prints
    one gateway per line; we take the first IPv4 (the jail network is IPv4-only)
    rather than concatenating them into a garbage address.

    Caveat: this reads the docker0 gateway, which is what host-gateway resolves
    to by default. A daemon configured with a custom `--host-gateway-ip` (or a
    bridge with multiple IPv4 pools) could make what the relay dials differ from
    what ide-host binds here; the bridge then silently won't connect. The common
    default (host-gateway == docker0 gateway) is assumed.
    """
    try:
        out = subprocess.run(
            ["docker", "network", "inspect", "bridge", "--format",
             "{{range .IPAM.Config}}{{println .Gateway}}{{end}}"],
            capture_output=True, text=True)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    for token in out.stdout.split():
        try:
            if ipaddress.ip_address(token).version == 4:
                return token
        except ValueError:
            continue
    return None


def _clear_mirror(mirror: Path) -> None:
    """Remove mirrored *.lock files; ignore a missing dir."""
    try:
        for lock in mirror.glob("*.lock"):
            lock.unlink(missing_ok=True)
    except OSError:
        pass
