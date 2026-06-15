#!/usr/bin/env python3
"""Merge the jail system prompt with an optional project-supplied one.

Usage: build_prompt.py <jail-dir> [config-file]

build_mounts.py owns the filesystem keys and build_env.py the bare-word
settings; this script owns the one free-text setting, system_prompt, since a
multi-line prompt cannot ride build_env.py's NAME=value line protocol.

Reads the base jail prompt from stdin and looks for `system_prompt` in
.claude-jail.json:

    "system_prompt": "inline text..."          # used verbatim
    "system_prompt": {"path": "path/to.md"}    # read from a jail-relative file

Writes the merged prompt to stdout: the jail prompt, then the project prompt
separated by a blank line. With no project prompt the base passes through
unchanged. Exits non-zero on a malformed config, an unsafe path, or a missing
prompt file.
"""
import json
import sys
from pathlib import Path, PurePosixPath

SETTING = "system_prompt"


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


def user_prompt(data: "dict", jail_dir: str,
                config_file: "str | None") -> "str | None":
    """Resolve the project-supplied prompt text, or None when unset."""
    value = data.get(SETTING)
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            die(f"'{SETTING}' in {config_file} must not be empty")
        return value
    if isinstance(value, dict):
        if set(value) != {"path"}:
            die(f"'{SETTING}' object in {config_file} must have exactly a "
                f"'path' key")
        rel = value["path"]
        if not isinstance(rel, str) or not rel:
            die(f"'{SETTING}.path' in {config_file} must be a non-empty string")
        pp = PurePosixPath(rel)
        if pp.is_absolute() or ".." in pp.parts:
            die(f"'{SETTING}.path' must be relative to the jail: {rel}")
        path = Path(jail_dir) / rel
        if not path.is_file():
            die(f"'{SETTING}.path' not found in the jail: {rel}")
        return path.read_text()
    die(f"'{SETTING}' in {config_file} must be a string or a "
        f'{{"path": ...}} object')


def main() -> None:
    if not 2 <= len(sys.argv) <= 3:
        sys.exit(f"Usage: {sys.argv[0]} <jail-dir> [config-file]")
    jail_dir = sys.argv[1].rstrip("/")
    config_file = sys.argv[2] if len(sys.argv) == 3 else None

    base = sys.stdin.read()
    data = read_config(config_file)
    extra = user_prompt(data, jail_dir, config_file)

    parts = [p.strip("\n") for p in (base, extra) if p and p.strip()]
    sys.stdout.write("\n\n".join(parts))


if __name__ == "__main__":
    main()
