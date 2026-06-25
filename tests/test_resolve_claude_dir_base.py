"""Tests for resolve_claude_dir_base.py: the per-user config store base dir."""
import os
from unittest import mock

from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import resolve_claude_dir_base as rcd


class ResolveClaudeDirBaseTests(JailTestCase):
    def _cfg(self, d):
        """A config-file path inside `d` (need not exist; only its dir is used)."""
        return os.path.join(d, ".claude-jail.json")

    def test_flag_overrides_config(self):
        flagdir, cfgdir = self.tmpdir(), self.tmpdir()
        self.assertEqual(
            rcd.resolve({"claude_dir_base": cfgdir}, flagdir,
                        self._cfg(self.tmpdir())),
            flagdir)

    def test_config_used_when_no_flag(self):
        cfgdir = self.tmpdir()
        self.assertEqual(
            rcd.resolve({"claude_dir_base": cfgdir}, None,
                        self._cfg(self.tmpdir())),
            cfgdir)

    def test_precedence_flag_over_config_over_home(self):
        flagdir, cfgdir, home = self.tmpdir(), self.tmpdir(), self.tmpdir()
        cfg = self._cfg(self.tmpdir())
        env = {"HOME": home}
        # All three set: flag wins; drop the flag, config wins; drop both, $HOME.
        self.assertEqual(
            rcd.resolve({"claude_dir_base": cfgdir}, flagdir, cfg, env=env),
            flagdir)
        self.assertEqual(
            rcd.resolve({"claude_dir_base": cfgdir}, None, cfg, env=env), cfgdir)
        self.assertEqual(rcd.resolve({}, None, cfg, env=env), home)

    def test_defaults_to_home_raw_and_unvalidated(self):
        # The $HOME default is returned verbatim — not checked for existence,
        # mirroring the bare ${HOME} the compose file used before this knob.
        self.assertEqual(
            rcd.resolve({}, None, self._cfg(self.tmpdir()),
                        env={"HOME": "/no/such/home"}),
            "/no/such/home")

    def test_unset_home_returns_empty_without_dying(self):
        # A non-launching command (down/logs/ps) must tolerate an unset $HOME;
        # the launcher turns the empty result into a fatal error only at launch.
        self.assertEqual(rcd.resolve({}, None, self._cfg(self.tmpdir()), env={}),
                         "")

    def test_absolute_explicit_taken_as_is(self):
        other = self.tmpdir()
        self.assertEqual(
            rcd.resolve({"claude_dir_base": other}, None,
                        self._cfg(self.tmpdir())),
            other)

    def test_relative_resolved_against_config_dir(self):
        d = self.tmpdir()
        sub = self.mkdir(os.path.join(d, "nested"))
        self.assertEqual(
            rcd.resolve({"claude_dir_base": "nested"}, None, self._cfg(d)), sub)

    def test_tilde_expanded(self):
        home = self.tmpdir()
        sub = self.mkdir(os.path.join(home, "store"))
        # expanduser reads os.environ, not the env= dict, so patch it directly.
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(
                rcd.resolve({"claude_dir_base": "~/store"}, None,
                            self._cfg(self.tmpdir())),
                sub)

    def test_nonexistent_explicit_dir_dies(self):
        missing = os.path.join(self.tmpdir(), "nope")
        with self.assertDies("--claude-dir-base", "not an existing directory"):
            rcd.resolve({}, missing, self._cfg(self.tmpdir()))

    def test_nonexistent_config_value_names_config_source(self):
        missing = os.path.join(self.tmpdir(), "nope")
        with self.assertDies("'claude_dir_base' in", "not an existing directory"):
            rcd.resolve({"claude_dir_base": missing}, None,
                        self._cfg(self.tmpdir()))

    def test_empty_config_value_dies(self):
        with self.assertDies("'claude_dir_base' in", "non-empty path"):
            rcd.resolve({"claude_dir_base": ""}, None, self._cfg(self.tmpdir()))

    def test_non_string_config_value_dies(self):
        with self.assertDies("'claude_dir_base' in", "non-empty path"):
            rcd.resolve({"claude_dir_base": 5}, None, self._cfg(self.tmpdir()))

    def test_missing_explicit_dir_allowed_when_must_exist_false(self):
        # A non-launching command (down/logs/ps) must tolerate a configured base
        # that has since gone missing; only a launch existence-checks it. The
        # resolved path is still returned (parse_roots needs it for its check).
        missing = os.path.join(self.tmpdir(), "gone")
        self.assertEqual(
            rcd.resolve({}, missing, self._cfg(self.tmpdir()), must_exist=False),
            missing)
        self.assertEqual(
            rcd.resolve({"claude_dir_base": missing}, None,
                        self._cfg(self.tmpdir()), must_exist=False),
            missing)

    def test_surrounding_whitespace_rejected(self):
        # A leading space would also slip past the isabs check and be silently
        # re-anchored to the config dir, so reject surrounding whitespace outright.
        for bad in (" /data/store", "/data/store ", "\t/data/store"):
            with self.assertDies("surrounding whitespace or control"):
                rcd.resolve({"claude_dir_base": bad}, None,
                            self._cfg(self.tmpdir()))

    def test_control_characters_rejected(self):
        # An embedded newline would corrupt the compose bind source; reject it
        # from both the config value and the flag.
        with self.assertDies("surrounding whitespace or control"):
            rcd.resolve({"claude_dir_base": "/data/sto\nre"}, None,
                        self._cfg(self.tmpdir()))
        with self.assertDies("surrounding whitespace or control"):
            rcd.resolve({}, "/data/sto\nre", self._cfg(self.tmpdir()))

    def test_interior_space_allowed(self):
        # Only surrounding whitespace/control chars are rejected — a real
        # directory name may contain an interior space.
        spaced = self.mkdir(os.path.join(self.tmpdir(), "my store"))
        self.assertEqual(
            rcd.resolve({"claude_dir_base": spaced}, None,
                        self._cfg(self.tmpdir())),
            spaced)
