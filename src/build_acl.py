"""Build the Squid egress ACL fragment for a claude-jail run from .claude-jail.json.

build_env.py owns the claude launch settings; this module owns the `egress` key —
the per-project network egress policy. Squid is an implementation detail: the
config speaks an abstract allow/deny vocabulary (a `default` policy plus an
`allowed`/`denied` host list with `*.` wildcards), and squid_rules() converts it
to a Squid http_access fragment so that syntax never leaks into the config. The
launcher exports the fragment as JAIL_SQUID_ACL; docker-compose.yml forwards it
into the squid container, whose entrypoint writes it to the file squid.conf
includes. die() (from jail_config) reports a malformed config.

Egress is default-deny: with no `egress` key at all, only Anthropic's domains
(*.anthropic.com and *.claude.com) are reachable. They are always allowed
regardless of the configured policy, so the jail can always reach the API.
"""
import re

from jail_config import die

# Anthropic's domains are always reachable: the jail exists to run claude, which
# talks to api.anthropic.com and related *.anthropic.com / *.claude.com subdomains.
# Allowed ahead of any deny rule (see _allow_fragment), so even a default-allow
# config that blacklists one of them keeps the API reachable. Each token's leading
# dot is Squid's apex+subdomain match.
ALWAYS_ALLOW = (".anthropic.com", ".claude.com")

EGRESS_KEYS = {"default", "allowed", "denied"}

# A host pattern is dot-joined DNS labels (1-63 chars, no leading/trailing
# hyphen), optionally prefixed with "*." for a subdomain wildcard. Matching this
# is the security boundary: a list entry can only ever become a dstdomain token,
# never smuggle whitespace, a newline, or an extra directive into squid.conf.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"\A{_LABEL}(?:\.{_LABEL})*\Z")


def squid_rules(data: "dict", config_file: str) -> str:
    """The Squid http_access fragment for the egress policy (always non-empty)."""
    egress = data.get("egress")
    if egress is None:
        return _deny_fragment([])  # default-deny: only ALWAYS_ALLOW is reachable
    if not isinstance(egress, dict):
        die(f"'egress' in {config_file} must be an object")
    unknown = set(egress) - EGRESS_KEYS
    if unknown:
        die(f"unknown key(s) in 'egress' in {config_file}: {sorted(unknown)}")
    default = egress.get("default", "deny")
    if default not in ("allow", "deny"):
        die(f"'egress.default' in {config_file} must be \"allow\" or \"deny\"; "
            f"got {default!r}")
    if default == "deny":
        if "denied" in egress:
            die(f"'egress' in {config_file}: with default \"deny\", list permitted "
                f"hosts under 'allowed' (a 'denied' list would have no effect)")
        return _deny_fragment(_host_tokens(egress, "allowed", config_file))
    if "allowed" in egress:
        die(f"'egress' in {config_file}: with default \"allow\", list blocked "
            f"hosts under 'denied' (an 'allowed' list would have no effect)")
    return _allow_fragment(_host_tokens(egress, "denied", config_file))


def _host_tokens(egress: "dict", key: str, config_file: str) -> "list[str]":
    """Validate egress[key] as a host list and convert it to dstdomain tokens."""
    value = egress.get(key, [])
    if not isinstance(value, list):
        die(f"'egress.{key}' in {config_file} must be an array of host patterns")
    return [_dstdomain_token(entry, config_file) for entry in value]


def _dstdomain_token(entry: object, config_file: str) -> str:
    """Validate one host pattern and return its Squid dstdomain token.

    `*.example.com` -> `.example.com` (Squid's leading dot matches the apex and
    every subdomain); a plain `example.com` stays exact.
    """
    if not isinstance(entry, str):
        die(f"egress host patterns in {config_file} must be strings; got {entry!r}")
    wildcard = entry.startswith("*.")
    host = entry[2:] if wildcard else entry
    if not _HOST.match(host):
        die(f"invalid egress host pattern in {config_file}: {entry!r} "
            f"(expected 'example.com' or '*.example.com')")
    return ("." + host) if wildcard else host


def _deny_fragment(allowed_tokens: "list[str]") -> str:
    """Allowlist fragment: only ALWAYS_ALLOW plus allowed_tokens get through."""
    tokens = list(dict.fromkeys(ALWAYS_ALLOW + tuple(allowed_tokens)))
    return "\n".join((
        f"acl jail_allow dstdomain {' '.join(tokens)}",
        "http_access allow localnet jail_allow",
        "http_access deny all",
    ))


def _allow_fragment(denied_tokens: "list[str]") -> str:
    """Denylist fragment: everything but denied_tokens passes; ALWAYS_ALLOW wins.

    With nothing denied this is plain allow-localnet (ALWAYS_ALLOW is reachable
    anyway). Otherwise ALWAYS_ALLOW is matched first so a denied wildcard that
    happens to cover it cannot block the API.
    """
    if not denied_tokens:
        return "\n".join((
            "http_access allow localnet",
            "http_access deny all",
        ))
    return "\n".join((
        f"acl jail_anthropic dstdomain {' '.join(ALWAYS_ALLOW)}",
        f"acl jail_deny dstdomain {' '.join(denied_tokens)}",
        "http_access allow localnet jail_anthropic",
        "http_access deny localnet jail_deny",
        "http_access allow localnet",
        "http_access deny all",
    ))
