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

# The repo metadata dir the review feature shadows; the single subtree isolation
# owns. Named once so the special-casing in requested_for_root / override / the
# isolation mounts reads from one place instead of scattering the literal.
GITDIR = ".git"

# Other popular VCS metadata dirs protected read-only on every root. Unlike .git,
# git-review gives them no writable shadow, so they stay read-only even in isolate
# mode (see requested_for_root) — a review run must not silently un-protect them.
OTHER_VCS_READ_ONLY = [".hg", ".svn"]

# Built-in read_only policy applied to every root.
DEFAULT_READ_ONLY = [GITDIR, *OTHER_VCS_READ_ONLY]

# Container mount points for git-review isolation (see override()). The real
# repo's .git is exposed read-only under VCS_GIT_PREFIX + <host path> (mirroring
# container_path's /workspace + <host path>), and the host spool — where the
# session drops its bundle — is bound at VCS_SPOOL.
VCS_GIT_PREFIX = "/vcs/git"
VCS_SPOOL = "/vcs/spool"

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


def under_gitdir(rel: str) -> bool:
    """True when a root-relative path is .git or something inside it.

    Normalizes first (PurePosixPath), so every spelling of the .git dir — `.git`,
    `.git/`, `.git/.`, `./.git`, `.git/config` — is recognised the same way. The
    single predicate the isolate-mode mask machinery shares, so a non-canonical
    spelling can't be handled one way here and another way there.
    """
    return PurePosixPath(rel).parts[:1] == (GITDIR,)


def is_gitdir(rel: str) -> bool:
    """True when a root-relative path is exactly the .git directory itself.

    Any spelling (`.git`, `.git/`, `.git/.`, `.git/./`, `./.git`) normalises here.
    The launcher's whole-.git-hidden refusal uses this instead of a raw string
    compare, so a non-canonical spelling can't slip past the guard and leave the
    real .git exposed read-only at /vcs/git to the sandboxed agent.
    """
    return PurePosixPath(rel).parts == (GITDIR,)


def masked_rels(root: Root, config_class: "tuple[str, str] | None",
                isolate: bool = False) -> "list[str]":
    """The rels masked (emptied) over `root`'s live working tree.

    The always-masked config name, the active config when it sits in this root,
    and the root's `hidden` entries — minus those under .git in isolate mode,
    which override() re-masks over the read-only original at /vcs/git rather than
    over the writable shadow. Delegates to requested_for_root so this is literally
    the same `hidden` list the mounts are built from (the launcher hands it to
    review.finalize, whose clobber warning must name exactly the masked files),
    rather than a parallel re-derivation of the rule that could drift.
    """
    return requested_for_root(root, config_class, isolate=isolate)[0]["hidden"]


def requested_for_root(root: Root, config_class: "tuple[str, str] | None",
                       read_only_all: bool = False, isolate: bool = False
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

    `isolate` (git-review) drops the built-in read-only .git mount: override()
    replaces it with a writable .git volume and a read-only original elsewhere,
    so the default would only fight that overmount. The other VCS dirs
    (OTHER_VCS_READ_ONLY) get no such shadow, so they stay read-only under review.
    """
    default_ro = list(OTHER_VCS_READ_ONLY) if isolate else list(DEFAULT_READ_ONLY)
    requested = {"read_only": default_ro, "hidden": [CONFIG_NAME]}
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
            if isolate and under_gitdir(p):
                # In review mode /workspace<root>/.git is the throwaway writable
                # shadow clone; a read_only/hidden mask inside it would make
                # `git init`/commit hit EROFS and abort the session. Drop it here so
                # the overmount stays writable — override() re-applies the `hidden`
                # ones over the read-only original at /vcs/git instead (see
                # _vcs_git_masks), so a masked secret in .git stays hidden there too.
                continue
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


def original_git_path(root_dir: str) -> str:
    """Container path where a root's real .git is exposed read-only for git-review.

    The VCS_GIT_PREFIX mirror of container_path. Shared by the isolation mount
    (_isolation_volumes) and the launcher's JAIL_GIT_ORIGINAL so the bind target
    and the in-jail alternate can never drift apart.
    """
    return VCS_GIT_PREFIX + root_dir


def _isolation_volumes(root_dir: str, gitdir: str) -> "list[str]":
    """The git-review mounts for one isolated root.

    A throwaway writable .git (`gitdir`, a user-owned host dir the launcher wipes
    each run) shadows the host's real .git inside the live root bind, so the
    agent's commits land there and never touch real history. A host bind, not an
    anonymous volume, so it is writable by the unprivileged container user and its
    per-run reset is what keeps it from drifting. The real .git is bound read-only
    under VCS_GIT_PREFIX so the entrypoint can reference-clone from it (and host
    commits made mid-session stay visible through it). The working tree itself
    stays the read-write root bind override() already emitted.
    """
    git_target = container_path(root_dir) + "/" + GITDIR
    lines = _bind_stanza(gitdir, git_target, read_only=False)
    lines += _bind_stanza(f"{root_dir}/{GITDIR}", original_git_path(root_dir),
                          read_only=True)
    return lines


def _mask_dir_stanza(target: str) -> "list[str]":
    """A read-only anonymous volume masking a directory's contents (nocopy so it
    stays empty). Shared by _mask_volumes and _vcs_git_masks so the two directory
    masks can't drift apart."""
    return [
        "      - type: volume",
        f"        target: {_yaml_path(target)}",
        "        read_only: true",
        "        volume:",
        "          nocopy: true",
    ]


