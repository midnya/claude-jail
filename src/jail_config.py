"""Shared helpers for the claude-jail config parsers (the src/ modules).

claude-jail reads .claude-jail.json once and hands the parsed data to several
small modules that each interpret their own slice of it — build_mounts.py
(roots / read_only / hidden), build_env.py (default_mode), build_prompt.py
(system_prompts) and resolve_user.py (user). This module is their single source
of truth for the things they must agree on: how errors are reported, how the
config is read and shape-checked, what counts as a shell-/compose-safe bare
word, what the jail's roots are, and how a path is resolved without letting it
escape those roots (including via symlinks).

The config is anchored on the config file, not a CLI directory: `roots` lists
the directories bind-mounted into the jail (each with its own read_only/hidden
rules), defaulting to the directory containing the config file when absent. A
root `path` is resolved relative to that directory.
"""
import json
import os
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

# Keys recognised inside a `roots` entry object.
ROOT_KEYS = {"path", "read_only", "hidden"}

# Keys recognised at the top level of .claude-jail.json. read_only/hidden are
# per-root only (under a `roots` entry); rejecting them — and any other unknown
# key — at the top level turns a silently-ignored legacy config or a typo into a
# hard error, instead of a jail that quietly drops the protections they meant.
TOP_LEVEL_KEYS = {"user", "default_mode", "system_prompts", "roots"}


def die(msg: str) -> "None":
    """Print 'Error: <msg>' to stderr and exit non-zero."""
    sys.exit(f"Error: {msg}")


def read_config(config_file: str) -> "dict":
    """Read and JSON-parse the jail config, returning {} when absent."""
    path = Path(config_file)
    if not path.is_file():
        # A path that exists but is not a regular file (a directory, FIFO, ...)
        # is a mistake, not an absent config: treating it as {} would silently
        # jail the *parent* directory, so fail loudly. A truly absent path is
        # fine — it yields the default config.
        if path.exists():
            die(f"config path is not a regular file: {config_file}")
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        die(f"could not read {config_file}: {e}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {config_file}: {e}")
    if not isinstance(data, dict):
        die(f"{config_file} must contain a JSON object")
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        legacy = unknown & {"read_only", "hidden"}
        if legacy:
            die(f"{config_file}: {sorted(legacy)} are now per-root keys; move "
                f"them inside a 'roots' entry (see README)")
        die(f"unknown top-level key(s) in {config_file}: {sorted(unknown)}")
    return data


def resolved_config(config_path: str) -> Path:
    """The config file's canonical (realpath) absolute path.

    The single canonicalization for the config anchor, so the jail identity, the
    working directory, the roots and the prompt files all agree on where the
    config really is (one API — no os.path.realpath vs Path.resolve drift).
    """
    return Path(config_path).resolve()


def config_dir(config_path: str) -> str:
    """The directory containing the config file, canonicalized.

    The anchor every relative path in the config (roots, system_prompts.path) is
    resolved against, and the agent's working directory under /workspace.
    """
    return str(resolved_config(config_path).parent)


def container_path(host_dir: str) -> str:
    """The container mount target for a host path: /workspace<host path>.

    The single host->container mapping shared by the bind mounts, the working
    directory and the project-roots prompt, so they cannot drift apart.
    """
    return f"/workspace{host_dir}"


class Root:
    """A jail root: a host directory and its per-root read_only/hidden lists.

    `dir` is the resolved (realpath) absolute directory; `read_only` and
    `hidden` are the raw, root-relative path lists from the config (validated
    and expanded with the built-in defaults by build_mounts.py).
    """
    __slots__ = ("dir", "read_only", "hidden")

    def __init__(self, dir: str, read_only: "list", hidden: "list") -> None:
        self.dir = dir
        self.read_only = read_only
        self.hidden = hidden


def _inside(root: Path, target: Path) -> bool:
    """True when `target` is `root` itself or sits beneath it."""
    return target == root or root in target.parents


def _forbidden_root(root_dir: str) -> bool:
    """True for a root too broad to jail: the filesystem root, or $HOME or any
    ancestor of it.

    The filesystem root bind-mounts the whole host. $HOME — or any directory
    that contains it (e.g. /home) — bind-mounts the per-user credential store
    (~/.claude-jail-*) read-write into the sandbox, letting the agent steal or
    rewrite its own login, so reject the home directory and every ancestor, not
    just an exact match.
    """
    if root_dir == os.sep:
        return True
    home = os.environ.get("HOME")
    if not home:
        return False
    # _inside(root, home) is True when root *is* HOME or an ancestor of it.
    return _inside(Path(root_dir), Path(os.path.realpath(home)))


def _key_list(entry: "dict", key: str, config_file: str) -> "list":
    """A roots-entry list value (read_only / hidden); [] when absent."""
    value = entry.get(key, [])
    if not isinstance(value, list):
        die(f"'roots[].{key}' in {config_file} must be a list")
    return value


