"""Resolve the extra packages for a claude-jail run from .claude-jail.json.

build_env.py owns the claude launch settings; this module owns the `packages`
key — an object grouping packages to install into the jail image by manager. Its
`apt` list names Debian packages added on top of the fixed set baked into
docker/claude/Dockerfile, and its `pip` list names Python distributions
installed into a venv baked into the same image. Both must be installed at
*build* time: a running jail's only egress is the default-deny Squid proxy, which
would refuse the Debian mirrors and PyPI, so a startup `apt-get`/`pip install`
cannot work. The launcher turns the parsed lists into three exports —
JAIL_APT_PACKAGES and JAIL_PIP_PACKAGES (the install args) and JAIL_IMAGE_SUFFIX
(a content digest folded into the image tag) — and docker-compose.yml forwards
them as build args and the image name. die() (from jail_config) reports a
malformed config.

The image tag is content-addressed on the build inputs (claude-jail-<digest>, or
plain claude-jail for the baseline) so two projects whose images would differ
never share — and so never clobber — one image, the same way egress folds into
the proxy's identity. The inputs are the package sets and the container user's
uid/gid (resolve_ids.py), both baked in at build time; image_suffix() takes both.
A new or changed input is a new tag that compose builds on first use; the previous
tag is left behind as an orphan (the launcher's `prune` command reclaims them),
the same accepted cost as the egress-keyed volumes.

Matching a package name is the security boundary: an entry can only ever become a
bare token on the install line (or one line of a generated pip requirements
file), never smuggle whitespace, a shell metacharacter, or a leading-dash flag.
Each manager is one row in _MANAGERS, carrying its validation pattern, the
normaliser that canonicalises an entry before dedup, and a human-readable
description of the shape it accepts — quoted back in the die() message so a
rejected entry is told what was expected, not merely that it failed. A new
manager's row folds into parse(), the Packages fields and the image digest
automatically; its install wiring (a JAIL_*_PACKAGES launcher export, a
docker-compose build arg, a Dockerfile install step) and its line in
build_prompt.py's packages_segment are per-manager and have to be added alongside
it. The patterns are security filters, not full validators — each is a
permissive superset of valid specs, so a malformed-but-safe entry passes here and
is caught later by the package manager at build time, not by die() at parse time.

The shape each manager accepts:
  - apt: the Debian package-name charset — lowercase letters, digits, '+', '-',
    '.', starting with an alphanumeric (so never a flag) and at least two
    characters long.
  - pip: a PyPI name with optional extras ('[standard]') and version specifiers
    ('==1.0', '<5', '~=2.0', '2.*', comma-separated ranges) — letters (any
    case), digits and '. _ + ! ~ < > = * , [ ]'. It excludes whitespace, every
    shell metacharacter, and '/', ':', '@', ';', so a URL or VCS install
    (git+https://…, pkg @ https://…) and an environment marker
    (pkg; python_version<'3') are all rejected. pip names are case-insensitive,
    so normalize lowercases the entry — two spellings of one distribution dedupe
    and content-address to one image. The leading-alphanumeric rule means an
    entry can never be read as a pip flag, nor as a '-r'/'--index-url' option
    line inside the requirements file.
"""
import re
from collections import namedtuple

from jail_config import die, short_digest

# A recognised package manager: the pattern an entry must fully match, the
# normaliser applied before the set/sort so equivalent spellings collapse to one
# (identity for apt — its charset is already lowercase; str.lower for pip, whose
# names are case-insensitive), and a human-readable description of the accepted
# shape, quoted back to the user when an entry is rejected. Rejecting any key not
# named here turns a typo (or a not-yet-supported manager) into a hard error
# rather than a silently-ignored install.
Manager = namedtuple("Manager", ["pattern", "normalize", "accepts"])

_MANAGERS = {
    "apt": Manager(
        pattern=re.compile(r"\A[a-z0-9][a-z0-9+.-]+\Z"),
        normalize=str,
        accepts=("a Debian package name: lowercase letters, digits, '+', '-' "
                 "and '.', starting with a letter or digit, at least two "
                 "characters"),
    ),
    "pip": Manager(
        pattern=re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+!~<>=*,\[\]-]*\Z"),
        normalize=str.lower,
        accepts=("a PyPI requirement: a name with optional extras and version "
                 "specifiers (e.g. 'requests', 'django<5', 'uvicorn[standard]', "
                 "'numpy==1.26.*'); no whitespace, URLs, VCS refs or "
                 "environment markers"),
    ),
}

# The parsed, validated package sets: one list field per manager (in _MANAGERS
# order), each sorted, deduped and normalized. Deriving the fields from _MANAGERS
# keeps the data model in lockstep with the registry, so a new manager extends
# the tuple — and the image digest, which iterates _fields — for free.
Packages = namedtuple("Packages", list(_MANAGERS))


def empty() -> "Packages":
    """A Packages with every manager's list empty (each its own fresh list)."""
    return Packages(**{key: [] for key in _MANAGERS})


def _manager_list(packages: "dict", key: str, config_file: str) -> "list[str]":
    """The validated `packages.<key>` entries, normalized, sorted and deduped.

    [] when the key is unset. A rejected entry dies naming the manager's accepted
    shape, so the message says what was expected, not merely that it failed.
    """
    value = packages.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        die(f"'packages.{key}' in {config_file} must be an array of package names")
    manager = _MANAGERS[key]
    for entry in value:
        if not isinstance(entry, str) or not manager.pattern.match(entry):
            die(f"'packages.{key}' in {config_file}: {entry!r} is not a valid "
                f"{key} entry — expected {manager.accepts}")
    return sorted({manager.normalize(entry) for entry in value})


def parse(data: "dict", config_file: str) -> "Packages":
    """The validated package sets from `packages` (empty lists when unset)."""
    packages = data.get("packages")
    if packages is None:
        return empty()
    if not isinstance(packages, dict):
        die(f"'packages' in {config_file} must be an object")
    unknown = set(packages) - set(_MANAGERS)
    if unknown:
        die(f"unknown key(s) in 'packages' in {config_file}: {sorted(unknown)}")
    return Packages(**{key: _manager_list(packages, key, config_file)
                       for key in _MANAGERS})


def image_suffix(packages: "Packages", ids: "Ids | None" = None) -> str:
    """The image-tag suffix for the build inputs: '' for the baseline, else '-<8 hex>'.

    Content-addressed so the tag (and thus the built image) changes only when a
    build input does, never with order or duplicates — parse() already
    canonicalises the package lists. The two inputs are the extra packages and
    the container user's numeric identity (resolve_ids.Ids), both baked into the
    image at build time; `ids` is None for the default user (no fold), so an
    all-default config — no packages, default uid/gid — maps to the bare
    `claude-jail` base tag. A non-default uid/gid appends its own labeled section
    (Ids.digest_key), keeping a differing user on its own image; the default-user
    digest is left untouched so an existing package set keeps its tag. The
    package key is built from the namedtuple's own fields, each manager prefixed
    by its name, so a new manager folds in automatically and an entry can never
    collide across managers or with the uid/gid section. Shares the launcher's
    short_digest, so the egress id and this tag can't drift to different digest
    shapes.
    """
    ids_default = ids is None or ids.is_default()
    if not any(packages) and ids_default:
        return ""
    key = "\n".join(f"{manager}\n" + "\n".join(getattr(packages, manager))
                    for manager in packages._fields)
    if not ids_default:
        key += "\n" + ids.digest_key()
    return "-" + short_digest(key)
