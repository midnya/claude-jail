"""Tests for build_env.py: the non-filesystem settings (default_mode, mem_limit,
gh_token_file)."""
from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import io
import os
import subprocess
import urllib.error
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

    def test_fifo_refused(self):
        d = self.tmpdir()
        os.mkfifo(os.path.join(d, "tok"))
        with self.assertDies("regular file"):
            be.gh_token({"gh_token_file": "tok"}, "cfg", d, False)

    def test_non_string_rejected(self):
        with self.assertDies("non-empty path"):
            be.gh_token({"gh_token_file": 3}, "cfg", self.tmpdir(), False)

    def test_no_read_skips_file(self):
        self.assertIsNone(be.gh_token({"gh_token_file": "missing"}, "cfg",
                                      self.tmpdir(), False, read=False))


# Isolates the test repos from the host's git config (insteadOf rules etc.).
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class GitRepoCase(JailTestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, **remotes):
        d = self.tmpdir()
        self.git(d, "init", "-q")
        for name, url in remotes.items():
            self.git(d, "remote", "add", name, url)
        return d

    def git(self, d, *args):
        subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)


class RemoteUrlsTests(GitRepoCase):
    def test_not_a_repo_is_empty(self):
        self.assertEqual(be.remote_urls(self.tmpdir()), [])

    def test_fetch_and_push_urls(self):
        d = self.repo(origin="git@github.com:o/r.git")
        self.git(d, "remote", "set-url", "--push", "origin",
                 "https://github.com/o/push.git")
        self.assertEqual(be.remote_urls(d), [
            ("origin", "git@github.com:o/r.git"),
            ("origin", "https://github.com/o/push.git")])

    def test_insteadof_applied(self):
        d = self.repo(origin="gh:o/r")
        self.git(d, "config", "url.https://github.com/.insteadOf", "gh:")
        self.assertIn(("origin", "https://github.com/o/r"), be.remote_urls(d))

    def test_subdirectory_of_repo(self):
        d = self.repo(origin="git@github.com:o/r.git")
        sub = self.mkdir(os.path.join(d, "sub"))
        self.assertIn(("origin", "git@github.com:o/r.git"), be.remote_urls(sub))


class GithubRepoTests(JailTestCase):
    def test_github_forms(self):
        for url in ("git@github.com:o/r.git", "github.com:o/r",
                    "ssh://git@github.com/o/r.git",
                    "ssh://git@ssh.github.com:443/o/r",
                    "https://github.com/o/r", "https://github.com/o/r.git/",
                    "https://user:secret@GitHub.com/o/r.git",
                    "git://github.com/o/r.git"):
            with self.subTest(url=url):
                self.assertEqual(be.github_repo(url, "w"), "o/r")

    def test_dotted_names_kept(self):
        self.assertEqual(be.github_repo("git@github.com:my.org/a.b.git", "w"),
                         "my.org/a.b")

    def test_not_github(self):
        for url in ("git@gitlab.com:o/r.git", "https://example.com/o/r",
                    "/srv/git/r.git", "./rel:path", "file:///srv/r.git",
                    "https://github.com.evil.example/o/r"):
            with self.subTest(url=url):
                self.assertIsNone(be.github_repo(url, "w"))

    def test_unparseable_github_dies_without_echoing_url(self):
        with self.assertRaises(SystemExit) as cm:
            be.github_repo("https://u:secret@github.com/only-owner", "here")
        self.assertIn("here", str(cm.exception.code))
        self.assertNotIn("secret", str(cm.exception.code))


