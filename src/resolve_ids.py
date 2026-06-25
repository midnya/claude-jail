"""Resolve the container user's numeric identity (uid/gid) for a claude-jail run.

resolve_user.py picks the config *namespace* (a name); this module picks the
numeric uid/gid the in-container `claude` user is built with. They default to the
host user's own ids — os.getuid()/os.getgid(), i.e. `id -u`/`id -g` — so files
the agent writes through the bind mounts (the project roots, the per-user
~/.claude-jail-<user> store) land owned by you, instead of by a mismatched
1000:1000. Either id can be pinned in .claude-jail.json with a top-level integer
"uid"/"gid", for the rare host whose ids the image's baked-in user must differ
from. Shared helpers live in jail_config.py.

The ids are baked into the image at *build* time (docker/claude/Dockerfile's
useradd), so they fold into the content-addressed image tag exactly as the extra
packages do (build_packages.image_suffix): a differing uid/gid mints its own
claude-jail-<digest> image rather than clobbering the shared one. The launcher
exports them as JAIL_UID/JAIL_GID, which docker-compose.yml forwards as the
USER_UID/USER_GID build args.

DEFAULT_UID/DEFAULT_GID mirror the Dockerfile's ARG defaults: an all-default run
(these ids, no extra packages) carries no uid/gid fold and so shares the base
`claude-jail` tag, keeping the common case on one image. Keep the two in sync.

resolve() returns the Ids. It calls die() (a message on stderr and a non-zero
exit) on a malformed config: a non-integer, a boolean, or a value outside the
kernel's uid/gid range. It also warns on stderr when the uid resolves to 0 (the
agent would run as root in the container); see resolve().
"""
import os
import sys
from collections import namedtuple

from jail_config import die

# Mirror docker/claude/Dockerfile's `ARG USER_UID=1000 / USER_GID=1000`. These
# ids carry no image-tag fold, so an otherwise-default run reuses the base
# `claude-jail` image; change one here only alongside the Dockerfile.
DEFAULT_UID = 1000
DEFAULT_GID = 1000

# The kernel's uid/gid range is 0 .. 2^32 - 2. The top value 2^32 - 1 is the
# reserved (uid_t)-1 "no such id" sentinel that chown/setuid reject, so it is
# excluded; a negative id is rejected by the lower bound.
_MAX_ID = 2 ** 32 - 2


class Ids(namedtuple("Ids", ["uid", "gid"])):
    """The container user's numeric identity: a (uid, gid) pair.

    is_default() is true for the Dockerfile's baked-in defaults, which the image
    tag treats as the no-fold base case; digest_key() is the content-key fragment
    folded into that tag when the ids are *not* default (build_packages.image_suffix).
    """
    __slots__ = ()

    def is_default(self) -> bool:
        return (self.uid, self.gid) == (DEFAULT_UID, DEFAULT_GID)

    def digest_key(self) -> str:
        # Labeled sections, like build_packages' per-manager key, so a uid value
        # can never collide with a same-spelled package token in the digest.
        return f"uid\n{self.uid}\ngid\n{self.gid}\n"


def _field(data: "dict", key: str, config_file: str, default: int) -> int:
    """The integer `key` from the config, or `default` when unset.

    Rejects a non-integer, a boolean (bool is an int subclass in Python, but
    true/false is not an id), and a value outside the kernel's uid/gid range.
    """
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        die(f"'{key}' in {config_file} must be an integer; got {value!r}")
    if not 0 <= value <= _MAX_ID:
        die(f"'{key}' in {config_file} must be between 0 and {_MAX_ID}; "
            f"got {value}")
    return value


def _host_default(getter_name: str, fallback: int) -> int:
    """The host's own uid/gid via os.getuid/os.getgid, or `fallback` on a
    platform that lacks them (Windows has neither)."""
    getter = getattr(os, getter_name, None)
    return getter() if getter is not None else fallback


def resolve(data: "dict", config_file: str,
            host: "Ids | None" = None) -> "Ids":
    """The container user's (uid, gid): config 'uid'/'gid', else the host's own.

    `host` defaults to the running host's ids (os.getuid/os.getgid); it is a
    parameter so tests can pin it. Each id falls back independently, so a config
    may pin only one and inherit the other from the host.

    Warns on stderr when the uid resolves to 0: the in-container 'claude' user is
    then built as root (the Dockerfile's `useradd -o -u 0`), so the root-owned
    read-only venv and the other guards that constrain an unprivileged agent no
    longer apply. Reached by running the launcher as root, or by pinning "uid": 0.
    """
    if host is None:
        host = Ids(_host_default("getuid", DEFAULT_UID),
                   _host_default("getgid", DEFAULT_GID))
    ids = Ids(_field(data, "uid", config_file, host.uid),
              _field(data, "gid", config_file, host.gid))
    if ids.uid == 0:
        print("claude-jail: uid 0 — the in-container agent runs as root, so the "
              "root-owned read-only venv no longer constrains it; pin a non-zero "
              "\"uid\" in the config to avoid this", file=sys.stderr)
    return ids
