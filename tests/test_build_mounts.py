"""Tests for build_mounts.py: the compose volume override builder."""
import os
from pathlib import Path
from unittest import mock

from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_mounts as bm
from jail_config import Root


def mounts_for(read_only=None, hidden=None):
    """Run build_tree + resolve and return the (relpath, mode) mount list."""
    requested = {"read_only": list(read_only or []), "hidden": list(hidden or [])}
    out = []
    bm.resolve(bm.build_tree(requested), [], False, out)
    return out


class RequestedForRootTests(JailTestCase):
    def test_defaults(self):
        requested, mask_absent = bm.requested_for_root(Root("/r", [], []), None)
        self.assertEqual(requested["read_only"], [".git"])
        self.assertEqual(requested["hidden"], [bm.CONFIG_NAME])
        self.assertEqual(mask_absent, {bm.CONFIG_NAME})

    def test_root_lists_appended(self):
        requested, _ = bm.requested_for_root(Root("/r", ["ro"], ["hid"]), None)
        self.assertEqual(requested["read_only"], [".git", "ro"])
        self.assertEqual(requested["hidden"], [bm.CONFIG_NAME, "hid"])

    def test_active_config_masked_in_its_own_root(self):
        rel = "sub/.claude-jail.json"
        requested, mask_absent = bm.requested_for_root(
            Root("/r", [], []), ("/r", rel))
        self.assertIn(rel, requested["hidden"])
        self.assertIn(rel, mask_absent)

    def test_active_config_in_other_root_not_added(self):
        requested, mask_absent = bm.requested_for_root(
            Root("/r", [], []), ("/other", "x"))
        self.assertNotIn("x", requested["hidden"])
        self.assertEqual(mask_absent, {bm.CONFIG_NAME})

    def test_invalid_entry_type(self):
        with self.assertDies("invalid hidden entry for root"):
            bm.requested_for_root(Root("/r", [], [123]), None)

    def test_absolute_entry_rejected(self):
        with self.assertDies("hidden path must be relative to its root"):
            bm.requested_for_root(Root("/r", [], ["/abs"]), None)

    def test_dotdot_entry_rejected(self):
        with self.assertDies("read_only path must be relative to its root"):
            bm.requested_for_root(Root("/r", ["../x"], []), None)

    def test_root_itself_rejected(self):
        with self.assertDies("hidden path must not be the root itself"):
            bm.requested_for_root(Root("/r", [], ["."]), None)


class TreeResolveTests(JailTestCase):
    def test_single_read_only(self):
        self.assertEqual(mounts_for(read_only=["a"]), [("a", "read_only")])

    def test_single_hidden(self):
        self.assertEqual(mounts_for(hidden=["a"]), [("a", "hidden")])

    def test_hidden_trumps_read_only_at_same_path(self):
        self.assertEqual(mounts_for(read_only=["a"], hidden=["a"]),
                         [("a", "hidden")])

    def test_nested_read_only_is_redundant(self):
        # The outer read_only already covers the child, so it isn't re-emitted.
        self.assertEqual(mounts_for(read_only=["a", "a/b"]),
                         [("a", "read_only")])

    def test_hidden_below_read_only_still_emitted(self):
        self.assertEqual(mounts_for(read_only=["a"], hidden=["a/b"]),
                         [("a", "read_only"), ("a/b", "hidden")])

    def test_siblings_sorted(self):
        self.assertEqual(mounts_for(read_only=["b"], hidden=["a"]),
                         [("a", "hidden"), ("b", "read_only")])


class YamlPathTests(JailTestCase):
    def test_plain_path_quoted(self):
        self.assertEqual(bm._yaml_path("/a/b"), '"/a/b"')

    def test_dollar_doubled(self):
        self.assertEqual(bm._yaml_path("/a/$x"), '"/a/$$x"')

    def test_spaces_and_quotes_escaped(self):
        self.assertEqual(bm._yaml_path('/a "b"/c'), '"/a \\"b\\"/c"')


class BindStanzaTests(JailTestCase):
    def test_read_write_omits_read_only_line(self):
        lines = bm._bind_stanza("/s", "/t", read_only=False)
        self.assertEqual(lines, [
            "      - type: bind",
            '        source: "/s"',
            '        target: "/t"',
            "        bind:",
            "          create_host_path: false",
        ])

    def test_read_only_adds_flag(self):
        lines = bm._bind_stanza("/s", "/t", read_only=True)
        self.assertIn("        read_only: true", lines)

    def test_rw_bind_targets_workspace_mirror(self):
        lines = bm._rw_bind("/host/proj")
        self.assertIn('        source: "/host/proj"', lines)
        self.assertIn('        target: "/workspace/host/proj"', lines)
        self.assertNotIn("        read_only: true", lines)


