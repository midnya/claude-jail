"""Build the environment for a claude-jail run from .claude-jail.json.

build_mounts.py owns the filesystem keys (read_only / hidden); this module owns
the rest of the config — the settings that shape how claude is launched.
exports() returns the {NAME: value} mapping for the recognised settings (empty
when none is set), which claude-jail sets in the environment and
docker-compose.yml forwards into the container. Calls die() with a message on
stderr if the config is malformed or a value is invalid. Shared
parsing/validation helpers live in jail_config.py.

Settings:
  default_mode -> CLAUDE_JAIL_PERMISSION_MODE
      The permission mode claude starts in, applied by the entrypoint as
      `claude --permission-mode`. The set of valid modes belongs to claude,
      which rejects an unknown one at startup and lists the choices, so we only
      check it is a bare word — both to keep this decoupled from claude's list
      and so the value is safe to export and interpolate without quoting.
"""
from jail_config import BARE_WORD, die


def default_mode(data: "dict", config_file: str) -> "str | None":
    """The claude --permission-mode value from default_mode, or None."""
    mode = data.get("default_mode")
    if mode is None:
        return None
    if not isinstance(mode, str) or not BARE_WORD.match(mode):
        die(f"'default_mode' in {config_file} must be a bare word "
            f"(letter, then letters/digits/'-'/'_'); got {mode!r}")
    return mode


def exports(data: "dict", config_file: str) -> "dict[str, str]":
    """The {NAME: value} env mapping for every recognised setting that is set."""
    # (env var, value) for each recognised setting; None means "not set".
    settings = [
        ("CLAUDE_JAIL_PERMISSION_MODE", default_mode(data, config_file)),
    ]
    return {name: value for name, value in settings if value is not None}
