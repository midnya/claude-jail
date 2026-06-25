"""Tests for resolve_ids.py: the container user's numeric uid/gid."""
import contextlib
import io
from unittest import mock

from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import resolve_ids as ri

HOST = ri.Ids(4321, 8765)  # a host id pair distinct from the defaults


class ResolveTests(JailTestCase):
    def test_defaults_to_host_when_unset(self):
        self.assertEqual(ri.resolve({}, "cfg", host=HOST), HOST)

    def test_config_overrides_host(self):
        self.assertEqual(ri.resolve({"uid": 1500, "gid": 1600}, "cfg", host=HOST),
                         ri.Ids(1500, 1600))

    def test_each_id_falls_back_independently(self):
        # uid pinned, gid inherited from the host (and vice versa).
        self.assertEqual(ri.resolve({"uid": 1500}, "cfg", host=HOST),
                         ri.Ids(1500, HOST.gid))
        self.assertEqual(ri.resolve({"gid": 1600}, "cfg", host=HOST),
                         ri.Ids(HOST.uid, 1600))

    def _resolve_quiet(self, *args, **kwargs):
        """resolve() with the uid-0 root warning swallowed."""
        with contextlib.redirect_stderr(io.StringIO()):
            return ri.resolve(*args, **kwargs)

    def test_zero_is_accepted(self):
        # 0 (root) is a valid id here; the Dockerfile builds it with `useradd -o`,
        # and resolve() warns (test_uid_zero_warns) rather than rejecting it.
        self.assertEqual(self._resolve_quiet({"uid": 0, "gid": 0}, "cfg", host=HOST),
                         ri.Ids(0, 0))

    def test_uid_zero_warns(self):
        # uid 0 means the in-container agent runs as root, so the guards that
        # constrain it (the root-owned read-only venv) no longer apply — warn.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ri.resolve({"uid": 0}, "cfg", host=HOST)
        warning = err.getvalue()
        self.assertIn("uid 0", warning)
        self.assertIn("root", warning)

    def test_nonzero_uid_does_not_warn(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ri.resolve({}, "cfg", host=HOST)
        self.assertEqual(err.getvalue(), "")

    def test_max_id_accepted(self):
        self.assertEqual(ri.resolve({"uid": ri._MAX_ID}, "cfg", host=HOST).uid,
                         ri._MAX_ID)

    def test_non_integer_rejected(self):
        with self.assertDies("'uid' in", "must be an integer"):
            ri.resolve({"uid": "1000"}, "cfg", host=HOST)

    def test_float_rejected(self):
        with self.assertDies("'gid' in", "must be an integer"):
            ri.resolve({"gid": 1000.0}, "cfg", host=HOST)

    def test_bool_rejected(self):
        # True is an int subclass in Python, but a boolean is not an id.
        with self.assertDies("'uid' in", "must be an integer"):
            ri.resolve({"uid": True}, "cfg", host=HOST)

    def test_negative_rejected(self):
        with self.assertDies("'uid' in", "between 0 and"):
            ri.resolve({"uid": -1}, "cfg", host=HOST)

    def test_above_max_rejected(self):
        # 2^32 - 1 is the reserved (uid_t)-1 sentinel, just past the range.
        with self.assertDies("'gid' in", "between 0 and"):
            ri.resolve({"gid": ri._MAX_ID + 1}, "cfg", host=HOST)


class HostDefaultTests(JailTestCase):
    def test_host_default_reads_real_ids(self):
        # With no host override, resolve() reads the running process's ids.
        with mock.patch("os.getuid", return_value=1234, create=True), \
             mock.patch("os.getgid", return_value=5678, create=True):
            self.assertEqual(ri.resolve({}, "cfg"), ri.Ids(1234, 5678))

    def test_falls_back_when_platform_lacks_getuid(self):
        # On a platform without os.getuid (Windows), the baked-in default stands.
        with mock.patch.object(ri.os, "getuid", None, create=True):
            self.assertEqual(ri._host_default("getuid", ri.DEFAULT_UID),
                             ri.DEFAULT_UID)


class IdsTests(JailTestCase):
    def test_is_default_matches_dockerfile_args(self):
        self.assertTrue(ri.Ids(ri.DEFAULT_UID, ri.DEFAULT_GID).is_default())

    def test_is_default_false_when_either_differs(self):
        self.assertFalse(ri.Ids(ri.DEFAULT_UID + 1, ri.DEFAULT_GID).is_default())
        self.assertFalse(ri.Ids(ri.DEFAULT_UID, ri.DEFAULT_GID + 1).is_default())

    def test_digest_key_is_labeled_and_distinguishes_uid_gid(self):
        # Labeled sections, and swapping uid/gid yields a different key.
        self.assertIn("uid\n1500", ri.Ids(1500, 1600).digest_key())
        self.assertIn("gid\n1600", ri.Ids(1500, 1600).digest_key())
        self.assertNotEqual(ri.Ids(1500, 1600).digest_key(),
                            ri.Ids(1600, 1500).digest_key())
