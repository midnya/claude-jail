"""Merge the jail system prompt with an optional project-supplied one.

build_mounts.py owns the filesystem keys and build_env.py the bare-word
settings; this module owns the one free-text setting, system_prompts. The shared
parsing/validation helpers live in jail_config.py.

merge() joins the base jail prompt with `system_prompts` from .claude-jail.json.
A prompt is one or more segments, each either inline text or a file:

    "system_prompts": "inline text..."          # used verbatim
    "system_prompts": {"path": "path/to.md"}    # read from a file
    "system_prompts": ["intro...", {"path": "more.md"}]   # segments joined

A list's segments are joined with a blank line, in order; a bare string or
object is shorthand for a single-segment list.

A `{"path": ...}` file is read on the host at launch (its text is injected into
the agent's prompt, never mounted), relative to the directory containing the
config file. The read never follows a symlink, so a symlink the agent planted
in a writable root cannot redirect it at a host secret. For a config inside the
jail the resolved file must additionally land inside one of the jail roots; for
a `--config` outside the jail it is trusted and may live anywhere on the host.

merge() returns the jail prompt, then generated sections naming the runtime
project roots and any extra installed packages, then the project prompt,
separated by blank lines. Calls die() on a malformed config, an unsafe path, a
missing or empty prompt file, or a prompt segment containing a NUL byte (which
cannot be placed in the environment).
"""
from pathlib import Path

from build_packages import Packages, empty
from jail_config import (Root, config_dir, confine_to_roots, container_path,
                         die, trusted_host_path)

SETTING = "system_prompts"


def prompt_path(cfg_dir: str, roots: "list[Root]", config_in_jail: bool,
                rel: str) -> Path:
    """Locate a system_prompts file to read on the host.

    Resolved relative to `cfg_dir`, the directory containing the config file. An
    in-jail config is agent-writable, so confine_to_roots refuses to follow any
    symlink and requires the result to land inside a jail root — otherwise the
    agent could redirect it at a host secret and read it back through its own
    injected prompt. An external (`--config`) config is trusted: the file may
    live anywhere (an absolute path is taken as-is), though a symlink is still
    refused so an in-root file the agent controls cannot redirect the read.
    """
    what = f"'{SETTING}.path'"
    if config_in_jail:
        return confine_to_roots(cfg_dir, rel, roots, what)
    return trusted_host_path(cfg_dir, rel, what)


