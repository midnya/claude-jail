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
project roots and any extra installed packages, then the project prompt, then
any CLI-supplied segments (--system-prompt / --system-prompt-file), separated by
blank lines. A --system-prompt-file follows the same relative-to-config-dir,
no-symlink read, but — like an external `--config`'s prompt files, and unlike an
in-jail config's own system_prompts.path — is trusted to live anywhere on the
host (the operator supplies it, not the agent), so it is not confined to the
roots. Calls die() on a malformed config, an unsafe path, a missing or empty
prompt file, or a prompt segment containing a NUL byte (which cannot be placed in
the environment).
"""
from pathlib import Path

from build_packages import Packages, empty
from jail_config import (Root, config_dir, confine_to_roots, container_path,
                         die, trusted_host_path)

SETTING = "system_prompts"

# Under git-review the agent's .git is writable and its commits are captured for
# review, so the base prompt's read-only-git guidance is wrong. The review
# machinery is meant to be transparent, so rather than explain it, review_base()
# just drops the git-specific read-only claims: the "no git writes" bullet goes
# entirely, and the read-only-paths bullet loses its ".git" clause. Each `old` is
# the exact base-prompt text (joined line-by-line so the wrapping matches
# system-prompt.md); a mismatch — the base file was reworded without updating
# these — dies rather than silently leaving the stale text, and a test drives
# the real base file through review_base() to catch that drift.
_REVIEW_SUBSTITUTIONS = (
    ("\n".join([
        "- **No git write operations.** Do not run git commands that change history or",
        "  refs — no `commit`, `push`, `pull`, `merge`, `rebase`, `reset`, branch",
        "  `checkout`/`switch`, `tag`, `stash`, etc. The `.git` directory is mounted",
        "  read-only, so these fail anyway. Read-only git commands (`status`, `log`,",
        "  `diff`, `show`, `blame`) are fine.",
     ]) + "\n", ""),
    ("\n".join([
        "- **Some paths are mounted read-only.** Besides each root's `.git`, any paths",
        "  the jail config lists under a root's `read_only` are bind-mounted read-only.",
     ]),
     "\n".join([
        "- **Some paths are mounted read-only.** Any paths the jail config lists under a",
        "  root's `read_only` are bind-mounted read-only.",
     ])),
)


def review_base(base: str) -> str:
    """Drop the base prompt's read-only-git claims for git-review mode.

    Under review the agent's .git is writable, so the base prompt's "no git
    writes / .git is read-only" guidance is wrong; review is transparent, so we
    just remove those claims rather than explain the machinery. Every anchor must
    be present — a missing one means the base file was reworded without updating
    the substitution here, which would silently ship contradictory instructions,
    so die() instead of no-op'ing.
    """
    for old, new in _REVIEW_SUBSTITUTIONS:
        if old not in base:
            die("review_base: base prompt no longer contains an expected git "
                "bullet; update _REVIEW_SUBSTITUTIONS to match system-prompt.md")
        base = base.replace(old, new)
    return base


def prompt_path(cfg_dir: str, roots: "list[Root]", path_untrusted: bool,
                rel: str, source: str) -> Path:
    """Locate a prompt file to read on the host.

    Resolved relative to `cfg_dir`, the directory containing the config file.
    `path_untrusted` says whether the *path string* is attacker-controlled — true
    only for a config that lives inside the jail, where the agent writes the
    config and so chooses `system_prompts.path`; there confine_to_roots refuses
    any symlink and requires the result to land inside a jail root, otherwise the
    agent could redirect the read at a host secret and read it back through its
    injected prompt. A trusted path string (an external `--config`'s
    system_prompts, or any --system-prompt-file, both supplied by the operator)
    may live anywhere (an absolute path is taken as-is), though a symlink is still
    refused so an in-root file the agent controls cannot redirect the read.
    `source` names the segment's origin so a confinement failure is attributed to
    what the user wrote (a config setting or a CLI flag), not a fixed key name.
    """
    if path_untrusted:
        return confine_to_roots(cfg_dir, rel, roots, source)
    return trusted_host_path(cfg_dir, rel, source)


def resolve_segment(value: "object", source: str, cfg_dir: str,
                    roots: "list[Root]", path_untrusted: bool) -> str:
    """Resolve one prompt segment (inline string or {"path": ...}) to text.

    `source` names the segment's origin and prefixes every error message — a
    config's setting-and-file (`'system_prompts' in <path>`) or a CLI flag (`a
    --system-prompt-file argument`) — so a rejection is attributed to what the
    user actually wrote, not the config key whose rules a CLI segment merely
    reuses. Rejects a NUL byte: the merged prompt becomes the
    CLAUDE_APPEND_SYSTEM_PROMPT environment variable, and os.environ cannot hold
    an embedded NUL.
    """
    if isinstance(value, str):
        if not value.strip():
            die(f"{source}: prompt segment must not be empty")
        if "\x00" in value:
            die(f"{source}: prompt segment must not contain a NUL byte")
        return value
    if isinstance(value, dict):
        if set(value) != {"path"}:
            die(f"{source}: a path segment must have exactly a 'path' key")
        rel = value["path"]
        if not isinstance(rel, str) or not rel:
            die(f"{source}: path must be a non-empty string")
        path = prompt_path(cfg_dir, roots, path_untrusted, rel, source)
        if not path.is_file():
            die(f"{source}: file not found: {rel}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            die(f"{source}: could not read file {rel}: {e}")
        if not text.strip():
            die(f"{source}: file is empty: {rel}")
        if "\x00" in text:
            die(f"{source}: file must not contain a NUL byte: {rel}")
        return text
    die(f'{source}: prompt segment must be a string or a {{"path": ...}} object')


def user_prompt(data: "dict", config_file: str, cfg_dir: str,
                roots: "list[Root]", config_in_jail: bool) -> "str | None":
    """Resolve the project-supplied prompt text, or None when unset.

    `system_prompts` may be a single segment (an inline string or a
    {"path": ...} object) or a list of such segments joined with a blank line.
    `config_in_jail` (True when classify_config placed the config inside the
    roots) is exactly when a config's own `system_prompts.path` is
    agent-controlled, so it is passed straight through as prompt_path's
    `path_untrusted`: an in-jail config's prompt files confine to the roots, an
    external config's may live anywhere.
    """
    value = data.get(SETTING)
    if value is None:
        return None
    source = f"'{SETTING}' in {config_file}"
    if isinstance(value, list):
        segments = [resolve_segment(v, source, cfg_dir, roots,
                                    config_in_jail) for v in value]
        return "\n\n".join(segments)
    if isinstance(value, (str, dict)):
        return resolve_segment(value, source, cfg_dir, roots, config_in_jail)
    die(f"{source} must be a string, a "
        f'{{"path": ...}} object, or a list of these')


def cli_prompt(segments: "list | None", cfg_dir: str,
               roots: "list[Root]") -> "str | None":
    """Resolve CLI-supplied prompt segments, or None when none were given.

    `segments` is the launcher's ordered list of ("inline", text) / ("path",
    rel) pairs from repeated --system-prompt / --system-prompt-file flags. An
    inline string is used verbatim; a --system-prompt-file path is resolved
    relative to the config dir with symlinks refused, exactly like a config
    system_prompts.path — but always as a *trusted* path (path_untrusted=False),
    since the operator typed it on the command line, not the agent. So unlike an
    in-jail config's own system_prompts.path, a --system-prompt-file is not
    confined to the roots: it may name any file the operator can read, just as an
    external `--config`'s prompt files may. Errors are attributed to the flag (the
    `source` label). Results are joined with a blank line, in command-line order.
    """
    if not segments:
        return None
    parts = []
    for kind, value in segments:
        seg = {"path": value} if kind == "path" else value
        source = ("a --system-prompt-file argument" if kind == "path"
                  else "a --system-prompt argument")
        parts.append(resolve_segment(seg, source, cfg_dir, roots,
                                     path_untrusted=False))
    return "\n\n".join(parts)


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
          packages: "Packages | None" = None, review: bool = False,
          cli_segments: "list | None" = None) -> str:
    """Merge the base jail prompt, the runtime sections, and the project prompt.

    In order: the base jail prompt, the generated project-roots section, the
    generated installed-packages section (always present — it reports the pip
    venv's contents, or their absence), then the project's own system_prompts,
    then any CLI-supplied segments (--system-prompt / --system-prompt-file),
    separated by blank lines. With no project prompt the generated sections still
    pass through. `workdir` is the container working directory the agent starts
    in. `review` (git-review isolation is on for this run) drops the base prompt's
    read-only-git claims, which don't hold when the agent's .git is a writable
    review clone. `cli_segments` are appended after the config's own prompt;
    their --system-prompt-file paths follow the same relative-to-config-dir,
    no-symlink rules but are trusted to live anywhere (the operator supplies
    them), so they are not confined to the roots.
    """
    if review:
        base = review_base(base)
    cfg_dir = config_dir(config_file)
    extra = user_prompt(data, config_file, cfg_dir, roots, config_in_jail)
    cli_extra = cli_prompt(cli_segments, cfg_dir, roots)
    sections = (base, roots_segment(roots, workdir),
                packages_segment(packages or empty()), extra, cli_extra)
    parts = [p.strip("\n") for p in sections if p and p.strip()]
    return "\n\n".join(parts)
