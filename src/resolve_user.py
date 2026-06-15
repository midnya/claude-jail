#!/usr/bin/env python3
"""Resolve the jail user (config namespace) for a claude-jail run.

Usage: resolve_user.py <config-file> [override]

The user names a per-identity config namespace: Claude's config and credentials
persist on the host in ~/.claude-jail-<user>/ and ~/.claude-jail-<user>.json, so
different names keep separate logins. It comes from the --user flag (passed here
as <override>) or the "user" key in .claude-jail.json; the flag wins. An empty
override means the flag was absent. run.sh interpolates the result into those
host paths and into docker-compose's ${JAIL_USER}, so the value is restricted to
a bare word with no whitespace, quotes, or path separators.

Prints the resolved user to stdout. Exits non-zero with a message on stderr if
the config is malformed, the value is invalid, or neither source supplies one.
"""
import json
import re
import sys
from pathlib import Path

SETTING = "user"

# A value safe to interpolate into a host path (~/.claude-jail-<user>) and a
# docker-compose ${JAIL_USER} substitution: a bare word, no whitespace, quotes,
# path separators, or other metacharacters. Matches build_env.py's rule.
BARE_WORD = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]*\Z")


def die(msg: str) -> "None":
    sys.exit(f"Error: {msg}")


def read_config(config_file: "str | None") -> "dict":
    """Read and JSON-parse the jail config, returning {} when absent."""
    if not config_file or not Path(config_file).is_file():
        return {}
    try:
        data = json.loads(Path(config_file).read_text())
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {config_file}: {e}")
    if not isinstance(data, dict):
        die(f"{config_file} must contain a JSON object")
    return data


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