def _mask_present_stanza(host: "Path", target: str) -> "list[str]":
    """Mask an existing host path at `target`: a nocopy read-only volume for a
    directory, an empty-file bind for a regular file.

    The single dir-vs-file mask decision, shared by _mask_volumes (masking over
    the live working tree) and _vcs_git_masks (re-masking over the read-only
    /vcs/git original), so the two mask pipelines can't drift — a drift risks
    leaving a `hidden` .git secret readable through /vcs/git in review mode.
    """
    if host.is_dir():
        return _mask_dir_stanza(target)
    return _bind_stanza(str(SCRIPT_DIR / EMPTY_MASK), target, read_only=True)


def _vcs_git_masks(root: Root) -> "list[str]":
    """Re-apply the root's `hidden` masks that fall under .git over /vcs/git.

    In isolate mode requested_for_root drops every mask under .git (the live .git
    overmount must stay writable for `git init`/commit), but _isolation_volumes
    then exposes the real .git read-only at /vcs/git. Without this, a secret the
    user masked with e.g. `hidden: [".git/config"]` (a credential-helper token, a
    remote URL with an embedded token) would be readable through /vcs/git the
    moment review is on — a confidentiality regression versus a normal run. So
    mask those same `hidden` paths over the original here. `read_only` entries
    need no mask: /vcs/git is bound read-only wholesale.
    """
    lines: "list[str]" = []
    for p in root.hidden:
        pp = PurePosixPath(p)
        if not under_gitdir(p) or is_gitdir(p):
            continue  # not under .git (or the .git dir itself — nothing to re-mask)
        # Same symlink-escape guard _mask_volumes applies before binding.
        resolve_in_root(root.dir, p, "hidden path")
        rel_in_git = "/".join(pp.parts[1:])             # path inside the .git dir
        host = Path(root.dir) / GITDIR / rel_in_git     # the real .git content
        target = original_git_path(root.dir) + "/" + rel_in_git
        if host.exists():
            lines += _mask_present_stanza(host, target)
        else:
            # Matches _mask_volumes' hard error for a missing `hidden` path.
            # requested_for_root dropped this .git sub-path before it could reach
            # that check, so enforce it here — otherwise a typo'd entry (e.g.
            # `.git/cfg` for `.git/config`) is silently ignored in review mode,
            # and review mode is exactly what newly exposes the real .git read-only
            # at /vcs/git: the secret the user meant to mask stays readable to the
            # sandboxed agent. Fail loudly, as a normal run would.
            die(f"hidden path not found in the jail: {p}")
    return lines


def _spool_bind(spool: str) -> "list[str]":
    """The read-write bind of the host review spool the session writes its bundle to."""
    return _bind_stanza(spool, VCS_SPOOL, read_only=False)


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
        # hidden. A directory mask is a fresh anonymous read-only volume (nocopy
        # keeps it empty, anonymous so nothing persists across jails/runs); a
        # regular file is masked by binding a shipped empty file (a volume can't
        # mount over a single file). Both refuse writes with EROFS rather than
        # swallowing them the way /dev/null did. (A tmpfs can't be made read-only
        # through compose, and tmpfs-mode is ignored when mounted over a dir.)
        if host.exists():
            volumes += _mask_present_stanza(host, target)
        elif rel in mask_absent:
            # A config-protection path is masked even while absent, so the agent
            # cannot create it.
            if root_read_only:
                # The read-only root bind already stops the agent from planting
                # this config, and docker can't materialise a mask target inside a
                # read-only bind — nothing to mask or seed.
                continue
            # The root is a rw bind, so docker materialises this read-only mask's
            # bind target as a file in the real directory. Left as the 0-byte file
            # docker creates, it is invalid JSON that makes the next run from here
            # die in read_config; seeding it with `{}` first leaves a valid,
            # default config behind instead.
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


def override(roots: "list[Root]", config_class: "tuple[str, str] | None",
             isolation: "dict | None" = None
             ) -> "tuple[str, list[str]]":
    """Build the docker compose volume override: a per-root bind + masks.

    Each root binds read-write, or read-only when a `read_only` entry names the
    root itself. Returns (document, seeds). Side-effect-free: before launching a
    container the caller must ensure_empty_mask() (the shipped empty file the
    hidden-file masks bind) and seed_masked_configs(seeds) (the absent
    config-protection files), keeping host writes out of the validation step.

    `isolation` (git-review, default off) is {"repo": <dir>, "gitdir": <host
    dir>, "spool": <host dir>}: that one root is mounted dev-container style —
    live working tree, .git shadowed by the throwaway writable `gitdir` (a clone
    of the read-only original) — and the host spool is bound so the session can
    drop its bundle for review. (The launcher passes its review context here, so
    extra keys like `masked`/`base` may ride along and are ignored.)
    """
    isolate_root = isolation["repo"] if isolation else None
    volumes: "list[str]" = []
    seeds: "list[str]" = []
    for root in roots:
        isolate = root.dir == isolate_root
        ro_all = root.read_only_all()
        volumes += _root_bind(root.dir, read_only=ro_all)
        requested, mask_absent = requested_for_root(root, config_class, ro_all,
                                                    isolate=isolate)
        tree = build_tree(requested)
        mounts: "list[tuple[str, str]]" = []
        resolve(tree, [], False, mounts)
        lines, root_seeds = _mask_volumes(mounts, root.dir, mask_absent, ro_all)
        volumes += lines
        seeds += root_seeds
        if isolate:
            volumes += _isolation_volumes(root.dir, isolation["gitdir"])
            # Re-apply this root's `hidden` masks under .git over the read-only
            # original at /vcs/git (requested_for_root dropped them from the
            # writable shadow) so a masked .git secret stays hidden in review mode.
            volumes += _vcs_git_masks(root)
    if isolation:
        volumes += _spool_bind(isolation["spool"])
    if not volumes:
        return "", []
    out: "list[str]" = ["services:", "  claude:", "    volumes:"]
    out += volumes
    return "\n".join(out) + "\n", seeds
