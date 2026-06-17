"""Tests for build_acl.py: the egress -> Squid http_access conversion."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_acl as ba


class DefaultDenyTests(JailTestCase):
    def test_no_egress_key_denies_all_but_anthropic(self):
        rules = ba.squid_rules({}, "cfg")
        self.assertEqual(rules, "\n".join((
            "acl jail_allow dstdomain .anthropic.com .claude.com",
            "http_access allow localnet jail_allow",
            "http_access deny all",
        )))

    def test_omitted_default_is_deny(self):
        self.assertEqual(ba.squid_rules({"egress": {"allowed": []}}, "cfg"),
                         ba.squid_rules({}, "cfg"))

    def test_allowed_exact_and_wildcard(self):
        rules = ba.squid_rules(
            {"egress": {"default": "deny",
                        "allowed": ["github.com", "*.example.com"]}}, "cfg")
        self.assertIn(
            "acl jail_allow dstdomain "
            ".anthropic.com .claude.com github.com .example.com", rules)
        self.assertIn("http_access allow localnet jail_allow", rules)
        self.assertTrue(rules.endswith("http_access deny all"))

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
                        "denied": ["*.tracker.example", "evil.com"]}}, "cfg")
        self.assertEqual(rules, "\n".join((
            "acl jail_anthropic dstdomain .anthropic.com .claude.com",
            "acl jail_deny dstdomain .tracker.example evil.com",
            "http_access allow localnet jail_anthropic",
            "http_access deny localnet jail_deny",
            "http_access allow localnet",
            "http_access deny all",
        )))


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


class InjectionTests(JailTestCase):
    """Each list entry can only become a dstdomain token, never a directive."""

    def _rejects(self, pattern):
        with self.assertDies("invalid egress host pattern"):
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
