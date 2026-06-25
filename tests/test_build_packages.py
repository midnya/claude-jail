"""Tests for build_packages.py: the per-project `packages.apt`/`packages.pip` keys."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_packages as bp


def _apt(*names):
    """A config dict carrying packages.apt = names."""
    return {"packages": {"apt": list(names)}}


def _pip(*specs):
    """A config dict carrying packages.pip = specs."""
    return {"packages": {"pip": list(specs)}}


class ParseAptTests(JailTestCase):
    def test_absent_is_empty(self):
        self.assertEqual(bp.parse({}, "cfg"), bp.Packages(apt=[], pip=[]))

    def test_packages_without_apt_is_empty(self):
        self.assertEqual(bp.parse({"packages": {}}, "cfg").apt, [])

    def test_valid_list_sorted_and_deduped(self):
        self.assertEqual(bp.parse(_apt("ripgrep", "jq", "jq"), "cfg").apt,
                         ["jq", "ripgrep"])

    def test_charset_extras_accepted(self):
        # '+', '-', '.' and digits are all valid in Debian package names.
        self.assertEqual(
            bp.parse(_apt("g++", "python3.11", "lib-foo"), "cfg").apt,
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

    def test_rejection_names_accepted_shape(self):
        # The message says what apt accepts, not just that the entry failed.
        with self.assertDies("not a valid", "expected", "Debian package name"):
            bp.parse(_apt("Bad Name"), "cfg")


class ParsePipTests(JailTestCase):
    def test_packages_without_pip_is_empty(self):
        self.assertEqual(bp.parse({"packages": {}}, "cfg").pip, [])

    def test_valid_list_sorted_and_deduped(self):
        self.assertEqual(bp.parse(_pip("ruff", "requests", "ruff"), "cfg").pip,
                         ["requests", "ruff"])

    def test_specs_accepted(self):
        # Pins, extras and mixed case are all valid PEP 508 specs; a name is
        # case-insensitive so it is lowercased (Flask -> flask).
        self.assertEqual(
            bp.parse(_pip("requests==2.31.0", "django<5",
                          "uvicorn[standard]", "Flask"), "cfg").pip,
            ["django<5", "flask", "requests==2.31.0", "uvicorn[standard]"])

    def test_case_insensitive_dedup(self):
        # A PyPI name is case-insensitive, so Flask and flask are one
        # distribution: deduped to the lowercase form, not installed twice.
        self.assertEqual(bp.parse(_pip("Flask", "flask"), "cfg").pip, ["flask"])

    def test_apt_and_pip_coexist(self):
        pkgs = bp.parse({"packages": {"apt": ["jq"], "pip": ["ruff"]}}, "cfg")
        self.assertEqual(pkgs, bp.Packages(apt=["jq"], pip=["ruff"]))

    def test_non_list_rejected(self):
        with self.assertDies("'packages.pip'", "must be an array"):
            bp.parse({"packages": {"pip": "ruff"}}, "cfg")

    def test_non_string_entry_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_pip("ruff", 1), "cfg")

    def test_empty_string_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_pip(""), "cfg")

    def test_leading_dash_rejected(self):
        # A spec starting with '-' would read as a pip flag (or a '-r' line in
        # the requirements file); the leading alphanumeric requirement forbids it.
        with self.assertDies("not a valid"):
            bp.parse(_pip("-rf"), "cfg")

    def test_embedded_space_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_pip("foo bar"), "cfg")

    def test_shell_metacharacter_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_pip("ruff; rm -rf /"), "cfg")

    def test_url_install_rejected(self):
        # No '/', ':' or '@' — a URL or VCS install is forbidden.
        with self.assertDies("not a valid"):
            bp.parse(_pip("git+https://example.com/x"), "cfg")

    def test_remote_ref_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_pip("pkg @ http://x"), "cfg")

    def test_environment_marker_rejected(self):
        # A valid PEP 508 marker carries ';' and whitespace; the filter rejects
        # it (the README documents this exclusion).
        with self.assertDies("not a valid"):
            bp.parse(_pip('requests; python_version<"3"'), "cfg")

    def test_wildcard_and_range_accepted(self):
        # Wildcards and comma ranges are valid version specifiers and pass.
        self.assertEqual(
            bp.parse(_pip("numpy==1.26.*", "django>=2,<5"), "cfg").pip,
            ["django>=2,<5", "numpy==1.26.*"])

    def test_rejection_names_accepted_shape(self):
        # The message says what pip accepts, not just that the entry failed.
        with self.assertDies("not a valid", "expected", "PyPI"):
            bp.parse(_pip("pkg @ http://x"), "cfg")


class ImageSuffixTests(JailTestCase):
    def test_empty_has_no_suffix(self):
        self.assertEqual(bp.image_suffix(bp.Packages(apt=[], pip=[])), "")

    def test_suffix_shape(self):
        suffix = bp.image_suffix(bp.Packages(apt=["jq"], pip=[]))
        self.assertRegex(suffix, r"\A-[0-9a-f]{8}\Z")

    def test_suffix_order_and_dup_independent(self):
        # parse() canonicalises, but the digest must not depend on order/dups
        # either, so the tag is stable for an equivalent set.
        self.assertEqual(
            bp.image_suffix(bp.parse(_apt("jq", "ripgrep"), "cfg")),
            bp.image_suffix(bp.parse(_apt("ripgrep", "jq", "jq"), "cfg")))

    def test_distinct_apt_sets_differ(self):
        self.assertNotEqual(bp.image_suffix(bp.Packages(apt=["jq"], pip=[])),
                            bp.image_suffix(bp.Packages(apt=["ripgrep"], pip=[])))

    def test_pip_changes_suffix(self):
        self.assertNotEqual(bp.image_suffix(bp.Packages(apt=["jq"], pip=[])),
                            bp.image_suffix(bp.Packages(apt=["jq"], pip=["ruff"])))

    def test_pip_case_insensitive_same_suffix(self):
        # Same distribution under two spellings must content-address to one
        # image, not mint a redundant tag.
        self.assertEqual(bp.image_suffix(bp.parse(_pip("Flask"), "cfg")),
                         bp.image_suffix(bp.parse(_pip("flask"), "cfg")))

    def test_apt_pip_namespaced_no_collision(self):
        # The same token as an apt vs a pip entry must yield different tags;
        # namespacing the managers in the digest key keeps them apart.
        self.assertNotEqual(bp.image_suffix(bp.Packages(apt=["foo"], pip=[])),
                            bp.image_suffix(bp.Packages(apt=[], pip=["foo"])))
