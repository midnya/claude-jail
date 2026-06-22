"""Build the egress ACL fragments for a claude-jail run from .claude-jail.json.

build_env.py owns the claude launch settings; this module owns the `egress` key —
the per-project network egress policy. The config speaks an abstract allow/deny
vocabulary (a `default` policy plus an `allowed`/`denied` list of domains and
IP/CIDRs); this module renders it to two enforcement layers so neither tool's
syntax leaks into the config:
  - squid_rules() -> a Squid http_access fragment (the L7 HTTP allowlist).
  - dns_rules()   -> an Unbound local-zone fragment (the L3 DNS allowlist that
    refuses non-allowlisted names one query earlier, closing the DNS-tunnel
    exfil channel an HTTP proxy can't see, and logging every lookup).
The launcher exports them as JAIL_SQUID_ACL / JAIL_DNS_ACL; docker-compose.yml
forwards each into its side container, whose entrypoint writes it to the file the
base config includes. die() (from jail_config) reports a malformed config.

Egress is default-deny: with no `egress` key at all, only Anthropic's domains
(*.anthropic.com and *.claude.com) are reachable. They are always allowed
regardless of the configured policy, so the jail can always reach the API.

A host pattern is either a domain or a CIDR:
  - A domain is a registrable name of two or more DNS labels (an optional "*."
    prefix is accepted as a synonym). It matches the domain AND every subdomain
    (Squid's leading-dot dstdomain), so `example.com` also covers
    `api.example.com`. A bare TLD or single label (`com`, `localhost`) is
    rejected, so a pattern can never open a whole TLD.
  - A CIDR (IPv4 or IPv6, e.g. `1.2.3.4/32`, `10.0.0.0/8`) matches that address
    or range, via Squid's `dst`. The prefix must be explicit, the host bits
    clear, and the prefix narrower than /0, so a bare IP or a typo'd prefix is
    rejected rather than silently widened.
"""
import ipaddress
import re

from jail_config import die

# Anthropic's domains are always reachable: the jail exists to run claude, which
# talks to api.anthropic.com and related *.anthropic.com / *.claude.com subdomains.
# Allowed ahead of any deny rule (see _allow_fragment), so even a default-allow
# config that blacklists one of them keeps the API reachable. Each token's leading
# dot is Squid's apex+subdomain match.
ALWAYS_ALLOW = (".anthropic.com", ".claude.com")

EGRESS_KEYS = {"default", "allowed", "denied"}

# A domain is two or more dot-joined DNS labels (1-63 chars, no leading/trailing
# hyphen); requiring >=2 labels keeps a pattern from ever being a bare TLD. An
# optional "*." prefix is stripped before matching. Matching this is the security
# boundary: a list entry can only ever become a dstdomain/dst token, never smuggle
# whitespace, a newline, or an extra directive into squid.conf.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = re.compile(rf"\A{_LABEL}(?:\.{_LABEL})+\Z")


def render(data: "dict", config_file: str) -> "tuple[str, str, str]":
    """Resolve the egress policy once and render every artifact a launch needs.

    Returns (squid_acl, dns_acl, policy_key). A launch needs all three, and each
    otherwise re-runs _resolve's parse + host-pattern classification over the same
    config; resolving a single time here pays that cost once.
    """
    resolved = _resolve(data, config_file)
    return _squid_rules(resolved), _dns_rules(resolved), _policy_key(resolved)


def squid_rules(data: "dict", config_file: str) -> str:
    """The Squid http_access fragment for the egress policy (always non-empty)."""
    return _squid_rules(_resolve(data, config_file))


def _squid_rules(resolved: "tuple[str, list, list]") -> str:
    default, domains, ips = resolved
    if default == "deny":
        return _deny_fragment(domains, ips)
    return _allow_fragment(domains, ips)


def dns_rules(data: "dict", config_file: str) -> str:
    """The Unbound local-zone fragment for the egress policy's DNS layer.

    Gates the SAME allowlist as squid_rules, one query earlier: default-deny
    refuses every name outside ALWAYS_ALLOW plus the allowed domains; default-allow
    resolves everything and refuses the denied domains. Only domain tokens apply —
    a CIDR is an address, not a name, so IP allow/deny stays Squid's `dst` job and
    never appears here. Rendered into the file unbound.conf includes via
    JAIL_DNS_ACL; may be empty (default-allow with nothing denied), which leaves
    the resolver fully recursive.
    """
    return _dns_rules(_resolve(data, config_file))


def _dns_rules(resolved: "tuple[str, list, list]") -> str:
    default, domains, _ips = resolved
    if default == "deny":
        return _dns_deny_fragment(domains)
    return _dns_allow_fragment(domains)


