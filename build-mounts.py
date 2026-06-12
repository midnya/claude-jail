#!/usr/bin/env python3
"""Build the docker compose override (volumes + configs) for a claude-jail run.

Usage: build-mounts.py <jail-dir> [config-file]

Combines the built-in mount policy with an optional .claude-jail.json config,
resolves nesting and precedence into a mount tree (hidden always trumps
read_only), and prints a docker compose override document to stdout. Prints
nothing when there are no mounts. Exits non-zero with a message on stderr if
the config is malformed, a requested path is unsafe, or a hidden path is
missing on the host.

Hidden directories are masked with an empty read-only volume; hidden files with
a read-only bind of an empty file shipped in the claude-jail repo (a volume
can't mount over a single file). Both read as empty; because the file mask is a
regular file on a read-only mount, writes to a hidden file are refused (EROFS)
rather than silently discarded as they were when masked with /dev/null. The
mask file lives outside the jail directory, so the sandboxed agent has no
writable path to it and cannot un-empty the masks.
"""
import json
import sys
from pathlib import Path, PurePosixPath

# Top-level array keys recognised in .claude-jail.json. Each holds a list of
# jail-relative paths.
#   read_only — bind-mounted read-only; visible but not writable.
#   hidden    — content masked (empty) inside the container.
KEYS = ("read_only", "hidden")

# Built-in policy applied to every jail. The config file itself is protected so
# the sandboxed agent cannot rewrite its own jail rules.
DEFAULTS = {
    "read_only": [".git", ".claude-jail.json"],
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


def die(msg: str) -> "None":
    sys.exit(f"Error: {msg}")


def load_requested(config_file: "str | None") -> "dict[str, list[str]]":
    """Merge the built-in defaults with the config file's path lists."""
    requested = {key: list(DEFAULTS[key]) for key in KEYS}
    if not config_file or not Path(config_file).is_file():
        return requested

    try:
        data = json.loads(Path(config_file).read_text())
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {config_file}: {e}")
    if not isinstance(data, dict):
        die(f"{config_file} must contain a JSON object")

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
    """Render the resolved mounts as a docker compose override document."""
    volumes: "list[str]" = []
    need_empty = False
    for rel, mode in mounts:
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
            need_empty = True
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
    if need_empty:
        # The mask source must exist before compose runs (create_host_path is
        # off). It sits outside the jail, so no in-container path can write it.
        ensure_empty_mask()
    out: "list[str]" = ["services:", "  claude-jail:", "    volumes:"]
    out += volumes
    return "\n".join(out) + "\n"


def main() -> None:
    if not 2 <= len(sys.argv) <= 3:
        sys.exit(f"Usage: {sys.argv[0]} <jail-dir> [config-file]")
    jail_dir = sys.argv[1].rstrip("/")
    config_file = sys.argv[2] if len(sys.argv) == 3 else None

    requested = load_requested(config_file)
    tree = build_tree(requested)
    mounts: "list[tuple[str, str]]" = []
    resolve(tree, [], False, mounts)
    sys.stdout.write(render(mounts, jail_dir))


if __name__ == "__main__":
    main()
