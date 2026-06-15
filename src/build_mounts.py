"""Build the docker compose override (volumes) for a claude-jail run.

Owns the filesystem keys of .claude-jail.json (read_only / hidden); the rest of
the config is parsed by build_env.py and the shared helpers live in
jail_config.py. override() combines the built-in mount policy with the config's
path lists, resolves nesting and precedence into a mount tree (hidden always
trumps read_only), and returns a docker compose override document (empty when
there are no mounts). Exits non-zero with a message on stderr if the config is
malformed, a requested path is unsafe (absolute, contains `..`, or resolves
outside the jail via a symlink), or a hidden path is missing on the host.

Hidden directories are masked with an empty read-only volume; hidden files with
a read-only bind of an empty file shipped in the claude-jail repo (a volume
can't mount over a single file). Both read as empty; because the file mask is a
regular file on a read-only mount, writes to a hidden file are refused (EROFS)
rather than silently discarded as they were when masked with /dev/null. The
mask file lives outside the jail directory, so the sandboxed agent has no
writable path to it and cannot un-empty the masks.
"""
import json
from pathlib import Path, PurePosixPath

from jail_config import die, resolve_in_jail

# Top-level array keys recognised in .claude-jail.json. Each holds a list of
# jail-relative paths.
#   read_only — bind-mounted read-only; visible but not writable.
#   hidden    — content masked (empty) inside the container.
KEYS = ("read_only", "hidden")

# Built-in policy applied to every jail. The active config file is hidden at
# runtime when it lives inside the jail (see load_requested).
DEFAULTS = {
    "read_only": [".git"],
    "hidden": [],
}

# When one path is requested under several modes, the highest priority wins.
# Hidden always trumps read_only.
PRIORITY = {"read_only": 1, "hidden": 2}

# Empty file, shipped in the claude-jail repo next to this script, that masks
# every hidden file. A read-only bind of this regular file makes writes to a
# hidden file fail with EROFS, instead of vanishing as they did when masked
# with /dev/null. It lives outside the jail, so the agent cannot write to it.
EMPTY_MASK = ".claude-jail-empty"
SCRIPT_DIR = Path(__file__).resolve().parent


def load_requested(data: "dict", config_file: str,
                   config_rel: "str | None") -> "dict[str, list[str]]":
    """Merge the built-in defaults with the config's path lists.

    The active config file is hidden (masked empty, unwritable) when it lives
    inside the jail, so the sandboxed agent can neither read nor rewrite its own
    jail rules — wherever -c placed it, not just the conventional
    .claude-jail.json. `config_rel` is its jail-relative path (None when the
    config is external, see relpath_in_jail). A config that doesn't exist on the
    host is skipped, since hiding a missing path is otherwise a hard error.
    """
    requested = {key: list(DEFAULTS[key]) for key in KEYS}
    if config_rel is not None and Path(config_file).is_file():
        requested["hidden"].append(config_rel)

    for key in KEYS:
        entries = data.get(key, [])
        if not isinstance(entries, list):
            die(f"'{key}' in {config_file} must be a list")
        for p in entries:
            if not isinstance(p, str) or not p:
                die(f"invalid {key} entry in {config_file}: {p!r}")
            pp = PurePosixPath(p)
            if pp.is_absolute() or ".." in pp.parts:
                die(f"{key} path must be relative to the jail: {p}")
            if not pp.parts:
                die(f"{key} path must not be the jail root: {p!r}")
            requested[key].append(p)
    return requested


class Node:
    __slots__ = ("children", "mode")

    def __init__(self) -> None:
        self.children: "dict[str, Node]" = {}
        self.mode: "str | None" = None


def build_tree(requested: "dict[str, list[str]]") -> Node:
    """Insert every requested path into a tree, keeping the winning mode."""
    root = Node()
    for mode in KEYS:
        for p in requested[mode]:
            node = root
            for part in PurePosixPath(p).parts:
                node = node.children.setdefault(part, Node())
            if node.mode is None or PRIORITY[mode] > PRIORITY[node.mode]:
                node.mode = mode
    return root


