"""Tests for build_env.py: the non-filesystem settings (default_mode, mem_limit,
gh_token_file)."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import os
from unittest import mock

import build_env as be

TOKEN = "github_pat_11ABCDEFG0abcdefghij_0123456789"


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


class GhTokenTests(JailTestCase):
    def _token_file(self, text=TOKEN + "\n"):
        d = self.tmpdir()
        return d, self.write(os.path.join(d, "tok"), text)

    def test_absent_is_none(self):
        self.assertIsNone(be.gh_token({}, "cfg", self.tmpdir(), False))

    def test_config_key_read_and_stripped(self):
        d, _f = self._token_file()
        self.assertEqual(
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, False), TOKEN)

    def test_config_key_expands_tilde(self):
        d, _f = self._token_file()
        with mock.patch.dict(os.environ, {"HOME": d}):
            self.assertEqual(be.gh_token({"gh_token_file": "~/tok"}, "cfg",
                                         self.tmpdir(), False), TOKEN)

    def test_flag_overrides_config(self):
        d, f = self._token_file()
        self.assertEqual(be.gh_token({"gh_token_file": "missing"}, "cfg", d,
                                     False, flag=f), TOKEN)

    def test_in_jail_config_key_refused(self):
        d, _f = self._token_file()
        with self.assertDies("is refused"):
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, True)

    def test_in_jail_refused_even_with_flag(self):
        d, f = self._token_file()
        with self.assertDies("is refused"):
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, True, flag=f)

    def test_flag_allowed_with_in_jail_config(self):
        d, f = self._token_file()
        self.assertEqual(be.gh_token({}, "cfg", d, True, flag=f), TOKEN)

    def test_classic_token_rejected(self):
        d, _f = self._token_file("ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        with self.assertDies("fine-grained"):
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, False)

    def test_multiple_tokens_rejected(self):
        d, _f = self._token_file(TOKEN + "\n" + TOKEN)
        with self.assertDies("exactly one"):
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, False)

    def test_rejection_does_not_echo_contents(self):
        d, _f = self._token_file("not-a-token-secret")
        with self.assertRaises(SystemExit) as cm:
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, False)
        self.assertNotIn("not-a-token-secret", str(cm.exception.code))

    def test_symlink_refused(self):
        d, f = self._token_file()
        self.symlink(os.path.join(d, "link"), f)
        with self.assertDies():
            be.gh_token({"gh_token_file": "link"}, "cfg", d, False)

    def test_non_string_rejected(self):
        with self.assertDies("non-empty path"):
            be.gh_token({"gh_token_file": 3}, "cfg", self.tmpdir(), False)

    def test_no_read_skips_file(self):
        self.assertIsNone(be.gh_token({"gh_token_file": "missing"}, "cfg",
                                      self.tmpdir(), False, read=False))


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
