"""Resolve the base directory for a claude-jail run's per-user config store.

The per-user store — ~/.claude-jail-<user>/ (bound to the container's
/home/claude/.claude) and ~/.claude-jail-<user>.json (bound to
/home/claude/.claude.json) — lives under a base directory that defaults to the
host's $HOME. This module makes that base configurable: it comes from the
--claude-dir-base flag (passed here as <override>), the "claude_dir_base" key in
.claude-jail.json, or — when neither is set — $HOME. The flag wins over the
config, which wins over $HOME.

An explicit value (flag or config) is validated: it is rejected if malformed
(surrounding whitespace or control characters, which would corrupt the compose
interpolation), ~ is expanded, a relative path is resolved against the directory
containing the config file (the same anchor parse_roots uses for `roots`), and —
only when the run will launch a container (`must_exist`) — the result must be an
existing directory. A non-launching command (down/logs/ps) skips that existence
check, so it still works when the configured base has since gone missing, just
as the $HOME default is returned raw and unvalidated (mirroring the bare ${HOME}
the compose file used before this knob existed). The launcher's launch-time path
is the single fatal point for a missing or unset store location.

The resolved base is interpolated into the host bind sources and into
docker-compose's ${JAIL_CLAUDE_DIR_BASE}. Shared helpers live in jail_config.py.

resolve() returns the resolved base directory (possibly "" when defaulting to an
unset $HOME). Calls die() (a message on stderr and a non-zero exit) if an
explicit value is malformed or — when must_exist — not an existing directory.
"""
import os

from jail_config import config_dir, die

SETTING = "claude_dir_base"


def resolve(data: "dict", override: "str | None", config_file: str,
            env: "dict | None" = None, must_exist: bool = True) -> str:
    """Pick the store base: --claude-dir-base, else config 'claude_dir_base',
    else $HOME.

    `must_exist` gates the existing-directory check: a launch mounts the store
    and so requires it, but a non-launching command (down/logs/ps) must still
    work when the configured base has since gone missing — matching the $HOME
    default, which is never existence-checked here.
    """
    env = os.environ if env is None else env
    configured = data.get(SETTING)
    if override is not None:
        value, source = override, "--claude-dir-base"
    elif configured is not None:
        value, source = configured, f"'{SETTING}' in {config_file}"
    else:
        # The $HOME default is returned raw and unvalidated (like the bare
        # ${HOME} compose used before): an unset $HOME yields "", which a
        # non-launching command tolerates and a launch turns into a fatal error.
        return env.get("HOME") or ""

    if not isinstance(value, str) or not value:
        die(f"{source} must be a non-empty path; got {value!r}")
    # The value is interpolated verbatim into the compose bind sources, so reject
    # surrounding whitespace (a leading space would also slip past the isabs check
    # below and be silently re-anchored to the config dir) and any control
    # character. An interior space is fine — a real directory name may hold one.
    if value != value.strip() or any(ord(c) < 0x20 for c in value):
        die(f"{source} must be a path without surrounding whitespace or control "
            f"characters; got {value!r}")
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        # A relative path is resolved against the config file's directory, the
        # same anchor parse_roots uses for a relative `roots` entry.
        path = os.path.join(config_dir(config_file), path)
    path = os.path.abspath(path)
    if must_exist and not os.path.isdir(path):
        die(f"{source} is not an existing directory: {path}")
    return path
