"""Tests for build_env.py: the non-filesystem settings (default_mode, mem_limit)."""
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


class MemLimitTests(JailTestCase):
    def test_absent_is_none(self):
        self.assertIsNone(be.mem_limit({}, "cfg"))

    def test_suffixed_size_accepted(self):
        self.assertEqual(be.mem_limit({"mem_limit": "8G"}, "cfg"), "8G")

    def test_integer_bytes_accepted(self):
        self.assertEqual(be.mem_limit({"mem_limit": 1073741824}, "cfg"),
                         "1073741824")

    def test_zero_rejected(self):
        with self.assertDies("must be a positive size"):
            be.mem_limit({"mem_limit": "0"}, "cfg")

    def test_bool_rejected(self):
        with self.assertDies("must be a positive size"):
            be.mem_limit({"mem_limit": True}, "cfg")

    def test_flag_overrides_config(self):
        self.assertEqual(
            be.mem_limit({"mem_limit": "8g"}, "cfg", flag="2g"), "2g")

    def test_invalid_flag_names_flag(self):
        with self.assertDies("--mem-limit must be a positive size"):
            be.mem_limit({}, "cfg", flag="lots")

    def test_unknown_unit_rejected(self):
        with self.assertDies("must be a positive size"):
            be.mem_limit({"mem_limit": "8gb"}, "cfg")


class ExportsTests(JailTestCase):
    def test_empty_when_nothing_set(self):
        self.assertEqual(be.exports({}, "cfg"), {})

    def test_default_mode_exported(self):
        self.assertEqual(
            be.exports({"default_mode": "acceptEdits"}, "cfg"),
            {"CLAUDE_JAIL_PERMISSION_MODE": "acceptEdits"})

    def test_mem_limit_not_exported(self):
        self.assertEqual(be.exports({"mem_limit": "4g"}, "cfg"), {})


class ServiceLinesTests(JailTestCase):
    def test_empty_when_nothing_set(self):
        self.assertEqual(be.service_lines({}, "cfg"), [])

    def test_mem_limit_rendered(self):
        self.assertEqual(be.service_lines({"mem_limit": "4g"}, "cfg"),
                         ["    mem_limit: 4g"])
