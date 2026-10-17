"""Build the environment for a claude-jail run from .claude-jail.json.

build_mounts.py owns the filesystem keys (read_only / hidden); this module owns
the rest of the config — the settings that shape how claude is launched.
exports() returns the {NAME: value} mapping for the recognised settings (empty
when none is set), which claude-jail sets in the environment and
docker-compose.yml forwards into the container; service_lines() returns the
settings rendered straight into the compose override instead. Calls die() with a message on
stderr if the config is malformed or a value is invalid. Shared
parsing/validation helpers live in jail_config.py.

Settings:
  default_mode -> CLAUDE_JAIL_PERMISSION_MODE
      The permission mode claude starts in, applied by the entrypoint as
      `claude --permission-mode`. The set of valid modes belongs to claude,
      which rejects an unknown one at startup and lists the choices, so we only
      check it is a bare word — both to keep this decoupled from claude's list
      and so the value is safe to export and interpolate without quoting.
  mem_limit -> compose override `mem_limit:`
      ✦ The agent container's memory cap, as docker's byte value: an integer
      with an optional b/k/m/g suffix ("8g"). Unset means unlimited. The
      --mem-limit flag overrides it. Not an env var, so nothing inherited from
      the shell can set it.
  gh_token_file -> GH_TOKEN (via JAIL_GH_TOKEN)
      ✦ A host file holding one fine-grained GitHub token, read by the launcher
      and handed to the in-jail gh CLI through the environment only. Honoured
      only from a config outside the jail: an in-jail config is agent-written, so
      its path could aim the read at any host file. The --gh-token-file flag
      overrides it.
"""
import os
import re

from jail_config import BARE_WORD, die, trusted_host_path

MEM_LIMIT = re.compile(r"\A[1-9][0-9]*[bkmg]?\Z", re.IGNORECASE)
# ✦ Fine-grained PATs only; classic tokens (ghp_) can't be scoped per repository.
GH_TOKEN = re.compile(r"\Agithub_pat_[A-Za-z0-9_]+\Z")


def default_mode(data: "dict", config_file: str) -> "str | None":
    """The claude --permission-mode value from default_mode, or None."""
    mode = data.get("default_mode")
    if mode is None:
        return None
    if not isinstance(mode, str) or not BARE_WORD.match(mode):
        die(f"'default_mode' in {config_file} must be a bare word "
            f"(letter, then letters/digits/'-'/'_'); got {mode!r}")
    return mode


def mem_limit(data: "dict", config_file: str,
              flag: "str | None" = None) -> "str | None":
    """✦ The docker memory cap (--mem-limit, else mem_limit), or None."""
    if flag is not None:
        value, source = flag, "--mem-limit"
    else:
        value, source = data.get("mem_limit"), f"'mem_limit' in {config_file}"
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) \
            or not MEM_LIMIT.match(str(value)):
        die(f"{source} must be a positive size like \"8g\" or \"512m\"; "
            f"got {value!r}")
    return str(value)


def gh_token(data: "dict", config_file: str, cfg_dir: str,
             config_in_jail: bool, flag: "str | None" = None,
             read: bool = True) -> "str | None":
    """✦ The token from --gh-token-file, else gh_token_file, or None.

    Paths expand `~`, resolve against `cfg_dir` and never through a symlink. `read=False`
    validates the setting without touching the file, for commands that don't
    launch a session.
    """
    key = data.get("gh_token_file")
    key_source = f"'gh_token_file' in {config_file}"
    if key is not None and config_in_jail:
        die(f"{key_source} is refused: the config is inside the jail, so the agent "
            "controls the path; use --gh-token-file or an external --config")
    rel, source = (flag, "--gh-token-file") if flag is not None \
        else (key, key_source)
    if rel is None:
        return None
    if not isinstance(rel, str) or not rel:
        die(f"{source} must be a non-empty path")
    if not read:
        return None
    rel = os.path.expanduser(rel)
    path = trusted_host_path(cfg_dir, rel, source)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        die(f"{source}: could not read {rel}: {e}")
    if not GH_TOKEN.match(token):
        die(f"{source}: {rel} must hold exactly one fine-grained GitHub token "
            "(github_pat_...)")
    return token


def exports(data: "dict", config_file: str) -> "dict[str, str]":
    """The {NAME: value} env mapping for every recognised setting that is set."""
    # (env var, value) for each recognised setting; None means "not set".
    settings = [
        ("CLAUDE_JAIL_PERMISSION_MODE", default_mode(data, config_file)),
    ]
    return {name: value for name, value in settings if value is not None}


def service_lines(data: "dict", config_file: str,
                  mem_limit_flag: "str | None" = None) -> "list[str]":
    """✦ The `claude` service keys for the compose override, indented to fit."""
    mem = mem_limit(data, config_file, mem_limit_flag)
    return [f"    mem_limit: {mem}"] if mem else []