def policy_key(data: "dict", config_file: str) -> str:
    """A canonical, render-independent digest of the egress policy.

    Configs that resolve to the same policy map to the same string — {} and
    {"egress": {"allowed": []}}, or two lists differing only in order or
    duplicates — while genuinely different policies map to different strings.
    Unlike the rendered Squid fragment, this does NOT change when squid_rules'
    output *formatting* does, so the launcher can fold it into the shared proxy's
    identity without that identity churning across cosmetic refactors of the
    fragment (which would orphan running proxies and their logs volumes).
    """
    return _policy_key(_resolve(data, config_file))


def _policy_key(resolved: "tuple[str, list, list]") -> str:
    default, domains, ips = resolved
    return "\n".join([default, *sorted(set(domains)), "", *sorted(set(ips))])


def _resolve(data: "dict", config_file: str) -> "tuple[str, list[str], list[str]]":
    """Validate the `egress` config and resolve it to (default, domains, ips).

    domains/ips are the Squid dstdomain/dst tokens for the configured list — the
    `allowed` list under default-deny, the `denied` list under default-allow.
    """
    egress = data.get("egress")
    if egress is None:
        return "deny", [], []  # default-deny: only ALWAYS_ALLOW is reachable
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
        return "deny", *_split_tokens(egress, "allowed", config_file)
    if "allowed" in egress:
        die(f"'egress' in {config_file}: with default \"allow\", list blocked "
            f"hosts under 'denied' (an 'allowed' list would have no effect)")
    return "allow", *_split_tokens(egress, "denied", config_file)


def _split_tokens(egress: "dict", key: str,
                  config_file: str) -> "tuple[list[str], list[str]]":
    """Validate egress[key] as a host list, split into (domain, ip) Squid tokens."""
    value = egress.get(key, [])
    if not isinstance(value, list):
        die(f"'egress.{key}' in {config_file} must be an array of host patterns")
    domains: "list[str]" = []
    ips: "list[str]" = []
    for entry in value:
        is_ip, token = _classify(entry, config_file)
        (ips if is_ip else domains).append(token)
    return domains, ips


def _classify(entry: object, config_file: str) -> "tuple[bool, str]":
    """Validate one host pattern; return (is_ip, token).

    A CIDR with an explicit prefix (`1.2.3.4/32`, `10.0.0.0/8`, IPv6) becomes a
    Squid `dst` token; a domain becomes a leading-dot `dstdomain` token matching
    the apex and every subdomain. A `*.` prefix on a domain is an accepted
    synonym. A bare IP without a prefix, `*.` on an IP/CIDR, a bare TLD/single
    label, a CIDR with host bits set or a /0, or anything else is rejected.
    """
    if not isinstance(entry, str):
        die(f"egress host patterns in {config_file} must be strings; got {entry!r}")
    wildcard = entry.startswith("*.")
    host = entry[2:] if wildcard else entry
    cidr = _cidr_token(host)
    if cidr is not None or _is_ip_address(host):
        if wildcard:
            die(f"egress host pattern in {config_file} applies '*.' to an IP "
                f"address: {entry!r}")
        if cidr is None:
            die(f"egress IP pattern in {config_file} must be a CIDR with an "
                f"explicit prefix length (e.g. '1.2.3.4/32' or "
                f"'2606:4700::/32'): {entry!r}")
        return (True, cidr)
    if not _DOMAIN.match(host):
        die(f"invalid egress host pattern in {config_file}: {entry!r} (expected a "
            f"domain like 'example.com' / '*.example.com', or a CIDR like "
            f"'1.2.3.4/32' / '10.0.0.0/8')")
    return (False, "." + host)


def parse_cidr(value: str) -> "ipaddress.IPv4Network | ipaddress.IPv6Network | None":
    """ip_network(value, strict=True), or None when value isn't a clean CIDR.

    strict=True is the teeth — a typo'd or over-broad prefix or host bits set
    (`1.2.3.4/24`) fails here instead of being silently canonicalized to a wider
    range than written. This owns only the strict parse; the caller decides what
    to make of the result (a /0, the address family, a minimum size), so the
    egress ACL and the launcher's --subnet check share one notion of a
    well-formed network rather than each spelling out its own try/except.
    """
    try:
        return ipaddress.ip_network(value, strict=True)
    except ValueError:
        return None


def _cidr_token(value: str) -> "str | None":
    """Canonical Squid `dst` token for an explicit, well-formed CIDR, else None.

    None means "not an explicit CIDR" so the caller rejects it: no `/prefix`,
    host bits set (`1.2.3.4/24`), a /0 that would cover everything, or simply not
    an address.
    """
    if "/" not in value:
        return None
    network = parse_cidr(value)
    if network is None or network.prefixlen == 0:
        return None
    return str(network)


