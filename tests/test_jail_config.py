"""Tests for jail_config.py: the shared config reader and path-confinement core."""
import json
import os
from pathlib import Path
from unittest import mock

from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import jail_config as jc


class ReadConfigTests(JailTestCase):
    def test_absent_path_is_empty_config(self):
        missing = os.path.join(self.tmpdir(), "nope.json")
        self.assertEqual(jc.read_config(missing), {})

    def test_valid_object(self):
        path = self.write(os.path.join(self.tmpdir(), "c.json"),
                          '{"user": "me", "default_mode": "plan"}')
        self.assertEqual(jc.read_config(path),
                         {"user": "me", "default_mode": "plan"})

    def test_directory_is_an_error_not_an_absent_config(self):
        d = self.tmpdir()
        with self.assertDies("not a regular file"):
            jc.read_config(d)

    def test_invalid_json(self):
        path = self.write(os.path.join(self.tmpdir(), "c.json"), "{not json}")
        with self.assertDies("invalid JSON"):
            jc.read_config(path)

    def test_non_object_json(self):
        path = self.write(os.path.join(self.tmpdir(), "c.json"), "[1, 2]")
        with self.assertDies("must contain a JSON object"):
            jc.read_config(path)

    def test_unknown_top_level_key(self):
        path = self.write(os.path.join(self.tmpdir(), "c.json"),
                          '{"user": "me", "bogus": 1}')
        with self.assertDies("unknown top-level key", "bogus"):
            jc.read_config(path)

    def test_legacy_per_root_keys_at_top_level(self):
        path = self.write(os.path.join(self.tmpdir(), "c.json"),
                          '{"read_only": ["x"]}')
        with self.assertDies("now per-root keys"):
            jc.read_config(path)


class PathMappingTests(JailTestCase):
    def test_container_path_prefixes_workspace(self):
        self.assertEqual(jc.container_path("/home/x/proj"),
                         "/workspace/home/x/proj")

    def test_config_dir_is_resolved_parent(self):
        d = self.tmpdir()
        path = self.write(os.path.join(d, "c.json"), "{}")
        self.assertEqual(jc.config_dir(path), d)

    def test_inside(self):
        root = Path("/a/b")
        self.assertTrue(jc._inside(root, Path("/a/b")))        # self
        self.assertTrue(jc._inside(root, Path("/a/b/c/d")))    # descendant
        self.assertFalse(jc._inside(root, Path("/a")))         # ancestor
        self.assertFalse(jc._inside(root, Path("/a/bb")))      # sibling-ish


class ForbiddenRootTests(JailTestCase):
    def test_filesystem_root_forbidden(self):
        self.assertTrue(jc._forbidden_root(os.sep))

    def test_home_and_ancestors_forbidden(self):
        home = self.tmpdir()
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertTrue(jc._forbidden_root(home))                  # home itself
            self.assertTrue(jc._forbidden_root(str(Path(home).parent)))  # ancestor

    def test_unrelated_dir_allowed(self):
        home = self.tmpdir()
        other = self.tmpdir()
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertFalse(jc._forbidden_root(other))

    def test_without_home_only_root_forbidden(self):
        other = self.tmpdir()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(jc._forbidden_root(other))
            self.assertTrue(jc._forbidden_root(os.sep))


