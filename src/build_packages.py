"""Resolve the extra packages for a claude-jail run from .claude-jail.json.

build_env.py owns the claude launch settings; this module owns the `packages`
key — an object grouping packages to install into the jail image by manager. Its
`apt` list names Debian packages added on top of the fixed set baked into
docker/claude/Dockerfile (the object leaves room for other managers later). They
must be installed at *build* time: a running jail's only egress is the default-deny
Squid proxy, which would refuse the Debian mirrors, so a startup `apt-get install`
cannot work. The launcher turns the parsed list into two exports —
JAIL_APT_PACKAGES (the install arg) and JAIL_IMAGE_SUFFIX (a content digest folded
into the image tag) — and docker-compose.yml forwards them as the build arg and
image name. die() (from jail_config) reports a malformed config.

The image tag is content-addressed on the package set (claude-jail-<digest>, or
plain claude-jail when empty) so two projects with different lists never share —
and so never clobber — one image, the same way egress folds into the proxy's
identity. A new or changed list is a new tag that compose builds on first use;
the previous tag is left behind as an orphan (the launcher's `prune` command
reclaims them), the same accepted cost as the egress-keyed volumes.

Matching a package name is the security boundary: an entry can only ever become a
bare token on the `apt-get install` line and a fragment of an image tag, never
smuggle whitespace, a shell metacharacter, or a leading-dash apt flag. The pattern
is the Debian package-name charset — lowercase letters, digits, '+', '-', '.',
starting with an alphanumeric (so never a flag) and at least two characters long.
"""
import re

from jail_config import die, short_digest

_PACKAGE = re.compile(r"\A[a-z0-9][a-z0-9+.-]+\Z")

# Managers recognised inside the `packages` object. Only `apt` so far; rejecting
# any other key turns a typo (or a not-yet-supported manager) into a hard error
# rather than a silently-ignored install.
_PACKAGES_KEYS = {"apt"}


def parse(data: "dict", config_file: str) -> "list[str]":
    """The validated apt package names from `packages.apt`, sorted and deduped ([] when unset)."""
    packages = data.get("packages")
    if packages is None:
        return []
    if not isinstance(packages, dict):
        die(f"'packages' in {config_file} must be an object")
    unknown = set(packages) - _PACKAGES_KEYS
    if unknown:
        die(f"unknown key(s) in 'packages' in {config_file}: {sorted(unknown)}")
    value = packages.get("apt")
    if value is None:
        return []
    if not isinstance(value, list):
        die(f"'packages.apt' in {config_file} must be an array of package names")
    for entry in value:
        if not isinstance(entry, str) or not _PACKAGE.match(entry):
            die(f"'packages.apt' in {config_file}: {entry!r} is not a valid "
                f"Debian package name (lowercase letters, digits, '+', '-', '.', "
                f"starting with a letter or digit)")
    return sorted(set(value))


def build_arg(packages: "list[str]") -> str:
    """The JAIL_APT_PACKAGES value: package names joined for the apt-get line."""
    return " ".join(packages)


def image_suffix(packages: "list[str]") -> str:
    """The image-tag suffix for a package set: '' when empty, else '-<8 hex>'.

    Content-addressed so the tag (and thus the built image) changes only when the
    package set does, never with order or duplicates — parse() already canonicalises
    the list. Shares the launcher's short_digest, so the egress id and this tag
    can't drift to different digest shapes.
    """
    if not packages:
        return ""
    return "-" + short_digest("\n".join(packages))