def resolve_segment(value: "object", config_file: str, cfg_dir: str,
                    roots: "list[Root]", config_in_jail: bool) -> str:
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
        path = prompt_path(cfg_dir, roots, config_in_jail, rel)
        if not path.is_file():
            die(f"'{SETTING}.path' file not found: {rel}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            die(f"could not read '{SETTING}.path' file {rel}: {e}")
        if not text.strip():
            die(f"'{SETTING}.path' file is empty: {rel}")
        if "\x00" in text:
            die(f"'{SETTING}.path' file must not contain a NUL byte: {rel}")
        return text
    die(f"'{SETTING}' segment in {config_file} must be a string or a "
        f'{{"path": ...}} object')


def user_prompt(data: "dict", config_file: str, cfg_dir: str,
                roots: "list[Root]", config_in_jail: bool) -> "str | None":
    """Resolve the project-supplied prompt text, or None when unset.

    `system_prompts` may be a single segment (an inline string or a
    {"path": ...} object) or a list of such segments joined with a blank line.
    `config_in_jail` (True when classify_config placed the config inside the
    roots) is threaded down to prompt_path, which confines an in-jail config's
    prompt files to the roots and trusts an external config's anywhere.
    """
    value = data.get(SETTING)
    if value is None:
        return None
    if isinstance(value, list):
        segments = [resolve_segment(v, config_file, cfg_dir, roots,
                                    config_in_jail) for v in value]
        return "\n\n".join(segments)
    if isinstance(value, (str, dict)):
        return resolve_segment(value, config_file, cfg_dir, roots,
                               config_in_jail)
    die(f"'{SETTING}' in {config_file} must be a string, a "
        f'{{"path": ...}} object, or a list of these')


def roots_segment(roots: "list[Root]", workdir: str) -> str:
    """A prompt segment telling the agent its working dir and project roots.

    The agent starts in `workdir` (the config file's directory under /workspace);
    each jail root is bind-mounted under /workspace at /workspace<host path>, so
    the agent needs the list to know where its project actually lives — its
    working directory is not necessarily one of the mounted roots.
    """
    lines = [
        "# Project roots",
        "",
        f"Your working directory is `{workdir}`. The directories you work in "
        "are bind-mounted under `/workspace` (read-write unless marked "
        "read-only), mirroring their host paths:",
        "",
    ]
    lines += [
        f"- `{container_path(r.dir)}`" + (" (read-only)" if r.read_only_all()
                                          else "")
        for r in roots
    ]
    lines += [
        "",
        "Other paths under `/workspace`, and the rest of the container "
        "filesystem, are not part of your project.",
    ]
    return "\n".join(lines)


def packages_segment(packages: "Packages") -> str:
    """A prompt segment reporting the extra packages baked into this jail.

    The project's `packages.apt`/`packages.pip`/`packages.npm` are installed into
    the image, so telling the agent what is there saves it rediscovering (or
    wrongly assuming the absence of) them. The pip line is always emitted — the
    venv always exists and the base prompt promises this section reports it either
    way, so the agent knows whether anything extra is installed in it — while the
    apt and npm lines name only the project's extra requests and appear only when
    there are some (node/npm work normally from the base image regardless, so an
    absent npm line means nothing special is set up rather than nothing exists).

    The venv lives at a root-owned path, so the agent (running unprivileged)
    cannot modify it in place, and `--user` installs are disabled image-wide
    (PYTHONNOUSERSITE=1) so a stray one isn't importable either. None of this is
    a security boundary — the agent can run anything; the real barrier is
    default-deny egress. The line just points at the ask-before-installing rule
    rather than enumerating these.
    """
    venv_note = ("`python3`, `pip`, and any installed console scripts resolve to "
                 "a venv on your `PATH`; the venv itself is read-only (root-owned), "
                 "so you can't change it in place — install anything new only "
                 "after asking, per the package rule above.")
    lines = ["# Installed packages", "",
             "Extra packages for this sandbox, requested by the project:"]
    if packages.apt:
        listed = ", ".join(f"`{p}`" for p in packages.apt)
        lines.append(f"- System packages (apt): {listed}.")
    if packages.npm:
        listed = ", ".join(f"`{p}`" for p in packages.npm)
        lines.append(f"- Node packages (npm): {listed} — installed via yarn into "
                     "a read-only tree on your `PATH` (and `NODE_PATH`, for "
                     "`require()`); install anything new only after asking, per "
                     "the package rule above.")
    pip_body = (", ".join(f"`{p}`" for p in packages.pip) if packages.pip
                else "none requested")
    pip_line = f"- Python packages (pip): {pip_body} — {venv_note}"
    if not packages.pip:
        pip_line += " Nothing extra is installed in the venv."
    lines.append(pip_line)
    return "\n".join(lines)


def merge(base: str, data: "dict", config_file: str, roots: "list[Root]",
          config_in_jail: bool, workdir: str,
          packages: "Packages | None" = None) -> str:
    """Merge the base jail prompt, the runtime sections, and the project prompt.

    In order: the base jail prompt, the generated project-roots section, the
    generated installed-packages section (always present — it reports the pip
    venv's contents, or their absence), then the project's own system_prompts,
    separated by blank lines. With no project prompt the generated sections still
    pass through. `workdir` is the container working directory the agent starts
    in.
    """
    extra = user_prompt(data, config_file, config_dir(config_file), roots,
                        config_in_jail)
    sections = (base, roots_segment(roots, workdir),
                packages_segment(packages or empty()), extra)
    parts = [p.strip("\n") for p in sections if p and p.strip()]
    return "\n\n".join(parts)