class ParseRootsTests(JailTestCase):
    def _config_in(self, d, data="{}"):
        return self.write(os.path.join(d, ".claude-jail.json"), data)

    def test_default_root_is_config_dir(self):
        d = self.tmpdir()
        cfg = self._config_in(d)
        roots = jc.parse_roots(jc.read_config(cfg), cfg)
        self.assertEqual([r.dir for r in roots], [d])

    def test_string_entry_shorthand(self):
        d = self.tmpdir()
        sub = self.mkdir(os.path.join(d, "sub"))
        cfg = self._config_in(d, '{"roots": ["sub"]}')
        roots = jc.parse_roots(jc.read_config(cfg), cfg)
        self.assertEqual([r.dir for r in roots], [sub])

    def test_object_entry_carries_lists(self):
        d = self.tmpdir()
        cfg = self._config_in(
            d, '{"roots": [{"path": ".", "read_only": ["a"], "hidden": ["b"]}]}')
        roots = jc.parse_roots(jc.read_config(cfg), cfg)
        self.assertEqual(roots[0].read_only, ["a"])
        self.assertEqual(roots[0].hidden, ["b"])

    def test_read_only_root_itself_parses(self):
        # "." is a valid read_only entry — it asks for a whole read-only root,
        # so the parser must accept it (the mount layer interprets it).
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": [{"path": ".", "read_only": ["."]}]}')
        roots = jc.parse_roots(jc.read_config(cfg), cfg)
        self.assertEqual(roots[0].read_only, ["."])
        self.assertTrue(roots[0].read_only_all())

    def test_relative_resolved_against_config_dir(self):
        d = self.tmpdir()
        sub = self.mkdir(os.path.join(d, "nested"))
        cfg = self._config_in(d, '{"roots": ["nested"]}')
        roots = jc.parse_roots(jc.read_config(cfg), cfg)
        self.assertEqual(roots[0].dir, sub)

    def test_absolute_path_taken_as_is(self):
        d = self.tmpdir()
        other = self.tmpdir()
        cfg = self._config_in(d, json.dumps({"roots": [other]}))
        roots = jc.parse_roots(jc.read_config(cfg), cfg)
        self.assertEqual(roots[0].dir, other)

    def test_roots_not_a_list(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": "."}')
        with self.assertDies("'roots'", "must be a list"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_roots_empty(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": []}')
        with self.assertDies("must not be empty"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_entry_wrong_type(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": [123]}')
        with self.assertDies("must be a string or"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_unknown_entry_key(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": [{"path": ".", "bogus": 1}]}')
        with self.assertDies("unknown key(s) in a 'roots' entry", "bogus"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_missing_path(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": [{"read_only": []}]}')
        with self.assertDies("'roots[].path'", "non-empty string"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_path_not_a_directory(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": ["does-not-exist"]}')
        with self.assertDies("root path is not a directory"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_read_only_not_a_list(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": [{"path": ".", "read_only": "x"}]}')
        with self.assertDies("'roots[].read_only'", "must be a list"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_forbidden_root_rejected(self):
        d = self.tmpdir()
        home = self.tmpdir()
        cfg = self._config_in(d, json.dumps({"roots": [home]}))
        with mock.patch.dict(os.environ, {"HOME": home}):
            with self.assertDies("a jail root may not be"):
                jc.parse_roots(jc.read_config(cfg), cfg)

    def test_duplicate_roots_rejected(self):
        d = self.tmpdir()
        cfg = self._config_in(d, '{"roots": [".", "."]}')
        with self.assertDies("roots overlap"):
            jc.parse_roots(jc.read_config(cfg), cfg)

    def test_nested_roots_rejected(self):
        d = self.tmpdir()
        self.mkdir(os.path.join(d, "child"))
        cfg = self._config_in(d, '{"roots": [".", "child"]}')
        with self.assertDies("roots overlap"):
            jc.parse_roots(jc.read_config(cfg), cfg)


class RootSelfTests(JailTestCase):
    def test_names_root_itself(self):
        for p in (".", "./", ".//.", "./."):
            self.assertTrue(jc.names_root_itself(p), p)
        for p in ("src", "a/b", "..", "../x", "/abs", ".git"):
            self.assertFalse(jc.names_root_itself(p), p)

    def test_read_only_all_true_for_root_entry(self):
        self.assertTrue(jc.Root("/r", ["."], []).read_only_all())
        self.assertTrue(jc.Root("/r", ["src", "./"], []).read_only_all())

    def test_read_only_all_false_otherwise(self):
        self.assertFalse(jc.Root("/r", [], []).read_only_all())
        self.assertFalse(jc.Root("/r", ["src"], []).read_only_all())
        # A "." under `hidden` does not make the root read-only.
        self.assertFalse(jc.Root("/r", [], ["."]).read_only_all())
        # An empty string is an invalid entry (requested_for_root die()s on it),
        # not a request for a read-only root — the two must agree.
        self.assertFalse(jc.Root("/r", [""], []).read_only_all())


class ResolveInRootTests(JailTestCase):
    def test_normal_relative(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "sub", "f"), "x")
        self.assertEqual(jc.resolve_in_root(root, "sub/f", "x"),
                         Path(root) / "sub" / "f")

    def test_absolute_rejected(self):
        root = self.tmpdir()
        with self.assertDies("must be relative to its root"):
            jc.resolve_in_root(root, "/etc/passwd", "read_only path")

    def test_dotdot_rejected(self):
        root = self.tmpdir()
        with self.assertDies("must be relative to its root"):
            jc.resolve_in_root(root, "../x", "read_only path")

    def test_symlink_escaping_root_rejected(self):
        root = self.tmpdir()
        outside = self.tmpdir()
        self.symlink(os.path.join(root, "link"), outside)
        with self.assertDies("escapes its root"):
            jc.resolve_in_root(root, "link/secret", "read_only path")

    def test_symlink_inside_root_allowed(self):
        root = self.tmpdir()
        self.mkdir(os.path.join(root, "real"))
        self.symlink(os.path.join(root, "link"), os.path.join(root, "real"))
        # Resolves to the in-root target, so it is accepted.
        self.assertEqual(jc.resolve_in_root(root, "link", "x"),
                         Path(root) / "real")


class WalkNoSymlinkTests(JailTestCase):
    def test_plain_path(self):
        base = self.tmpdir()
        self.assertEqual(jc._walk_no_symlink(base, "a/b", "x"),
                         Path(base) / "a" / "b")

    def test_symlink_component_refused(self):
        base = self.tmpdir()
        self.symlink(os.path.join(base, "link"), self.tmpdir())
        with self.assertDies("must not traverse a symlink"):
            jc._walk_no_symlink(base, "link/x", "x")

    def test_dotdot_climbs_lexically(self):
        base = self.tmpdir()
        self.assertEqual(jc._walk_no_symlink(base, "a/../b", "x"),
                         Path(base) / "b")

    def test_absolute_walks_from_filesystem_root(self):
        base = self.tmpdir()
        # An absolute rel walks from the filesystem root, ignoring base. The
        # target sits under a realpath'd tmpdir (no symlink components), so the
        # lexical path is returned unchanged.
        abs_rel = os.path.join(self.tmpdir(), "sub", "file")
        self.assertEqual(jc._walk_no_symlink(base, abs_rel, "x"),
                         Path(abs_rel))


class ConfineToRootsTests(JailTestCase):
    def test_inside_a_root(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "doc.md"), "x")
        self.assertEqual(
            jc.confine_to_roots(root, "doc.md", self.roots(root), "x"),
            Path(root) / "doc.md")

    def test_outside_every_root_dies(self):
        root = self.tmpdir()
        with self.assertDies("escapes the jail"):
            jc.confine_to_roots(root, "../escape/doc.md", self.roots(root), "x")

    def test_symlink_refused(self):
        root = self.tmpdir()
        self.symlink(os.path.join(root, "link"), self.tmpdir())
        with self.assertDies("must not traverse a symlink"):
            jc.confine_to_roots(root, "link/doc.md", self.roots(root), "x")


class TrustedHostPathTests(JailTestCase):
    def test_absolute_path_allowed_anywhere(self):
        base = self.tmpdir()
        # A trusted (--config) absolute path may live anywhere; under a
        # realpath'd tmpdir it has no symlink components and is returned as-is.
        abs_rel = os.path.join(self.tmpdir(), "anywhere", "p.md")
        self.assertEqual(jc.trusted_host_path(base, abs_rel, "x"),
                         Path(abs_rel))

    def test_symlink_still_refused(self):
        base = self.tmpdir()
        self.symlink(os.path.join(base, "link"), self.tmpdir())
        with self.assertDies("must not traverse a symlink"):
            jc.trusted_host_path(base, "link/x", "x")


class ClassifyConfigTests(JailTestCase):
    def test_config_inside_root_returns_rel(self):
        root = self.tmpdir()
        cfg = os.path.join(root, "sub", ".claude-jail.json")
        self.write(cfg, "{}")
        self.assertEqual(jc.classify_config(self.roots(root), cfg),
                         (root, "sub/.claude-jail.json"))

    def test_config_at_root_returns_none(self):
        # A config whose real path *is* a root dir (rel == ".") is not masked.
        root = self.tmpdir()
        self.assertIsNone(jc.classify_config(self.roots(root), root))

    def test_config_outside_roots_returns_none(self):
        root = self.tmpdir()
        cfg = self.write(os.path.join(self.tmpdir(), ".claude-jail.json"), "{}")
        self.assertIsNone(jc.classify_config(self.roots(root), cfg))

    def test_config_escaping_via_symlink_dies(self):
        # Lexically inside the root, but a symlink redirects the realpath out.
        root = self.tmpdir()
        outside = self.tmpdir()
        self.write(os.path.join(outside, ".claude-jail.json"), "{}")
        self.symlink(os.path.join(root, "link"), outside)
        cfg = os.path.join(root, "link", ".claude-jail.json")
        with self.assertDies("config file escapes the jail"):
            jc.classify_config(self.roots(root), cfg)
