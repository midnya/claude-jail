"""Tests for build_packages.py: the per-project `packages.apt`/`.pip`/`.npm` keys."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_packages as bp
import resolve_ids as ri

DEFAULT_IDS = ri.Ids(ri.DEFAULT_UID, ri.DEFAULT_GID)
OTHER_IDS = ri.Ids(1500, 1600)


def _apt(*names):
    """A config dict carrying packages.apt = names."""
    return {"packages": {"apt": list(names)}}


def _pip(*specs):
    """A config dict carrying packages.pip = specs."""
    return {"packages": {"pip": list(specs)}}


def _npm(*specs):
    """A config dict carrying packages.npm = specs."""
    return {"packages": {"npm": list(specs)}}


class ParseAptTests(JailTestCase):
    def test_absent_is_empty(self):
        self.assertEqual(bp.parse({}, "cfg"),
                         bp.Packages(apt=[], pip=[], npm=[]))

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
        with self.assertDies("unknown key(s) in 'packages'", "cargo"):
            bp.parse({"packages": {"cargo": ["serde"]}}, "cfg")

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

    def test_all_managers_coexist(self):
        pkgs = bp.parse({"packages": {"apt": ["jq"], "pip": ["ruff"],
                                      "npm": ["eslint"]}}, "cfg")
        self.assertEqual(pkgs, bp.Packages(apt=["jq"], pip=["ruff"],
                                           npm=["eslint"]))

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


class ParseNpmTests(JailTestCase):
    def test_packages_without_npm_is_empty(self):
        self.assertEqual(bp.parse({"packages": {}}, "cfg").npm, [])

    def test_valid_list_sorted_and_deduped(self):
        self.assertEqual(bp.parse(_npm("eslint", "chalk", "eslint"), "cfg").npm,
                         ["chalk", "eslint"])

    def test_specs_accepted(self):
        # Scopes, version ranges, wildcards and dist-tags are all valid npm
        # specs; '/' is allowed as the scope separator.
        self.assertEqual(
            bp.parse(_npm("typescript", "@types/node", "eslint@^8.0.0",
                          "prettier@~3", "typescript@5.*",
                          "@angular/cli@latest", "foo@1.2.3-beta.1"), "cfg").npm,
            ["@angular/cli@latest", "@types/node", "eslint@^8.0.0",
             "foo@1.2.3-beta.1", "prettier@~3", "typescript", "typescript@5.*"])

    def test_case_preserved(self):
        # The part after '@' is a case-sensitive version/dist-tag, so normalize
        # is identity (unlike pip's str.lower) — it must not be mangled.
        self.assertEqual(bp.parse(_npm("foo@1.0.0-RC1"), "cfg").npm,
                         ["foo@1.0.0-RC1"])

    def test_name_case_not_folded(self):
        # The NAME is case-sensitive too: legacy packages like 'JSONStream'
        # keep their casing, so two spellings must NOT dedupe (lowercasing would
        # 404 the legacy name). Contrast pip, where 'Flask'/'flask' collapse.
        self.assertEqual(bp.parse(_npm("chalk", "Chalk"), "cfg").npm,
                         ["Chalk", "chalk"])

    def test_compound_range_rejected(self):
        # A range needing whitespace ('>=2 <3') or '|' ('1||2') can't be
        # expressed — both chars are excluded as injection vectors.
        with self.assertDies("not a valid"):
            bp.parse(_npm("eslint@>=2 <3"), "cfg")
        with self.assertDies("not a valid"):
            bp.parse(_npm("eslint@1||2"), "cfg")

    def test_non_list_rejected(self):
        with self.assertDies("'packages.npm'", "must be an array"):
            bp.parse({"packages": {"npm": "eslint"}}, "cfg")

    def test_non_string_entry_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_npm("eslint", 1), "cfg")

    def test_empty_string_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_npm(""), "cfg")

    def test_leading_dash_rejected(self):
        # A spec starting with '-' would read as a yarn flag; the leading
        # alphanumeric-or-'@' requirement forbids it.
        with self.assertDies("not a valid"):
            bp.parse(_npm("-rf"), "cfg")

    def test_embedded_space_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_npm("foo bar"), "cfg")

    def test_shell_metacharacter_rejected(self):
        with self.assertDies("not a valid"):
            bp.parse(_npm("eslint; rm -rf /"), "cfg")

    def test_url_install_rejected(self):
        # ':' is excluded, so a URL or VCS install cannot pass.
        with self.assertDies("not a valid"):
            bp.parse(_npm("git+https://example.com/x"), "cfg")

    def test_npm_alias_rejected(self):
        # An aliased install carries ':' (pkg@npm:other), which is excluded.
        with self.assertDies("not a valid"):
            bp.parse(_npm("pkg@npm:left-pad"), "cfg")

    def test_github_shorthand_rejected(self):
        # A bare 'user/repo' would make yarn fetch from GitHub at build time;
        # '/' is allowed only as the '@scope/' separator, so this is rejected.
        with self.assertDies("not a valid"):
            bp.parse(_npm("user/repo"), "cfg")

    def test_rejection_names_accepted_shape(self):
        # The message says what npm accepts, not just that the entry failed.
        with self.assertDies("not a valid", "expected", "npm"):
            bp.parse(_npm("user/repo"), "cfg")


class ImageSuffixTests(JailTestCase):
    def test_empty_has_no_suffix(self):
        self.assertEqual(bp.image_suffix(bp.Packages(apt=[], pip=[], npm=[])), "")

    def test_suffix_shape(self):
        suffix = bp.image_suffix(bp.Packages(apt=["jq"], pip=[], npm=[]))
        self.assertRegex(suffix, r"\A-[0-9a-f]{8}\Z")

    def test_suffix_order_and_dup_independent(self):
        # parse() canonicalises, but the digest must not depend on order/dups
        # either, so the tag is stable for an equivalent set.
        self.assertEqual(
            bp.image_suffix(bp.parse(_apt("jq", "ripgrep"), "cfg")),
            bp.image_suffix(bp.parse(_apt("ripgrep", "jq", "jq"), "cfg")))

    def test_distinct_apt_sets_differ(self):
        self.assertNotEqual(
            bp.image_suffix(bp.Packages(apt=["jq"], pip=[], npm=[])),
            bp.image_suffix(bp.Packages(apt=["ripgrep"], pip=[], npm=[])))

    def test_pip_changes_suffix(self):
        self.assertNotEqual(
            bp.image_suffix(bp.Packages(apt=["jq"], pip=[], npm=[])),
            bp.image_suffix(bp.Packages(apt=["jq"], pip=["ruff"], npm=[])))

    def test_npm_changes_suffix(self):
        self.assertNotEqual(
            bp.image_suffix(bp.Packages(apt=["jq"], pip=[], npm=[])),
            bp.image_suffix(bp.Packages(apt=["jq"], pip=[], npm=["eslint"])))

    def test_pip_case_insensitive_same_suffix(self):
        # Same distribution under two spellings must content-address to one
        # image, not mint a redundant tag.
        self.assertEqual(bp.image_suffix(bp.parse(_pip("Flask"), "cfg")),
                         bp.image_suffix(bp.parse(_pip("flask"), "cfg")))

    def test_managers_namespaced_no_collision(self):
        # The same token as an apt vs a pip vs an npm entry must yield different
        # tags; namespacing the managers in the digest key keeps them apart.
        apt = bp.image_suffix(bp.Packages(apt=["foo"], pip=[], npm=[]))
        pip = bp.image_suffix(bp.Packages(apt=[], pip=["foo"], npm=[]))
        npm = bp.image_suffix(bp.Packages(apt=[], pip=[], npm=["foo"]))
        self.assertEqual(len({apt, pip, npm}), 3)


class ImageSuffixIdsTests(JailTestCase):
    """The container uid/gid is a build input, so it folds into the image tag."""

    def test_default_ids_match_no_ids(self):
        # None (the default user) and the explicit default ids are equivalent —
        # neither folds in, so an existing package set keeps its old tag.
        pkgs = bp.parse(_apt("jq"), "cfg")
        self.assertEqual(bp.image_suffix(pkgs),
                         bp.image_suffix(pkgs, DEFAULT_IDS))

    def test_default_ids_empty_packages_has_no_suffix(self):
        # The all-default baseline still maps to the bare `claude-jail` tag.
        empty = bp.Packages(apt=[], pip=[], npm=[])
        self.assertEqual(bp.image_suffix(empty, DEFAULT_IDS), "")

    def test_non_default_ids_suffix_even_without_packages(self):
        # A pinned uid/gid alone needs its own image (the base tag is built with
        # the Dockerfile's default user).
        suffix = bp.image_suffix(bp.Packages(apt=[], pip=[], npm=[]), OTHER_IDS)
        self.assertRegex(suffix, r"\A-[0-9a-f]{8}\Z")

    def test_non_default_ids_change_suffix(self):
        pkgs = bp.parse(_apt("jq"), "cfg")
        self.assertNotEqual(bp.image_suffix(pkgs, DEFAULT_IDS),
                            bp.image_suffix(pkgs, OTHER_IDS))

    def test_uid_and_gid_independent(self):
        # Swapping uid for gid is a different image.
        self.assertNotEqual(
            bp.image_suffix(bp.Packages(apt=[], pip=[], npm=[]),
                            ri.Ids(1500, 1600)),
            bp.image_suffix(bp.Packages(apt=[], pip=[], npm=[]),
                            ri.Ids(1600, 1500)))

    def test_ids_section_cannot_collide_with_a_package(self):
        # The labeled sections keep a package token from masquerading as the
        # uid/gid fold: a package set spelling out the fold's own labels and
        # values ("uid", "gid", "1500", "1600" — all valid tokens) under the
        # default ids (so only the packages fold in) must still digest
        # differently than the real OTHER_IDS fold over an empty set.
        spoof = bp.Packages(apt=["uid", "gid"], pip=["1500", "1600"], npm=[])
        self.assertNotEqual(bp.image_suffix(spoof, DEFAULT_IDS),
                            bp.image_suffix(bp.Packages(apt=[], pip=[], npm=[]),
                                            OTHER_IDS))