class MaskVolumesTests(JailTestCase):
    def test_existing_read_only_file_bound(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "cfg.yml"), "x")
        volumes, seeds = bm._mask_volumes([("cfg.yml", "read_only")], root, set())
        text = "\n".join(volumes)
        self.assertIn(f'source: "{root}/cfg.yml"', text)
        self.assertIn("read_only: true", text)
        self.assertEqual(seeds, [])

    def test_absent_read_only_skipped(self):
        root = self.tmpdir()
        volumes, seeds = bm._mask_volumes([("gone", "read_only")], root, set())
        self.assertEqual((volumes, seeds), ([], []))

    def test_hidden_directory_uses_nocopy_volume(self):
        root = self.tmpdir()
        self.mkdir(os.path.join(root, "secret"))
        volumes, seeds = bm._mask_volumes([("secret", "hidden")], root, set())
        text = "\n".join(volumes)
        self.assertIn("type: volume", text)
        self.assertIn("nocopy: true", text)
        self.assertIn(f'target: "/workspace{root}/secret"', text)
        self.assertEqual(seeds, [])

    def test_hidden_existing_file_masked_with_empty_file(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "notes"), "secret")
        volumes, seeds = bm._mask_volumes([("notes", "hidden")], root, set())
        text = "\n".join(volumes)
        self.assertIn(str(bm.SCRIPT_DIR / bm.EMPTY_MASK), text)
        self.assertIn("read_only: true", text)
        self.assertEqual(seeds, [])

    def test_absent_config_protection_masked_and_seeded(self):
        root = self.tmpdir()
        rel = ".claude-jail.json"
        volumes, seeds = bm._mask_volumes([(rel, "hidden")], root, {rel})
        self.assertIn(str(bm.SCRIPT_DIR / bm.EMPTY_MASK), "\n".join(volumes))
        self.assertEqual(seeds, [f"{root}/{rel}"])

    def test_missing_hidden_path_is_an_error(self):
        root = self.tmpdir()
        with self.assertDies("hidden path not found in the jail"):
            bm._mask_volumes([("nope", "hidden")], root, set())

    def test_symlink_escape_refused(self):
        root = self.tmpdir()
        self.symlink(os.path.join(root, "link"), self.tmpdir())
        with self.assertDies("escapes its root"):
            bm._mask_volumes([("link/x", "read_only")], root, set())


class EnsureEmptyMaskTests(JailTestCase):
    def test_creates_when_absent(self):
        d = self.tmpdir()
        with mock.patch.object(bm, "SCRIPT_DIR", Path(d)):
            bm.ensure_empty_mask()
            mask = Path(d) / bm.EMPTY_MASK
            self.assertTrue(mask.is_file())
            self.assertEqual(mask.stat().st_size, 0)
            bm.ensure_empty_mask()  # idempotent

    def test_non_empty_mask_rejected(self):
        d = self.tmpdir()
        self.write(os.path.join(d, bm.EMPTY_MASK), "leak")
        with mock.patch.object(bm, "SCRIPT_DIR", Path(d)):
            with self.assertDies("must be empty"):
                bm.ensure_empty_mask()

    def test_directory_at_mask_path_rejected(self):
        d = self.tmpdir()
        self.mkdir(os.path.join(d, bm.EMPTY_MASK))
        with mock.patch.object(bm, "SCRIPT_DIR", Path(d)):
            with self.assertDies("not a regular file"):
                bm.ensure_empty_mask()

    def test_shipped_mask_is_present_and_empty(self):
        # The real repo file must exist and be empty for the masks to work.
        mask = bm.SCRIPT_DIR / bm.EMPTY_MASK
        self.assertTrue(mask.is_file())
        self.assertEqual(mask.stat().st_size, 0)


class SeedMaskedConfigsTests(JailTestCase):
    def test_creates_absent_with_default_object(self):
        path = os.path.join(self.tmpdir(), "deep", ".claude-jail.json")
        bm.seed_masked_configs([path])
        self.assertEqual(Path(path).read_text(), "{}\n")

    def test_does_not_overwrite_existing(self):
        path = self.write(os.path.join(self.tmpdir(), "c.json"), "KEEP")
        bm.seed_masked_configs([path])
        self.assertEqual(Path(path).read_text(), "KEEP")

    def test_unwritable_target_skipped_silently(self):
        # Parent is a file, so mkdir/write raise OSError; seed must just skip.
        parent = self.write(os.path.join(self.tmpdir(), "afile"), "")
        bad = os.path.join(parent, "child.json")
        bm.seed_masked_configs([bad])  # no exception
        self.assertFalse(os.path.exists(bad))


class OverrideTests(JailTestCase):
    def test_empty_roots_yields_empty_document(self):
        self.assertEqual(bm.override([], None), ("", []))

    def test_single_root_full_document(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "cfg.yml"), "x")
        self.mkdir(os.path.join(root, "secret"))
        document, seeds = bm.override(
            [Root(root, ["cfg.yml"], ["secret"])], None)

        self.assertTrue(document.startswith(
            "services:\n  claude:\n    volumes:\n"))
        # The root itself is bound read-write.
        self.assertIn(f'source: "{root}"', document)
        self.assertIn(f'target: "/workspace{root}"', document)
        # The explicit read_only file and hidden dir are present.
        self.assertIn(f'source: "{root}/cfg.yml"', document)
        self.assertIn("nocopy: true", document)
        # The absent active-config mask is scheduled for seeding.
        self.assertEqual(seeds, [f"{root}/{bm.CONFIG_NAME}"])

    def test_git_default_read_only_skipped_when_absent(self):
        # .git is read_only by default; with no .git on disk it is just omitted.
        root = self.tmpdir()
        document, _ = bm.override([Root(root, [], [])], None)
        self.assertNotIn(f'source: "{root}/.git"', document)