class RefuseWritableRemotesTests(GitRepoCase):
    def _check(self, *dirs, status=403, headers=None):
        probe = mock.patch.object(be, "_probe_write",
                                  return_value=(status, headers or {}))
        with probe as m:
            be.refuse_writable_remotes(TOKEN, self.roots(*dirs))
        return m

    def test_writable_dies_loudly(self):
        d = self.repo(origin="git@github.com:o/r.git")
        with self.assertDies("REFUSING TO START", "o/r", "remote 'origin'"):
            self._check(d, status=422)

    def test_read_only_and_no_access_pass(self):
        d = self.repo(origin="git@github.com:o/r.git")
        for status in (403, 404):
            with self.subTest(status=status):
                self._check(d, status=status)

    def test_probe_targets_the_repo(self):
        d = self.repo(origin="git@github.com:o/r.git")
        m = self._check(d)
        m.assert_called_once_with("o/r", TOKEN)

    def test_non_github_and_non_repo_not_probed(self):
        d = self.repo(origin="git@gitlab.com:o/r.git")
        self._check(d, self.tmpdir()).assert_not_called()

    def test_same_repo_probed_once(self):
        a = self.repo(origin="git@github.com:O/R.git")
        b = self.repo(up="https://github.com/o/r")
        self._check(a, b).assert_called_once()

    def test_any_writable_remote_refuses(self):
        d = self.repo(origin="git@github.com:o/ro.git",
                      fork="git@github.com:me/rw.git")
        with mock.patch.object(be, "_probe_write",
                               side_effect=lambda repo, _t:
                               (422 if repo == "me/rw" else 403, {})):
            with self.assertDies("me/rw"):
                be.refuse_writable_remotes(TOKEN, self.roots(d))

    def test_unexpected_status_fails_closed(self):
        d = self.repo(origin="git@github.com:o/r.git")
        for status in (401, 429, 500):
            with self.subTest(status=status):
                with self.assertDies("HTTP", "refusing to start"):
                    self._check(d, status=status)

    def test_rate_limited_403_fails_closed(self):
        d = self.repo(origin="git@github.com:o/r.git")
        for headers in ({"X-RateLimit-Remaining": "0"}, {"Retry-After": "60"}):
            with self.subTest(headers=headers):
                with self.assertDies("refusing to start"):
                    self._check(d, status=403, headers=headers)

    def test_network_error_fails_closed(self):
        d = self.repo(origin="git@github.com:o/r.git")
        with mock.patch.object(be, "_probe_write",
                               side_effect=OSError("unreachable")):
            with self.assertDies("unreachable", "refusing to start"):
                be.refuse_writable_remotes(TOKEN, self.roots(d))

    def test_message_never_holds_token(self):
        d = self.repo(origin="git@github.com:o/r.git")
        with self.assertRaises(SystemExit) as cm:
            self._check(d, status=422)
        self.assertNotIn(TOKEN, str(cm.exception.code))


class ProbeWriteTests(JailTestCase):
    def _http_error(self, url, code, location=None):
        headers = {"Location": location} if location else {}
        return urllib.error.HTTPError(url, code, "x", headers, io.BytesIO())

    def _probe(self, *responses):
        with mock.patch.object(be.urllib.request, "urlopen",
                               side_effect=list(responses)) as m:
            result = be._probe_write("o/r", TOKEN)
        return result, [c.args[0].full_url for c in m.call_args_list]

    def test_plain_status(self):
        (status, _h), urls = self._probe(self._http_error("u", 403))
        self.assertEqual(status, 403)
        self.assertEqual(urls, [f"{be.GITHUB_API}/repos/o/r/git/refs/"
                                f"{be.PROBE_REF}"])

    def test_follows_renamed_repo_redirect(self):
        moved = f"{be.GITHUB_API}/repositories/42/git/refs/{be.PROBE_REF}"
        (status, _h), urls = self._probe(self._http_error("u", 307, moved),
                                         self._http_error(moved, 422))
        self.assertEqual(status, 422)
        self.assertEqual(urls[1], moved)

    def test_redirect_off_api_host_not_followed(self):
        (status, _h), urls = self._probe(
            self._http_error("u", 307, "https://evil.example/steal"))
        self.assertEqual(status, 307)
        self.assertEqual(len(urls), 1)

    def test_redirect_loop_bounded(self):
        loop = f"{be.GITHUB_API}/repositories/42/x"
        errors = [self._http_error(loop, 307, loop)
                  for _ in range(be.MAX_REDIRECTS + 1)]
        (status, _h), urls = self._probe(*errors)
        self.assertEqual(status, 307)
        self.assertEqual(len(urls), be.MAX_REDIRECTS + 1)


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
