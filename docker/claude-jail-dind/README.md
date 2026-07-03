# claude-jail-dind — the jail, with Docker inside

`claude-jail --dind` runs a jail exactly like a normal `claude-jail run`, but the
`claude` service uses the **claude-jail-dind image** — the production image plus a
rootless Docker daemon — so the session has a working Docker to launch nested
containers. That's what lets the `claude` in a jail use `claude-jail` itself, and
it's where the full `run --review` → `review` → `apply` round-trip runs (which the
pure-Python unit suite, mocked docker, cannot cover).

It reuses the **real** `claude` service, it does not redefine one:
`compose.override.yml` is merged *over* `docker-compose.yml` by the launcher, so
the internal network, squid/dns `depends_on`, the proxy + review environment, the
`.claude` store binds, `working_dir`, and the mount override all still apply. Only
the image, `privileged`, `/dev/fuse`, and a persistent daemon-store volume are
layered on.

Production is untouched when `--dind` is absent: the flag is opt-in and, because
it runs the container **privileged**, is for dev/testing only.

## Usage

Just add `--dind` to any launcher command — no wrapper script:

```sh
./claude-jail --dind run             # build the image on demand, then run
./claude-jail --dind --review run    # a review session, with Docker inside
./claude-jail --dind build           # (re)build the dind image explicitly
./claude-jail --dind down            # tear it down
```

`--review` (like `--dind` itself) is a launcher flag, so it goes **before** the
command: argparse gathers everything from the command word onward as arguments for
the agent, so a trailing `--dind run --review` would forward `--review` to claude
rather than enable review isolation.

`--dind run` builds the image the first time and reuses it after; edit the image
and force a rebuild with `--dind build`. Run on a Docker-capable **host** — not
inside the production jail.

Unlike the base jail (whose same-config runs safely share squid/network/`jail-tmp`),
**concurrent `--dind` runs of the same config are not supported**: they resolve to
the one persistent `dind-data` volume, and a rootless daemon takes an exclusive
lock on its data-root, so the second session's daemon can't start (it times out in
the readiness wait). Run same-config `--dind` sessions sequentially. The persisted
image store also has no automatic GC beyond `--dind down -v`; `claude-jail prune`
reclaims orphaned `claude-jail-dind-*` volumes (and images) host-wide.

## What's here

| File | Role |
|---|---|
| `Dockerfile` | `FROM claude-jail:latest` (so `claude` + the full toolchain are identical to prod) + rootless Docker (engine, cli, buildx, compose, rootless-extras, iproute2, procps, kmod, iptables, uidmap, fuse-overlayfs, slirp4netns). Its `CMD` is empty; the entrypoint hands off to `claude-jail-entrypoint` after `dockerd` comes up. |
| `entrypoint.sh` | Starts the **rootless** `dockerd` as `claude`, waits for the socket, then `exec`s the prod entrypoint. |
| `compose.override.yml` | Overlay on the `claude` service: the dind image, `privileged`, `/dev/fuse`, and the `dind-data` volume. Merged by the launcher's `--dind`. |

The launcher owns the rest (no run.sh):

- `--dind run` inserts `compose.override.yml` as a second `-f`, after
  `docker-compose.yml` and before the mount override — so it merges onto the real
  service and review isolation still wins over it — and calls `ensure_dind_image`
  first so the image exists.
- `--dind build` builds the prod base (`docker compose build`) then the dind image
  (`docker build`, which resolves the local `FROM claude-jail…` a compose
  container-driver builder can't). It never adds the overlay, so compose is never
  asked to build that FROM.

## Egress for the nested daemon

Because the dind jail reuses the normal `claude` service, it's on the **internal
network behind squid** — so the nested daemon's registry pulls / build traffic go
through the jail's proxy. The first nested `run` builds the inner
`claude-jail`/squid/unbound images, so the jail's **egress ACL must allow** the
registries and package mirrors that build needs (or pre-seed them). `dind-data`
persists what it builds, so the cost is one-time. This is the trade-off of reusing
the locked-down service rather than a bare bridge-network container.

## Dogfooding & auth

Because this is the prod image, `claude` is present — so a `claude-jail --dind run`
session is Claude Code with Docker (and `claude-jail`) available, to develop
claude-jail from inside. Claude's credentials come through the normal `.claude`
store binds the service already has; nothing extra to mount.

## Notes & not-yet-validated points

Written without a Docker daemon to test against — treat as correct-by-construction
and shake out on the host. In rough order:

1. **Rootless uid remap (the sharp edge).** `claude-jail` builds the *inner*
   container's `claude` user at your host uid; under the rootless nested daemon
   that uid is remapped into the `100000+` subuid range, so files the inner
   container writes to its bind-mounted tree land as a subuid the outer `claude`
   **cannot remove** — breaking review's per-run gitdir wipe and spool cleanup.
   For a throwaway e2e repo, `"uid": 0, "gid": 0` in its `.claude-jail.json` maps
   the inner uid 0 back to `claude` and lines ownership up (`claude-jail` warns on
   uid 0; expected). Validate first.
2. **The dind image is built with `docker buildx build --builder default`, not
   `docker compose build`** (the launcher does this in `--dind build` / on-demand),
   after building the prod base. The pinned `default` (docker-driver) builder reads
   the daemon's local image store, so it resolves the local `FROM claude-jail<suffix>`;
   a bare `docker build` would use whatever builder the operator has *selected* (a
   `docker buildx create --use` container/remote driver can't see the local store),
   and `docker compose build` can route through such a builder too — either way the
   FROM becomes a registry pull that fails `pull access denied ... claude-jail<suffix>`.
3. **Daemon start.** If `docker version` never succeeds inside, read
   `~/.dind/dockerd.log` (`$XDG_RUNTIME_DIR/dockerd.log`; kept off the shared
   `/tmp` volume so concurrent sessions don't clobber it). Usual causes: a missing
   userspace tool (`exit status 127` —
   the rootless startup shells out to `ip`/iproute2, `sysctl`/procps,
   `modprobe`/kmod; all installed, but this is what a gap looks like), missing
   `/dev/fuse` (privileged should cover it), or a kernel without user-namespace
   support. The `child propagation to "rslave" is not supported` warning is benign.

## The automated round-trip test

`tests/test_e2e_review.py` drives `run --review` → `review` → `accept` → `apply`
through the launcher and asserts a scripted agent's commit lands in the real repo.
It **skips** unless both `CLAUDE_JAIL_E2E=1` and a reachable docker daemon are
present, so the normal suite stays docker-free. It needs a Docker daemon — run it
inside a `claude-jail --dind` session (where the nested rootless daemon provides
one). Its throwaway repo config uses `uid:0` so the nested container's writes
remap back to the outer `claude` and stay cleanable; on a bare Docker host that
same `uid:0` writes host-root-owned files a non-root runner can't clean up, so run
it there only as root (or, better, inside `--dind`). The deterministic agent works
via a small production seam: `entrypoint.sh` (the prod one) runs `claude` unless
`CLAUDE_JAIL_E2E=1` **and** `CLAUDE_JAIL_AGENT_CMD` are both present, in which case
it runs the latter. The launcher forwards both into the container only under
`CLAUDE_JAIL_E2E=1` and pins them off otherwise, and the entrypoint re-checks the
gate — so a stray `CLAUDE_JAIL_AGENT_CMD` can't swap the agent on a normal run.
