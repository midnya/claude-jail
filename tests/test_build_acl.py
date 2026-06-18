"""Tests for build_acl.py: the egress -> Squid http_access conversion."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_acl as ba


class DefaultDenyTests(JailTestCase):
    def test_no_egress_key_denies_all_but_anthropic(self):
        rules = ba.squid_rules({}, "cfg")
        self.assertEqual(rules, "\n".join((
            "acl jail_allow_dom dstdomain .anthropic.com .claude.com",
            "http_access allow localnet jail_allow_dom",
            "http_access deny all",
        )))

    def test_omitted_default_is_deny(self):
        self.assertEqual(ba.squid_rules({"egress": {"allowed": []}}, "cfg"),
                         ba.squid_rules({}, "cfg"))

    def test_allowed_domains_are_subdomain_inclusive(self):
        rules = ba.squid_rules(
            {"egress": {"default": "deny",
                        "allowed": ["github.com", "*.example.com"]}}, "cfg")
        # Both a bare host and a "*." host become a leading-dot dstdomain token,
        # which Squid matches against the apex and every subdomain.
        self.assertEqual(rules, "\n".join((
            "acl jail_allow_dom dstdomain "
            ".anthropic.com .claude.com .github.com .example.com",
            "http_access allow localnet jail_allow_dom",
            "http_access deny all",
        )))

    def test_allowed_cidrs_use_dst(self):
        rules = ba.squid_rules(
            {"egress": {"default": "deny",
                        "allowed": ["1.2.3.4/32", "10.0.0.0/8", "github.com"]}},
            "cfg")
        self.assertEqual(rules, "\n".join((
            "acl jail_allow_dom dstdomain "
            ".anthropic.com .claude.com .github.com",
            "acl jail_allow_ip dst 1.2.3.4/32 10.0.0.0/8",
            "http_access allow localnet jail_allow_dom",
            "http_access allow localnet jail_allow_ip",
            "http_access deny all",
        )))

    def test_ipv6_cidr_is_accepted(self):
        rules = ba.squid_rules(
            {"egress": {"default": "deny", "allowed": ["2606:4700::/32"]}}, "cfg")
        self.assertIn("acl jail_allow_ip dst 2606:4700::/32", rules)

    def test_network_address_cidr_is_accepted(self):
        rules = ba.squid_rules(
            {"egress": {"default": "deny", "allowed": ["192.168.1.0/24"]}}, "cfg")
        self.assertIn("acl jail_allow_ip dst 192.168.1.0/24", rules)

    def test_anthropic_deduped_when_listed(self):
        rules = ba.squid_rules(
            {"egress": {"default": "deny", "allowed": ["*.anthropic.com"]}},
            "cfg")
        self.assertEqual(rules.count(".anthropic.com"), 1)


class DefaultAllowTests(JailTestCase):
    def test_empty_denied_is_plain_allow(self):
        rules = ba.squid_rules({"egress": {"default": "allow"}}, "cfg")
        self.assertEqual(rules, "\n".join((
            "http_access allow localnet",
            "http_access deny all",
        )))

    def test_denied_blocks_and_anthropic_wins_first(self):
        rules = ba.squid_rules(
            {"egress": {"default": "allow",
                        "denied": ["*.tracker.example", "evil.com", "1.2.3.4/32"]}},
            "cfg")
        self.assertEqual(rules, "\n".join((
            "acl jail_anthropic dstdomain .anthropic.com .claude.com",
            "acl jail_deny_dom dstdomain .tracker.example .evil.com",
            "acl jail_deny_ip dst 1.2.3.4/32",
            "http_access allow localnet jail_anthropic",
            "http_access deny localnet jail_deny_dom",
            "http_access deny localnet jail_deny_ip",
            "http_access allow localnet",
            "http_access deny all",
        )))

    def test_denied_host_blocks_its_subdomains(self):
        # A bare denied "b.a" must also cover "c.b.a" / "d.c.b.a": the token is
        # a leading-dot dstdomain, Squid's apex+subdomain match.
        rules = ba.squid_rules(
            {"egress": {"default": "allow", "denied": ["b.a"]}}, "cfg")
        self.assertIn("acl jail_deny_dom dstdomain .b.a", rules)

    def test_denied_dedup_on_allow_side(self):
        rules = ba.squid_rules(
            {"egress": {"default": "allow",
                        "denied": ["evil.com", "evil.com", "*.anthropic.com"]}},
            "cfg")
        self.assertIn("acl jail_deny_dom dstdomain .evil.com .anthropic.com",
                      rules)


class ValidationTests(JailTestCase):
    def test_egress_not_object(self):
        with self.assertDies("must be an object"):
            ba.squid_rules({"egress": ["github.com"]}, "cfg")

    def test_unknown_sub_key(self):
        with self.assertDies("unknown key"):
            ba.squid_rules({"egress": {"default": "deny", "allow": []}}, "cfg")

    def test_bad_default_value(self):
        with self.assertDies("must be"):
            ba.squid_rules({"egress": {"default": "block"}}, "cfg")

    def test_denied_present_under_deny(self):
        with self.assertDies("default \"deny\""):
            ba.squid_rules({"egress": {"default": "deny", "denied": ["x.com"]}},
                           "cfg")

    def test_allowed_present_under_allow(self):
        with self.assertDies("default \"allow\""):
            ba.squid_rules({"egress": {"default": "allow", "allowed": ["x.com"]}},
                           "cfg")

    def test_list_not_array(self):
        with self.assertDies("must be an array"):
            ba.squid_rules({"egress": {"allowed": "github.com"}}, "cfg")

    def test_non_string_entry(self):
        with self.assertDies("must be strings"):
            ba.squid_rules({"egress": {"allowed": [1]}}, "cfg")


class HostPatternTests(JailTestCase):
    """A list entry can only become a dstdomain/dst token, never a directive."""

    def _rejects(self, pattern, *needles):
        with self.assertDies(*(needles or ("invalid egress host pattern",))):
            ba.squid_rules({"egress": {"allowed": [pattern]}}, "cfg")

    def test_newline(self):
        self._rejects("evil.com\nhttp_access allow all")

    def test_scheme(self):
        self._rejects("https://evil.com")

    def test_port(self):
        self._rejects("evil.com:443")

    def test_path(self):
        self._rejects("evil.com/admin")

    def test_whitespace(self):
        self._rejects("evil.com bad.com")

    def test_bare_wildcard(self):
        self._rejects("*")

    def test_non_leading_wildcard(self):
        self._rejects("foo.*.com")

    def test_empty(self):
        self._rejects("")

    def test_bare_tld_rejected(self):
        self._rejects("com")

    def test_single_label_rejected(self):
        self._rejects("localhost")

    def test_wildcard_tld_rejected(self):
        self._rejects("*.com")

    def test_wildcard_on_ip_rejected(self):
        self._rejects("*.1.2.3.4", "applies '*.' to an IP")

    def test_bare_ipv4_rejected(self):
        self._rejects("1.2.3.4", "must be a CIDR")

    def test_bare_ipv6_rejected(self):
        self._rejects("::1", "must be a CIDR")

    def test_cidr_with_host_bits_rejected(self):
        # strict=True: '10.0.0.1/8' has host bits set; rather than silently widen
        # to 10.0.0.0/8 it must fail, so a typo'd prefix can't broaden egress.
        self._rejects("10.0.0.1/8")

    def test_cidr_zero_prefix_rejected(self):
        # A /0 would cover every address; reject it so it can't quietly open all
        # egress through a single allow entry.
        self._rejects("0.0.0.0/0")
        self._rejects("::/0")


class PolicyKeyTests(JailTestCase):
    """policy_key is canonical: same policy -> same key, regardless of rendering."""

    def test_no_egress_matches_empty_allowed(self):
        self.assertEqual(ba.policy_key({}, "cfg"),
                         ba.policy_key({"egress": {"allowed": []}}, "cfg"))

    def test_order_and_duplicates_do_not_matter(self):
        a = ba.policy_key(
            {"egress": {"allowed": ["a.com", "b.com", "1.2.3.4/32"]}}, "cfg")
        b = ba.policy_key(
            {"egress": {"allowed": ["1.2.3.4/32", "b.com", "a.com", "a.com"]}},
            "cfg")
        self.assertEqual(a, b)

    def test_different_policies_differ(self):
        allow_a = ba.policy_key({"egress": {"allowed": ["a.com"]}}, "cfg")
        allow_b = ba.policy_key({"egress": {"allowed": ["b.com"]}}, "cfg")
        # Same token list, opposite default, is a different policy.
        deny_side = ba.policy_key(
            {"egress": {"default": "allow", "denied": ["a.com"]}}, "cfg")
        self.assertNotEqual(allow_a, allow_b)
        self.assertNotEqual(allow_a, deny_side)

    def test_key_is_independent_of_fragment_formatting(self):
        # The whole point: the key must not be the rendered Squid text, so that a
        # cosmetic change to squid_rules' output can't churn the proxy identity.
        data = {"egress": {"allowed": ["a.com"]}}
        self.assertNotIn("http_access", ba.policy_key(data, "cfg"))
        self.assertNotIn("dstdomain", ba.policy_key(data, "cfg"))