def resolve(node: Node, parts: "list[str]", covered_ro: bool,
            out: "list[tuple[str, str]]") -> None:
    """Walk the tree, collecting (relpath, mode) mounts in precedence order.

    A `hidden` node masks its whole subtree, so we emit it and stop. A
    `read_only` node binds its subtree read-only; a nested read_only is
    redundant, but a nested `hidden` still masks a sub-path, so we keep walking.
    """
    recurse = True
    if node.mode == "hidden":
        out.append(("/".join(parts), "hidden"))
        recurse = False
    elif node.mode == "read_only":
        if not covered_ro:
            out.append(("/".join(parts), "read_only"))
        covered_ro = True
    if recurse:
        for name in sorted(node.children):
            resolve(node.children[name], parts + [name], covered_ro, out)


def ensure_empty_mask() -> None:
    """Ensure the empty file that masks hidden files exists and is empty.

    It is shipped in the claude-jail repo alongside this script; recreate it if
    absent. Fail closed if something non-empty sits at that path, since using it
    as the mask would leak its content into every hidden file.
    """
    path = SCRIPT_DIR / EMPTY_MASK
    if path.exists():
        if not path.is_file():
            die(f"mask path exists but is not a regular file: {path}")
        if path.stat().st_size != 0:
            die(f"mask file must be empty: {path}")
        return
    try:
        path.touch()
    except OSError as e:
        die(f"could not create mask file {path}: {e}")


def render(mounts: "list[tuple[str, str]]", jail_dir: str) -> str:
    """Render the resolved mounts as a docker compose override document.

    Side-effect-free: when the result binds the empty mask (a hidden file is
    present, see needs_empty_mask) the caller must ensure_empty_mask() before
    compose runs, keeping host writes out of the validation step.
    """
    volumes: "list[str]" = []
    for rel, mode in mounts:
        # Refuse a path that escapes the jail via a symlink before we bind it;
        # otherwise a `read_only` symlink could expose a host file to the agent
        # and a `hidden` one could mask the wrong target. (Absolute / `..` were
        # already rejected at parse time.)
        resolve_in_jail(jail_dir, rel, f"{mode} path")
        source = f"{jail_dir}/{rel}"
        target = f"/workspace{jail_dir}/{rel}"
        host = Path(source)
        tgt = json.dumps(target, ensure_ascii=False)
        if mode == "read_only":
            if not host.exists():
                # Nothing on the host to protect; create_host_path is off.
                continue
            src = json.dumps(source, ensure_ascii=False)
            volumes += [
                "      - type: bind",
                f"        source: {src}",
                f"        target: {tgt}",
                "        read_only: true",
                "        bind:",
                "          create_host_path: false",
            ]
            continue
        # hidden — fail closed: a missing path is almost always a typo, and
        # silently skipping it would leave the real secret unmasked.
        if not host.exists():
            die(f"hidden path not found in the jail: {rel}")
        if host.is_dir():
            # A fresh anonymous read-only volume masks the directory's contents
            # and stays unwritable. nocopy keeps it empty; anonymous (no source)
            # so each mask is isolated and nothing persists across jails or
            # runs. (A tmpfs can't be made read-only through compose, and
            # tmpfs-mode is ignored when mounted over an existing directory.)
            volumes += [
                "      - type: volume",
                f"        target: {tgt}",
                "        read_only: true",
                "        volume:",
                "          nocopy: true",
            ]
        else:
            # A volume can't mount over a single file, so bind an empty file
            # (shipped in the claude-jail repo) over it. Being a regular file on
            # a read-only mount, writes to a hidden file are refused with EROFS
            # rather than silently swallowed the way /dev/null swallowed them.
            src = json.dumps(str(SCRIPT_DIR / EMPTY_MASK), ensure_ascii=False)
            volumes += [
                "      - type: bind",
                f"        source: {src}",
                f"        target: {tgt}",
                "        read_only: true",
                "        bind:",
                "          create_host_path: false",
            ]
    if not volumes:
        return ""
    out: "list[str]" = ["services:", "  claude-jail:", "    volumes:"]
    out += volumes
    return "\n".join(out) + "\n"


def needs_empty_mask(override: str) -> bool:
    """True when the override binds the empty mask (a hidden file is present).

    The launcher checks this in its launch phase to create the mask (the only
    host write build_mounts performs) only once the config is fully validated.
    """
    return EMPTY_MASK in override


def override(data: "dict", jail_dir: str, config_file: str,
             config_rel: "str | None") -> str:
    """Build the docker compose volume override; "" when there are no mounts."""
    requested = load_requested(data, config_file, config_rel)
    tree = build_tree(requested)
    mounts: "list[tuple[str, str]]" = []
    resolve(tree, [], False, mounts)
    return render(mounts, jail_dir)
