"""Build the docker compose override (volumes) for a claude-jail run.

Owns the filesystem keys of .claude-jail.json (the per-root read_only / hidden
lists); the roots themselves are parsed by jail_config.py and the rest of the
config by build_env.py. override() emits, for each jail root, a bind of the root
(read-write, or read-only when a `read_only` entry names the root itself) plus
its masks: it combines the built-in mount policy with that root's path lists,
resolves nesting and precedence into a mount tree (hidden always trumps
read_only), and renders a docker compose override document. Exits
non-zero with a message on stderr if the config is malformed, a requested path
is unsafe (absolute, contains `..`, or resolves outside its root via a symlink),
or a hidden path is missing on the host.

Hidden directories are masked with an empty read-only volume; hidden files with
a read-only bind of an empty file shipped in the claude-jail repo (a volume
can't mount over a single file). Both read as empty; because the file mask is a
regular file on a read-only mount, writes to a hidden file are refused (EROFS)
rather than silently discarded as they were when masked with /dev/null. The
mask file lives outside the jail roots, so the sandboxed agent has no writable
path to it and cannot un-empty the masks.
"""
import json
from pathlib import Path, PurePosixPath

from jail_config import (Root, container_path, die, names_root_itself,
                         resolve_in_root)

# Per-root array keys recognised in a roots entry. Each holds a list of paths
# relative to that root.
#   read_only — bind-mounted read-only; visible but not writable.
#   hidden    — content masked (empty) inside the container.
KEYS = ("read_only", "hidden")

# Built-in read_only policy applied to every root.
DEFAULT_READ_ONLY = [".git"]

# The jail config file name. Every root masks its own .claude-jail.json — and
# the active config wherever it sits (see requested_for_root) — even when it is
# absent on the host, so the sandboxed agent can neither read nor plant the jail
# rules a later run would load.
CONFIG_NAME = ".claude-jail.json"

# When one path is requested under several modes, the highest priority wins.
# Hidden always trumps read_only.
PRIORITY = {"read_only": 1, "hidden": 2}

# Empty file, shipped in the claude-jail repo next to this script, that masks
# every hidden file. A read-only bind of this regular file makes writes to a
# hidden file fail with EROFS, instead of vanishing as they did when masked
# with /dev/null. It lives outside the jail roots, so the agent cannot write it.
EMPTY_MASK = ".claude-jail-empty"
SCRIPT_DIR = Path(__file__).resolve().parent


def requested_for_root(root: Root, config_class: "tuple[str, str] | None",
                       read_only_all: bool = False
                       ) -> "tuple[dict[str, list[str]], set[str]]":
    """Per-root requested mounts + the rels to mask even when absent.

    Every root hides its own .claude-jail.json (CONFIG_NAME), and the active
    config too when it lives inside *this* root, so the sandboxed agent can
    neither read nor rewrite the jail rules a later run would load. `config_class`
    is (root_dir, rel) from classify_config (None when the config is external).
    Returns (requested, mask_absent); mask_absent is the set of these
    config-protection rels, which `_mask_volumes` masks with an empty read-only
    file even when they do not yet exist (so the agent cannot create one).

    `read_only_all` (computed once by override() via Root.read_only_all() and
    shared with _mask_volumes) is True when a `read_only` entry names the root
    itself; the per-path `read_only` mounts are then dropped as redundant — the
    read-only root bind override() emits covers them — while `hidden` is kept.
    """
    requested = {"read_only": list(DEFAULT_READ_ONLY), "hidden": [CONFIG_NAME]}
    mask_absent = {CONFIG_NAME}
    if config_class is not None and config_class[0] == root.dir:
        requested["hidden"].append(config_class[1])
        mask_absent.add(config_class[1])

    for key in KEYS:
        entries = getattr(root, key)
        for p in entries:
            if not isinstance(p, str) or not p:
                die(f"invalid {key} entry for root {root.dir}: {p!r}")
            pp = PurePosixPath(p)
            if pp.is_absolute() or ".." in pp.parts:
                die(f"{key} path must be relative to its root: {p}")
            if names_root_itself(p):  # names the root itself
                # `read_only: ["."]` marks the whole root read-only (dropped
                # below); hiding the whole root is meaningless, so reject `.`
                # under `hidden`.
                if key == "read_only":
                    continue
                die(f"{key} path must not be the root itself: {p!r}")
            requested[key].append(p)
    if read_only_all:
        # The read-only root bind covers .git and every sub-path, so drop the
        # now-redundant per-path read_only mounts (`hidden` still masks the
        # content, which read-only does not). Skipping them also skips their
        # per-path symlink-escape check, which is moot: nothing per-path is bound.
        requested["read_only"] = []
    return requested, mask_absent


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


def _yaml_path(value: str) -> str:
    """A host/container path as a YAML scalar for the override document.

    json.dumps gives a safely-quoted string (handles spaces, quotes, control
    chars); doubling `$` to `$$` stops docker compose from interpolating a
    literal dollar in the path — compose interpolates every -f file it merges
    (the piped override included) and `$$` is its escape for a literal `$`.
    """
    return json.dumps(value, ensure_ascii=False).replace("$", "$$")


def _bind_stanza(source: str, target: str, read_only: bool) -> "list[str]":
    """A docker compose bind-mount stanza, read-write unless `read_only`."""
    lines = [
        "      - type: bind",
        f"        source: {_yaml_path(source)}",
        f"        target: {_yaml_path(target)}",
    ]
    if read_only:
        lines.append("        read_only: true")
    lines += [
        "        bind:",
        "          create_host_path: false",
    ]
    return lines


