"""Tests for build_env.py: the non-filesystem settings (default_mode)."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_env as be


class DefaultModeTests(JailTestCase):
    def test_absent_is_none(self):
        self.assertIsNone(be.default_mode({}, "cfg"))

    def test_bare_word_accepted(self):
        self.assertEqual(be.default_mode({"default_mode": "plan"}, "cfg"),
                         "plan")

    def test_non_string_rejected(self):
        with self.assertDies("must be a bare word"):
            be.default_mode({"default_mode": 1}, "cfg")

    def test_word_with_metacharacters_rejected(self):
        with self.assertDies("must be a bare word"):
            be.default_mode({"default_mode": "plan; rm -rf"}, "cfg")


class ExportsTests(JailTestCase):
    def test_empty_when_nothing_set(self):
        self.assertEqual(be.exports({}, "cfg"), {})

    def test_default_mode_exported(self):
        self.assertEqual(
            be.exports({"default_mode": "acceptEdits"}, "cfg"),
            {"CLAUDE_JAIL_PERMISSION_MODE": "acceptEdits"})