def _is_ip_address(value: str) -> bool:
    """True when value is a bare IP address with no prefix (IPv4 or IPv6)."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _group(acl_name: str, kind: str, tokens: "list[str]",
           verb: str) -> "tuple[str, str] | tuple[()]":
    """An (acl-definition, http_access) line pair for a token group, or () when empty.

    Pairing the definition with its reference means an http_access line can never
    name an acl that was dropped for being empty.
    """
    if not tokens:
        return ()
    return (f"acl {acl_name} {kind} {' '.join(tokens)}",
            f"http_access {verb} localnet {acl_name}")


def _fragment(groups: "list[tuple]", tail: "list[str]") -> str:
    """Join all acl definitions, then their http_access lines, then `tail`.

    Each group is a _group() pair or (); emitting every acl before any
    http_access keeps each acl defined before Squid sees it referenced.
    """
    acls = [g[0] for g in groups if g]
    accesses = [g[1] for g in groups if g]
    return "\n".join([*acls, *accesses, *tail])


def _allowlist_domains(domain_tokens: "list[str]") -> "list[str]":
    """ALWAYS_ALLOW plus the configured tokens, order-preserving and de-duplicated.

    The effective default-deny domain set both enforcement layers render from:
    Squid's dstdomain allow group and Unbound's transparent zones key on the
    same names, so the union/dedup lives here rather than in each renderer.
    """
    return list(dict.fromkeys(ALWAYS_ALLOW + tuple(domain_tokens)))


def _deny_fragment(domain_tokens: "list[str]",
                   ip_tokens: "list[str]") -> str:
    """Allowlist fragment: only ALWAYS_ALLOW plus the listed hosts get through."""
    domains = _allowlist_domains(domain_tokens)
    ips = list(dict.fromkeys(ip_tokens))
    return _fragment(
        [_group("jail_allow_dom", "dstdomain", domains, "allow"),
         _group("jail_allow_ip", "dst", ips, "allow")],
        ["http_access deny all"],
    )


def _allow_fragment(domain_tokens: "list[str]",
                    ip_tokens: "list[str]") -> str:
    """Denylist fragment: everything but the listed hosts passes; ALWAYS_ALLOW wins.

    With nothing denied this is plain allow-localnet (ALWAYS_ALLOW is reachable
    anyway). Otherwise ALWAYS_ALLOW is matched first so a denied domain that
    happens to cover it cannot block the API.
    """
    domains = list(dict.fromkeys(domain_tokens))
    ips = list(dict.fromkeys(ip_tokens))
    if not domains and not ips:
        return "\n".join((
            "http_access allow localnet",
            "http_access deny all",
        ))
    return _fragment(
        [_group("jail_anthropic", "dstdomain", list(ALWAYS_ALLOW), "allow"),
         _group("jail_deny_dom", "dstdomain", domains, "deny"),
         _group("jail_deny_ip", "dst", ips, "deny")],
        ["http_access allow localnet", "http_access deny all"],
    )


def _zone(domain_token: str) -> str:
    """Unbound zone name for a leading-dot dstdomain token: '.x.com' -> 'x.com.'.

    _classify and ALWAYS_ALLOW both yield a single-leading-dot token, so dropping
    that dot and appending the root dot gives the FQDN zone Unbound's local-zone
    keys on; its closest-enclosing-zone match is Squid dstdomain's apex+subdomain.
    """
    return domain_token[1:] + "."


def _dns_deny_fragment(domain_tokens: "list[str]") -> str:
    """Allowlist: refuse all names, then let ALWAYS_ALLOW plus the listed domains resolve.

    `refuse` on the root is the default-deny; each allowed apex is a `transparent`
    zone, which (being more specific) wins and falls through to normal resolution
    for that domain and its subdomains. Always non-empty (ALWAYS_ALLOW is present).
    """
    domains = _allowlist_domains(domain_tokens)
    return "\n".join([
        'local-zone: "." refuse',
        *(f'local-zone: "{_zone(d)}" transparent' for d in domains),
    ])


def _dns_allow_fragment(domain_tokens: "list[str]") -> str:
    """Denylist: resolve everything, refuse the listed domains; ALWAYS_ALLOW is never refused.

    With nothing denied this is empty (the resolver recurses for every name).
    A denied domain at or under an ALWAYS_ALLOW apex is dropped so the API stays
    resolvable, mirroring _allow_fragment matching jail_anthropic first. The
    endswith test is exact because every token carries a single leading dot
    (_classify / ALWAYS_ALLOW), so e.g. '.notanthropic.com' never matches
    '.anthropic.com'.
    """
    domains = [d for d in dict.fromkeys(domain_tokens)
               if not any(d.endswith(aa) for aa in ALWAYS_ALLOW)]
    return "\n".join(f'local-zone: "{_zone(d)}" always_refuse' for d in domains)
