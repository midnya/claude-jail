"""Resolve the jail user (config namespace) for a claude-jail run.

The user names a per-identity config namespace: Claude's config and credentials
persist on the host in ~/.claude-jail-<user>/ and ~/.claude-jail-<user>.json, so
different names keep separate logins. It comes from the --user flag (passed here
as <override>), the "user" key in .claude-jail.json, or — when neither is set —
the host's $USER (then $USERNAME) environment variable, with a warning on
stderr. The flag wins over the config, which wins over the environment. A
missing <override> means the flag was absent — claude-jail rejects an explicit
empty --user before it gets here, so an empty override never reaches resolve().
claude-jail interpolates the result into those host paths and into
docker-compose's
${JAIL_USER}, so the value is restricted to a bare word with no whitespace,
quotes, or path separators — the environment fallback is bare-word-validated
just like the explicit sources. Shared helpers live in jail_config.py.

resolve() returns the resolved user. Calls die() (a message on stderr and a
non-zero exit) if the config is malformed, the value is invalid, or no source
supplies one.
"""
import os
import sys

from jail_config import BARE_WORD, die

SETTING = "user"

# Host environment variables consulted, in order, when no user is set
# explicitly. $USER is the POSIX convention; $USERNAME is its Windows-ish twin.
ENV_VARS = ("USER", "USERNAME")


def _env_default(env: "dict") -> "tuple[str | None, str]":
    """The first set, non-empty $USER/$USERNAME and its '$NAME' source label.

    Returns (None, "") when neither is available ("available" = set and
    non-empty, so an empty $USER falls through to $USERNAME).
    """
    for name in ENV_VARS:
        value = env.get(name)
        if value:
            return value, f"${name}"
    return None, ""


def resolve(data: "dict", override: "str | None", config_file: str,
            env: "dict | None" = None) -> str:
    """Pick the user: --user flag, else config 'user', else $USER/$USERNAME."""
    env = os.environ if env is None else env
    from_env = False
    if override is not None:
        value, source = override, "--user"
    else:
        value, source = data.get(SETTING), f"'{SETTING}' in {config_file}"
        if value is None:
            value, source = _env_default(env)
            from_env = value is not None
    if value is None:
        env_hint = "/".join(f"${name}" for name in ENV_VARS)
        die("no user set: pass --user <name>, add a \"user\" key to "
            f"{config_file}, or set {env_hint}")
    if not isinstance(value, str) or not BARE_WORD.match(value):
        die(f"{source} must be a bare word (letter, then "
            f"letters/digits/'-'/'_'); got {value!r}")
    if from_env:
        print(f"claude-jail: no user set; defaulting to {source}={value!r}",
              file=sys.stderr)
    return value
