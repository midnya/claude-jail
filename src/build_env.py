#!/usr/bin/env python3
"""Build the environment exports for a claude-jail run from .claude-jail.json.

Usage: build_env.py [config-file]

build_mounts.py owns the filesystem keys (read_only / hidden); this script owns
the rest of the config — the settings that shape how claude is launched. For
each recognised setting present it prints one NAME=value line to stdout, which
run.sh exports and docker-compose.yml forwards into the container. Prints
nothing when no such setting is present. Exits non-zero with a message on
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
import sys

from jail_config import BARE_WORD, die, read_config


def default_mode(data: "dict", config_file: "str | None") -> "str | None":
    """The claude --permission-mode value from default_mode, or None."""
    mode = data.get("default_mode")
    if mode is None:
        return None
    if not isinstance(mode, str) or not BARE_WORD.match(mode):
        die(f"'default_mode' in {config_file} must be a bare word "
            f"(letter, then letters/digits/'-'/'_'); got {mode!r}")
    return mode


def main() -> None:
    if len(sys.argv) > 2:
        sys.exit(f"Usage: {sys.argv[0]} [config-file]")
    config_file = sys.argv[1] if len(sys.argv) == 2 else None
    data = read_config(config_file)

    # (env var, value) for each recognised setting; None means "not set".
    exports = [
        ("CLAUDE_JAIL_PERMISSION_MODE", default_mode(data, config_file)),
    ]
    for name, value in exports:
        if value is not None:
            sys.stdout.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