def parse_roots(data: "dict", config_file: str) -> "list[Root]":
    """Resolve the jail roots from the config.

    `roots` is a list whose entries are either a bare string (shorthand for
    {"path": ...}) or an object with `path` and optional `read_only`/`hidden`.
    A relative `path` is resolved against the directory containing the config
    file; an absolute one is taken as-is; both are realpath'd. When `roots` is
    absent the single root is the config file's own directory. Each root must be
    an existing directory, and roots may not be nested in or duplicate one
    another (overlapping bind mounts). Calls die() on any violation.
    """
    base = config_dir(config_file)
    entries = data.get("roots")
    if entries is None:
        entries = ["."]
    elif not isinstance(entries, list):
        die(f"'roots' in {config_file} must be a list")
    elif not entries:
        die(f"'roots' in {config_file} must not be empty")

    roots: "list[Root]" = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            die(f"each 'roots' entry in {config_file} must be a string or "
                f"a {{\"path\": ...}} object")
        unknown = set(entry) - ROOT_KEYS
        if unknown:
            die(f"unknown key(s) in a 'roots' entry in {config_file}: "
                f"{sorted(unknown)}")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            die(f"'roots[].path' in {config_file} must be a non-empty string")
        p = Path(path)
        if not p.is_absolute():
            p = Path(base) / path
        root_dir = os.path.realpath(p)
        if not os.path.isdir(root_dir):
            die(f"root path is not a directory: {path}")
        if _forbidden_root(root_dir):
            die(f"a jail root may not be the filesystem root, your home "
                f"directory, or a directory containing it: {path}")
        for other in roots:
            if (root_dir == other.dir
                    or _inside(Path(root_dir), Path(other.dir))
                    or _inside(Path(other.dir), Path(root_dir))):
                die(f"roots overlap (nested or duplicate): {path}")
        roots.append(Root(
            root_dir,
            _key_list(entry, "read_only", config_file),
            _key_list(entry, "hidden", config_file),
        ))
    return roots


def resolve_in_root(root_dir: str, rel: str, what: str) -> Path:
    """Resolve a root-relative path, refusing to escape that root.

    Rejects absolute paths and `..` components up front, then resolves symlinks
    and confirms the real target stays inside the (real) root — so an in-jail
    symlink cannot point claude-jail at a host file outside the root (e.g. a
    `read_only` entry pointing at ~/.ssh/id_rsa). On any violation it calls
    die(); otherwise it returns the resolved Path. `what` names the setting for
    error messages, e.g. "read_only path".
    """
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts:
        die(f"{what} must be relative to its root: {rel}")
    # root_dir is already an os.path.realpath result, so Path(root_dir) is
    # canonical and needs no further .resolve().
    root = Path(root_dir)
    target = (root / rel).resolve()
    if not _inside(root, target):
        die(f"{what} escapes its root (resolves outside it): {rel}")
    return target


def _walk_no_symlink(base_dir: str, rel: str, what: str) -> Path:
    """Walk `rel` from `base_dir` one component at a time, refusing any symlink.

    Returns the lexical path (no .resolve()): because every existing component
    is checked not to be a symlink, that lexical path equals the real one. A
    planted symlink (e.g. doc.md -> a hidden file, or -> a host secret) is the
    whole attack, so traversing one is never allowed. An absolute `rel` walks
    from the filesystem root; `..` climbs lexically.
    """
    pp = PurePosixPath(rel)
    if pp.is_absolute():
        cur, parts = Path(pp.anchor), pp.parts[1:]
    else:
        cur, parts = Path(base_dir), pp.parts
    for part in parts:
        if part == "..":
            cur = cur.parent
            continue
        cur = cur / part
        if cur.is_symlink():
            die(f"{what} must not traverse a symlink: {rel}")
    return cur


def confine_to_roots(base_dir: str, rel: str, roots: "list[Root]",
                     what: str) -> Path:
    """Resolve an agent-influenced host file: refuse symlinks AND confine to a root.

    For a config that lives inside the jail, the agent controls every byte under
    a root, so a file read into its prompt must be one it could already read: no
    symlink may be traversed and the result must land inside some root. Walking
    from the trusted, already-resolved `base_dir` (the config file's directory).
    """
    cur = _walk_no_symlink(base_dir, rel, what)
    if not any(_inside(Path(r.dir), cur) for r in roots):
        die(f"{what} escapes the jail (resolves outside every root): {rel}")
    return cur


def trusted_host_path(base_dir: str, rel: str, what: str) -> Path:
    """Resolve a user-owned host file: refuse symlinks, but allow it anywhere.

    For a `--config` the user keeps outside the jail the file is trusted and may
    live anywhere (an absolute `rel` is taken as-is); a symlink is still refused,
    so an in-root file the agent controls cannot redirect the read.
    """
    return _walk_no_symlink(base_dir, rel, what)


def classify_config(roots: "list[Root]",
                    config_path: str) -> "tuple[str, str] | None":
    """Locate the active config within the roots: (root_dir, rel) or None.

    Classifies by the *resolved* location, not the spelled one: a config whose
    real path is inside a root is agent-writable and must be masked there (and
    have its prompt files confined), even when reached through a symlink that
    lexically points elsewhere. A config whose real path is outside every root
    is unreachable from the container, so this returns None and nothing is
    mounted for it.

    The one hard error is a path that lexically *names* something inside a root
    but escapes through a symlink: the agent controls that in-root symlink and
    could otherwise dodge the mask that hides the active config, so it dies
    loudly. A not-yet-created path still resolves by name.
    """
    real = resolved_config(config_path)
    for r in roots:
        root = Path(r.dir)
        if _inside(root, real):
            rel = str(real.relative_to(root))
            return None if rel == "." else (r.dir, rel)
    abspath = Path(os.path.abspath(config_path))
    for r in roots:
        if _inside(Path(r.dir), abspath):
            die(f"config file escapes the jail (resolves outside it): "
                f"{config_path}")
    return None
