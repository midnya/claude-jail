"""Tests for resolve_user.py: picking the jail user (config namespace)."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import resolve_user as ru


class ResolveUserTests(JailTestCase):
    def test_flag_overrides_config(self):
        self.assertEqual(ru.resolve({"user": "fromcfg"}, "fromflag", "cfg"),
                         "fromflag")

    def test_config_used_when_no_flag(self):
        self.assertEqual(ru.resolve({"user": "fromcfg"}, None, "cfg"),
                         "fromcfg")

    def test_neither_source_dies(self):
        with self.assertDies("no user set"):
            ru.resolve({}, None, "cfg")

    def test_invalid_flag_value(self):
        with self.assertDies("--user", "must be a bare word"):
            ru.resolve({}, "bad name", "cfg")

    def test_invalid_config_value_names_config_source(self):
        with self.assertDies("'user' in", "must be a bare word"):
            ru.resolve({"user": "bad/name"}, None, "cfg")

    def test_non_string_config_value(self):
        with self.assertDies("must be a bare word"):
            ru.resolve({"user": 5}, None, "cfg")
