#!/usr/bin/env python3
"""Shared helpers for the claude-jail config parsers (the src/ scripts).

run.sh drives several small scripts that each read .claude-jail.json for their
own slice of it — build_mounts.py (read_only / hidden), build_env.py
(default_mode), build_prompt.py (system_prompt) and resolve_user.py (user).
This module is their single source of truth for the things they must agree on:
how errors are reported, how the config is read and shape-checked, what counts
as a shell-/compose-safe bare word, and how a jail-relative path is resolved
without letting it escape the jail (including via symlinks).
"""
import json
import re
import sys
from pathlib import Path, PurePosixPath

# A value safe to place after NAME= in a shell export, to pass through a
# docker-compose ${VAR} interpolation, and to interpolate into a host path: a
# bare word with no whitespace, quotes, path separators, or other
# metacharacters that could break out of the assignment. Shared by build_env.py
# (default_mode) and resolve_user.py (user) so the rule cannot drift between
# them.
BARE_WORD = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]*\Z")


def die(msg: str) -> "None":
    """Print 'Error: <msg>' to stderr and exit non-zero."""
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


def resolve_in_jail(jail_dir: str, rel: str, what: str) -> Path:
    """Resolve a jail-relative path, refusing to escape the jail.

    Rejects absolute paths and `..` components up front, then resolves symlinks
    and confirms the real target stays inside the (real) jail directory — so an
    in-jail symlink cannot point run.sh at a host file outside the jail (e.g. a
    `system_prompt.path` or `read_only` entry pointing at ~/.ssh/id_rsa). On any
    violation it calls die(); otherwise it returns the resolved Path.

    `what` names the setting for error messages, e.g. "'system_prompt.path'".
    """
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts:
        die(f"{what} must be relative to the jail: {rel}")
    jail_root = Path(jail_dir).resolve()
    target = (jail_root / rel).resolve()
    if target != jail_root and jail_root not in target.parents:
        die(f"{what} escapes the jail (resolves outside it): {rel}")
    return target
