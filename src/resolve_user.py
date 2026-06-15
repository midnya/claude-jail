#!/usr/bin/env python3
"""Resolve the jail user (config namespace) for a claude-jail run.

Usage: resolve_user.py <config-file> [override]

The user names a per-identity config namespace: Claude's config and credentials
persist on the host in ~/.claude-jail-<user>/ and ~/.claude-jail-<user>.json, so
different names keep separate logins. It comes from the --user flag (passed here
as <override>) or the "user" key in .claude-jail.json; the flag wins. A missing
<override> means the flag was absent — run.sh rejects an explicit empty --user
before it gets here, so an empty override never reaches this script. run.sh
interpolates the result into those host paths and into docker-compose's
${JAIL_USER}, so the value is restricted to a bare word with no whitespace,
quotes, or path separators. Shared helpers live in jail_config.py.

Prints the resolved user to stdout. Exits non-zero with a message on stderr if
the config is malformed, the value is invalid, or neither source supplies one.
"""
import sys

from jail_config import BARE_WORD, die, read_config

SETTING = "user"


def resolve(data: "dict", override: "str | None",
            config_file: "str | None") -> str:
    """Pick the user: the --user flag wins, else the config 'user' key."""
    if override is not None:
        value, source = override, "--user"
    else:
        value, source = data.get(SETTING), f"'{SETTING}' in {config_file}"
    if value is None:
        die("no user set: pass --user <name> or add a \"user\" key to "
            f"{config_file}")
    if not isinstance(value, str) or not BARE_WORD.match(value):
        die(f"{source} must be a bare word (letter, then "
            f"letters/digits/'-'/'_'); got {value!r}")
    return value


def main() -> None:
    if not 2 <= len(sys.argv) <= 3:
        sys.exit(f"Usage: {sys.argv[0]} <config-file> [override]")
    config_file = sys.argv[1]
    override = sys.argv[2] if len(sys.argv) == 3 and sys.argv[2] else None
    data = read_config(config_file)
    sys.stdout.write(resolve(data, override, config_file))


if __name__ == "__main__":
    main()
