"""Merge the jail system prompt with an optional project-supplied one.

build_mounts.py owns the filesystem keys and build_env.py the bare-word
settings; this module owns the one free-text setting, system_prompt. The shared
parsing/validation helpers live in jail_config.py.

merge() joins the base jail prompt with `system_prompt` from .claude-jail.json.
A prompt is one or more segments, each either inline text or a file:

    "system_prompt": "inline text..."          # used verbatim
    "system_prompt": {"path": "path/to.md"}    # read from a file
    "system_prompt": ["intro...", {"path": "more.md"}]   # segments joined

A list's segments are joined with a blank line, in order; a bare string or
object is shorthand for a single-segment list.

A `{"path": ...}` file is read on the host at launch (its text is injected into
the agent's prompt, never mounted). For a config inside the jail the path is
jail-relative and confined to the jail; for a `--config` outside the jail it is
read relative to that config file and may live anywhere on the host.

merge() returns the jail prompt, then the project prompt separated by a blank
line; with no project prompt the base passes through unchanged. Calls die() on a
malformed config, an unsafe path, a missing or empty prompt file, or a prompt
segment containing a NUL byte (which cannot be placed in the environment).
"""
from pathlib import Path

from jail_config import die, resolve_in_jail

SETTING = "system_prompt"


def prompt_path(jail_dir: str, config_file: str,
                config_rel: "str | None", rel: str) -> Path:
    """Locate a system_prompt file to read on the host.

    A config inside the jail (config_rel is not None) is agent-writable, so its
    prompt file must stay inside the jail (resolve_in_jail) — otherwise the agent
    could redirect it at a host secret and read it back through its own injected
    prompt. A config the user keeps outside the jail (config_rel is None) is
    trusted: its prompt file is read relative to that config (an absolute path is
    taken as-is) and may live anywhere.
    """
    if config_rel is not None:
        return resolve_in_jail(jail_dir, rel, f"'{SETTING}.path'")
    return (Path(config_file).resolve().parent / rel).resolve()


def resolve_segment(value: "object", jail_dir: str, config_file: str,
                    config_rel: "str | None") -> str:
    """Resolve one prompt segment (inline string or {"path": ...}) to text.

    Rejects a NUL byte: the merged prompt becomes the CLAUDE_APPEND_SYSTEM_PROMPT
    environment variable, and os.environ cannot hold an embedded NUL.
    """
    if isinstance(value, str):
        if not value.strip():
            die(f"'{SETTING}' segment in {config_file} must not be empty")
        if "\x00" in value:
            die(f"'{SETTING}' segment in {config_file} must not contain a NUL "
                f"byte")
        return value
    if isinstance(value, dict):
        if set(value) != {"path"}:
            die(f"'{SETTING}' object in {config_file} must have exactly a "
                f"'path' key")
        rel = value["path"]
        if not isinstance(rel, str) or not rel:
            die(f"'{SETTING}.path' in {config_file} must be a non-empty string")
        path = prompt_path(jail_dir, config_file, config_rel, rel)
        if not path.is_file():
            die(f"'{SETTING}.path' file not found: {rel}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            die(f"'{SETTING}.path' file is empty: {rel}")
        if "\x00" in text:
            die(f"'{SETTING}.path' file must not contain a NUL byte: {rel}")
        return text
    die(f"'{SETTING}' segment in {config_file} must be a string or a "
        f'{{"path": ...}} object')


def user_prompt(data: "dict", jail_dir: str, config_file: str,
                config_rel: "str | None") -> "str | None":
    """Resolve the project-supplied prompt text, or None when unset.

    `system_prompt` may be a single segment (an inline string or a
    {"path": ...} object) or a list of such segments joined with a blank line.
    `config_rel` (the config's jail-relative path, or None when it lives outside
    the jail) is threaded down to prompt_path, which confines an in-jail config's
    prompt files to the jail and trusts an external config's anywhere.
    """
    value = data.get(SETTING)
    if value is None:
        return None
    if isinstance(value, list):
        segments = [resolve_segment(v, jail_dir, config_file, config_rel)
                    for v in value]
        return "\n\n".join(segments)
    if isinstance(value, (str, dict)):
        return resolve_segment(value, jail_dir, config_file, config_rel)
    die(f"'{SETTING}' in {config_file} must be a string, a "
        f'{{"path": ...}} object, or a list of these')


def merge(base: str, data: "dict", jail_dir: str, config_file: str,
          config_rel: "str | None") -> str:
    """Merge the base jail prompt with the project's, the base first.

    The two are separated by a blank line; with no project prompt the base
    passes through unchanged.
    """
    extra = user_prompt(data, jail_dir, config_file, config_rel)
    parts = [p.strip("\n") for p in (base, extra) if p and p.strip()]
    return "\n\n".join(parts)
