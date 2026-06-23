"""Tests for resolve_user.py: picking the jail user (config namespace)."""
import contextlib
import io

from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import resolve_user as ru


class ResolveUserTests(JailTestCase):
    def test_flag_overrides_config(self):
        self.assertEqual(ru.resolve({"user": "fromcfg"}, "fromflag", "cfg"),
                         "fromflag")

    def test_config_used_when_no_flag(self):
        self.assertEqual(ru.resolve({"user": "fromcfg"}, None, "cfg"),
                         "fromcfg")

    def test_explicit_sources_ignore_env(self):
        # Flag and config both win over a set environment, and neither prints
        # the env-fallback warning (only the env path is "no user set").
        env = {"USER": "fromenv"}
        cases = (({}, "fromflag", "fromflag"),
                 ({"user": "fromcfg"}, None, "fromcfg"))
        for data, override, expected in cases:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                value = ru.resolve(data, override, "cfg", env=env)
            self.assertEqual(value, expected)
            self.assertEqual(err.getvalue(), "")

    def test_precedence_flag_over_config_over_env(self):
        # All three sources set at once: flag wins; drop the flag, config wins.
        env = {"USER": "fromenv"}
        data = {"user": "fromcfg"}
        self.assertEqual(ru.resolve(data, "fromflag", "cfg", env=env),
                         "fromflag")
        self.assertEqual(ru.resolve(data, None, "cfg", env=env), "fromcfg")

    def test_neither_source_dies(self):
        with self.assertDies("no user set"):
            ru.resolve({}, None, "cfg", env={})

    def test_env_user_default(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ru.resolve({}, None, "cfg", env={"USER": "alice"}),
                             "alice")
        warning = err.getvalue()
        self.assertIn("$USER", warning)
        self.assertIn("alice", warning)

    def _resolve_quiet(self, *args, **kwargs):
        """resolve() with the stderr fallback warning swallowed."""
        with contextlib.redirect_stderr(io.StringIO()):
            return ru.resolve(*args, **kwargs)

    def test_env_username_used_when_user_absent(self):
        self.assertEqual(
            self._resolve_quiet({}, None, "cfg", env={"USERNAME": "bob"}),
            "bob")

    def test_env_user_preferred_over_username(self):
        env = {"USER": "alice", "USERNAME": "bob"}
        self.assertEqual(self._resolve_quiet({}, None, "cfg", env=env), "alice")

    def test_empty_env_user_falls_through_to_username(self):
        env = {"USER": "", "USERNAME": "bob"}
        self.assertEqual(self._resolve_quiet({}, None, "cfg", env=env), "bob")

    def test_env_username_warning_names_source(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ru.resolve({}, None, "cfg", env={"USERNAME": "bob"})
        warning = err.getvalue()
        self.assertIn("$USERNAME", warning)
        self.assertIn("bob", warning)

    def test_empty_config_value_does_not_use_env(self):
        # An explicit-but-empty config value is invalid, not "unset": it dies
        # naming the config source rather than falling through to $USER.
        with self.assertDies("'user' in", "must be a bare word"):
            ru.resolve({"user": ""}, None, "cfg", env={"USER": "alice"})

    def test_invalid_env_value_dies(self):
        with self.assertDies("$USER", "must be a bare word"):
            ru.resolve({}, None, "cfg", env={"USER": "bad/name"})

    def test_invalid_flag_value(self):
        with self.assertDies("--user", "must be a bare word"):
            ru.resolve({}, "bad name", "cfg")

    def test_invalid_config_value_names_config_source(self):
        with self.assertDies("'user' in", "must be a bare word"):
            ru.resolve({"user": "bad/name"}, None, "cfg")

    def test_non_string_config_value(self):
        with self.assertDies("must be a bare word"):
            ru.resolve({"user": 5}, None, "cfg")