def _root_bind(root_dir: str, read_only: bool = False) -> "list[str]":
    """The bind that mounts a jail root into the container.

    Read-write by default; read-only when the root asked for it (a `read_only`
    entry naming the root itself), making the whole root immutable while its
    `hidden` masks still apply.
    """
    return _bind_stanza(root_dir, container_path(root_dir),
                        read_only=read_only)


def _mask_volumes(mounts: "list[tuple[str, str]]", root_dir: str,
                  mask_absent: "set[str]", root_read_only: bool = False
                  ) -> "tuple[list[str], list[str]]":
    """Render one root's read_only/hidden mounts as compose volume entries.

    Returns (lines, seeds). A hidden path in `mask_absent` (the config-protection
    paths) is masked even when it does not exist on the host; any other missing
    hidden path is a hard error (almost always a typo, and skipping it would
    leave a secret unmasked). `seeds` lists the host paths of those absent
    config-protection files: the caller seeds each with `{}` before compose runs
    (see seeds note in the hidden branch).

    When `root_read_only` (the whole root is bound read-only), an *absent*
    config-protection path is left alone rather than masked-and-seeded: the
    read-only bind already stops the agent from planting a config there, and
    docker cannot materialise a mask target inside a read-only bind anyway.
    """
    volumes: "list[str]" = []
    seeds: "list[str]" = []
    for rel, mode in mounts:
        # Refuse a path that escapes the root via a symlink before we bind it;
        # otherwise a `read_only` symlink could expose a host file to the agent
        # and a `hidden` one could mask the wrong target. (Absolute / `..` were
        # already rejected at parse time.)
        resolve_in_root(root_dir, rel, f"{mode} path")
        source = f"{root_dir}/{rel}"
        target = container_path(f"{root_dir}/{rel}")
        host = Path(source)
        if mode == "read_only":
            if not host.exists():
                # Nothing on the host to protect; create_host_path is off.
                continue
            volumes += _bind_stanza(source, target, read_only=True)
            continue
        # hidden
        if host.is_dir():
            # A fresh anonymous read-only volume masks the directory's contents
            # and stays unwritable. nocopy keeps it empty; anonymous (no source)
            # so each mask is isolated and nothing persists across jails or
            # runs. (A tmpfs can't be made read-only through compose, and
            # tmpfs-mode is ignored when mounted over an existing directory.)
            volumes += [
                "      - type: volume",
                f"        target: {_yaml_path(target)}",
                "        read_only: true",
                "        volume:",
                "          nocopy: true",
            ]
        elif host.exists() or rel in mask_absent:
            # A volume can't mount over a single file, so bind an empty file
            # (shipped in the claude-jail repo) over it. Being a regular file on
            # a read-only mount, writes to a hidden file are refused with EROFS
            # rather than silently swallowed the way /dev/null swallowed them.
            # A config-protection path is masked even while absent, so the agent
            # cannot create it.
            if not host.exists():
                if root_read_only:
                    # The read-only root bind already stops the agent from
                    # planting this config, and docker can't materialise a mask
                    # target inside a read-only bind — nothing to mask or seed.
                    continue
                # The root is a rw bind, so docker materialises this read-only
                # mask's bind target as a file in the real directory. Left as the
                # 0-byte file docker creates, it is invalid JSON that makes the
                # next run from here die in read_config; seeding it with `{}`
                # first leaves a valid, default config behind instead.
                seeds.append(source)
            volumes += _bind_stanza(str(SCRIPT_DIR / EMPTY_MASK), target,
                                    read_only=True)
        else:
            die(f"hidden path not found in the jail: {rel}")
    return volumes, seeds


def seed_masked_configs(paths: "list[str]") -> None:
    """Best-effort seed of each absent, about-to-be-masked config with `{}`.

    Masking an absent config makes docker materialise the bind target in the
    rw-bound root, i.e. in the real host directory. Pre-seeding `{}` leaves a
    valid, default config there instead of the 0-byte file docker would
    otherwise create. This is only a courtesy on top of docker's own
    materialisation: when the seed can't be written (an unwritable root, a
    missing parent dir) docker still creates the target itself, so we skip
    rather than abort an otherwise-fine launch. Idempotent: only writes what is
    still missing.
    """
    for path in paths:
        p = Path(path)
        if p.exists():
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}\n")
        except OSError:
            continue  # docker materialises the target; the seed is optional


def override(roots: "list[Root]", config_class: "tuple[str, str] | None"
             ) -> "tuple[str, list[str]]":
    """Build the docker compose volume override: a per-root bind + masks.

    Each root binds read-write, or read-only when a `read_only` entry names the
    root itself. Returns (document, seeds). Side-effect-free: before launching a
    container the caller must ensure_empty_mask() (the shipped empty file the
    hidden-file masks bind) and seed_masked_configs(seeds) (the absent
    config-protection files), keeping host writes out of the validation step.
    """
    volumes: "list[str]" = []
    seeds: "list[str]" = []
    for root in roots:
        ro_all = root.read_only_all()
        volumes += _root_bind(root.dir, read_only=ro_all)
        requested, mask_absent = requested_for_root(root, config_class, ro_all)
        tree = build_tree(requested)
        mounts: "list[tuple[str, str]]" = []
        resolve(tree, [], False, mounts)
        lines, root_seeds = _mask_volumes(mounts, root.dir, mask_absent, ro_all)
        volumes += lines
        seeds += root_seeds
    if not volumes:
        return "", []
    out: "list[str]" = ["services:", "  claude:", "    volumes:"]
    out += volumes
    return "\n".join(out) + "\n", seeds
