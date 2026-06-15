"""Resolve the jail user (config namespace) for a claude-jail run.

The user names a per-identity config namespace: Claude's config and credentials
persist on the host in ~/.claude-jail-<user>/ and ~/.claude-jail-<user>.json, so
different names keep separate logins. It comes from the --user flag (passed here
as <override>) or the "user" key in .claude-jail.json; the flag wins. A missing
<override> means the flag was absent — claude-jail rejects an explicit empty
--user before it gets here, so an empty override never reaches resolve().
claude-jail interpolates the result into those host paths and into
docker-compose's
${JAIL_USER}, so the value is restricted to a bare word with no whitespace,
quotes, or path separators. Shared helpers live in jail_config.py.

resolve() returns the resolved user. Calls die() (a message on stderr and a
non-zero exit) if the config is malformed, the value is invalid, or neither
source supplies one.
"""
from jail_config import BARE_WORD, die

SETTING = "user"


def resolve(data: "dict", override: "str | None",
            config_file: str) -> str:
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
