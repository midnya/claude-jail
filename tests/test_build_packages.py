"""Tests for build_packages.py: the per-project `packages.apt` key."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_packages as bp


def _apt(*names):
    """A config dict carrying packages.apt = names."""
    return {"packages": {"apt": list(names)}}


class ParseTests(JailTestCase):
    def test_absent_is_empty(self):
        self.assertEqual(bp.parse({}, "cfg"), [])

    def test_packages_without_apt_is_empty(self):
        self.assertEqual(bp.parse({"packages": {}}, "cfg"), [])

    def test_valid_list_sorted_and_deduped(self):
        self.assertEqual(bp.parse(_apt("ripgrep", "jq", "jq"), "cfg"),
                         ["jq", "ripgrep"])

    def test_charset_extras_accepted(self):
        # '+', '-', '.' and digits are all valid in Debian package names.
        self.assertEqual(bp.parse(_apt("g++", "python3.11", "lib-foo"), "cfg"),
                         ["g++", "lib-foo", "python3.11"])

    def test_packages_not_object_rejected(self):
        with self.assertDies("'packages'", "must be an object"):
            bp.parse({"packages": ["jq"]}, "cfg")

    def test_unknown_manager_rejected(self):
        with self.assertDies("unknown key(s) in 'packages'", "npm"):
            bp.parse({"packages": {"npm": ["left-pad"]}}, "cfg")

    def test_non_list_rejected(self):
        with self.assertDies("'packages.apt'", "must be an array"):
            bp.parse({"packages": {"apt": "jq"}}, "cfg")

    def test_non_string_entry_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_apt("jq", 1), "cfg")

    def test_empty_string_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_apt(""), "cfg")

    def test_uppercase_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_apt("Jq"), "cfg")

    def test_leading_dash_rejected(self):
        # A name starting with '-' would read as an apt flag; the leading
        # alphanumeric requirement forbids it.
        with self.assertDies("not a valid"):
            bp.parse(_apt("-rf"), "cfg")

    def test_embedded_space_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_apt("foo bar"), "cfg")

    def test_shell_metacharacter_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_apt("jq; rm -rf /"), "cfg")


class BuildArgTests(JailTestCase):
    def test_empty_is_blank(self):
        self.assertEqual(bp.build_arg([]), "")

    def test_joined_with_single_spaces(self):
        self.assertEqual(bp.build_arg(["jq", "ripgrep"]), "jq ripgrep")


class ImageSuffixTests(JailTestCase):
    def test_empty_has_no_suffix(self):
        self.assertEqual(bp.image_suffix([]), "")

    def test_suffix_shape(self):
        suffix = bp.image_suffix(["jq"])
        self.assertRegex(suffix, r"\A-[0-9a-f]{8}\Z")

    def test_suffix_order_and_dup_independent(self):
        # parse() canonicalises, but the digest must not depend on order/dups
        # either, so the tag is stable for an equivalent set.
        self.assertEqual(
            bp.image_suffix(bp.parse(_apt("jq", "ripgrep"), "cfg")),
            bp.image_suffix(bp.parse(_apt("ripgrep", "jq", "jq"), "cfg")))

    def test_distinct_sets_differ(self):
        self.assertNotEqual(bp.image_suffix(["jq"]), bp.image_suffix(["ripgrep"]))
