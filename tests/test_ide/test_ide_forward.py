"""The ide-host forwarder's pure port discovery and reconcile (no sockets)."""
from pathlib import Path

from ide import forward
from jail_test_helpers import JailTestCase  # noqa: I001


class PortsFromLockfilesTests(JailTestCase):
    def test_port_is_the_filename(self):
        d = Path(self.tmpdir())
        for name in ("25179.lock", "47507.lock"):
            (d / name).write_text('{"authToken": "x"}')  # content is irrelevant
        (d / "abc.lock").write_text("{}")            # non-numeric stem -> skipped
        (d / "70000.lock").write_text("{}")          # out of range -> skipped
        (d / "63986.txt").write_text("{}")           # not *.lock -> ignored
        self.assertEqual(forward.ports_from_lockfiles(d), {25179, 47507})

    def test_missing_dir_is_empty(self):
        self.assertEqual(
            forward.ports_from_lockfiles(Path(self.tmpdir()) / "nope"), set())


class ReconcileTests(JailTestCase):
    def test_add_and_remove(self):
        self.assertEqual(forward.reconcile({1, 2, 3}, {2, 3, 4}), ({4}, {1}))

    def test_steady_state_is_noop(self):
        self.assertEqual(forward.reconcile({5, 6}, {5, 6}), (set(), set()))

    def test_from_empty(self):
        self.assertEqual(forward.reconcile(set(), {7, 8}), ({7, 8}, set()))
