"""Tests for build_prompt.py: system_prompts resolution and prompt assembly."""
import os

from jail_test_helpers import JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_prompt as bp
from build_packages import Packages
from jail_config import Root


class ResolveSegmentTests(JailTestCase):
    def seg(self, value, root, in_jail=True):
        return bp.resolve_segment(value, "cfg.json", root, self.roots(root),
                                  in_jail)

    def test_inline_string_verbatim(self):
        self.assertEqual(self.seg("hello", self.tmpdir()), "hello")

    def test_inline_empty_rejected(self):
        with self.assertDies("must not be empty"):
            self.seg("   ", self.tmpdir())

    def test_inline_nul_rejected(self):
        with self.assertDies("must not contain a NUL byte"):
            self.seg("a\x00b", self.tmpdir())

    def test_dict_path_read(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "from file")
        self.assertEqual(self.seg({"path": "p.md"}, root), "from file")

    def test_dict_extra_key_rejected(self):
        with self.assertDies("exactly a 'path' key"):
            self.seg({"path": "p.md", "x": 1}, self.tmpdir())

    def test_dict_path_not_string(self):
        with self.assertDies("'system_prompts.path'", "non-empty string"):
            self.seg({"path": 5}, self.tmpdir())

    def test_file_not_found(self):
        with self.assertDies("file not found"):
            self.seg({"path": "missing.md"}, self.tmpdir())

    def test_empty_file_rejected(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "   \n")
        with self.assertDies("is empty"):
            self.seg({"path": "p.md"}, root)

    def test_file_with_nul_rejected(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "ok\x00no")
        with self.assertDies("must not contain a NUL byte"):
            self.seg({"path": "p.md"}, root)

    def test_bad_segment_type(self):
        with self.assertDies("must be a string or a"):
            self.seg(123, self.tmpdir())


class PromptPathConfinementTests(JailTestCase):
    def test_in_jail_path_escaping_roots_rejected(self):
        root = self.tmpdir()
        with self.assertDies("escapes the jail"):
            bp.resolve_segment({"path": "../out/p.md"}, "cfg", root,
                               self.roots(root), True)

    def test_external_config_may_read_outside_roots(self):
        # An external (--config) prompt file is trusted and may live anywhere.
        root = self.tmpdir()
        outside = self.tmpdir()
        self.write(os.path.join(outside, "p.md"), "trusted")
        text = bp.resolve_segment({"path": os.path.join(outside, "p.md")},
                                  "cfg", root, self.roots(root), False)
        self.assertEqual(text, "trusted")


class UserPromptTests(JailTestCase):
    def up(self, data, root, in_jail=True):
        return bp.user_prompt(data, "cfg.json", root, self.roots(root), in_jail)

    def test_absent_is_none(self):
        self.assertIsNone(self.up({}, self.tmpdir()))

    def test_string(self):
        self.assertEqual(self.up({"system_prompts": "hi"}, self.tmpdir()), "hi")

    def test_dict(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "file")
        self.assertEqual(self.up({"system_prompts": {"path": "p.md"}}, root),
                         "file")

    def test_list_joined_with_blank_line(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "second")
        out = self.up({"system_prompts": ["first", {"path": "p.md"}]}, root)
        self.assertEqual(out, "first\n\nsecond")

    def test_invalid_type(self):
        with self.assertDies("must be a string, a"):
            self.up({"system_prompts": 5}, self.tmpdir())


class RootsSegmentTests(JailTestCase):
    def test_lists_workdir_and_roots(self):
        a, b = self.tmpdir(), self.tmpdir()
        out = bp.roots_segment(self.roots(a, b), "/workspace" + a)
        self.assertIn("# Project roots", out)
        self.assertIn("/workspace" + a, out)
        self.assertIn(f"- `/workspace{a}`", out)
        self.assertIn(f"- `/workspace{b}`", out)

    def test_read_only_root_is_flagged(self):
        a, b = self.tmpdir(), self.tmpdir()
        out = bp.roots_segment([Root(a, ["."], []), Root(b, [], [])],
                               "/workspace" + a)
        self.assertIn(f"- `/workspace{a}` (read-only)", out)
        # A read-write root carries no annotation: the bare line is present and
        # the read-only suffix is not.
        self.assertIn(f"- `/workspace{b}`\n", out + "\n")
        self.assertNotIn(f"- `/workspace{b}` (read-only)", out)


class PackagesSegmentTests(JailTestCase):
    def test_reports_pip_none_when_empty(self):
        # The section always renders; the pip line reports the absence (the venv
        # exists but is empty) and no apt/npm line appears.
        out = bp.packages_segment(Packages(apt=[], pip=[], npm=[]))
        self.assertIn("# Installed packages", out)
        self.assertIn("none requested", out)
        # Accurate read-only wording: tell the agent to ask, not that every
        # install "fails" — `pip install --user` succeeds, so guard that false
        # claim from creeping back in.
        self.assertIn("read-only", out)
        self.assertIn("after asking", out)
        self.assertNotIn("fails", out)
        self.assertNotIn("System packages", out)
        self.assertNotIn("Node packages", out)

    def test_lists_apt_packages(self):
        out = bp.packages_segment(Packages(apt=["jq", "ripgrep"], pip=[], npm=[]))
        self.assertIn("# Installed packages", out)
        self.assertIn("`jq`", out)
        self.assertIn("`ripgrep`", out)
        # apt present but pip empty still reports pip's absence.
        self.assertIn("none requested", out)

    def test_lists_pip_packages(self):
        out = bp.packages_segment(Packages(apt=[], pip=["ruff"], npm=[]))
        self.assertIn("# Installed packages", out)
        self.assertIn("`ruff`", out)
        # The pip line tells the agent the packages live in a venv on PATH, and
        # is accurate about the read-only venv (no false "install fails" claim).
        self.assertIn("venv", out)
        self.assertIn("read-only", out)
        self.assertNotIn("fails", out)
        self.assertNotIn("System packages", out)

    def test_lists_npm_packages(self):
        out = bp.packages_segment(Packages(apt=[], pip=["ruff"], npm=["eslint"]))
        self.assertIn("# Installed packages", out)
        self.assertIn("- Node packages (npm): `eslint` —", out)
        # Assert the npm line's OWN wording via fragments unique to it — the
        # always-present pip venv_note also contains "read-only", so a bare
        # assertIn("read-only") would pass even if the npm line dropped it.
        self.assertIn("NODE_PATH", out)       # require()-ability (npm-only)
        self.assertIn("read-only tree", out)  # pip says "read-only (root-owned)"
        self.assertIn("via yarn", out)        # the installer (npm-only)

    def test_npm_line_absent_when_empty(self):
        # Unlike pip, the npm line is conditional: with no npm packages it does
        # not render (node/npm work normally from the base image regardless).
        out = bp.packages_segment(Packages(apt=["jq"], pip=[], npm=[]))
        self.assertNotIn("Node packages", out)
        # The always-emitted pip line is still there.
        self.assertIn("none requested", out)

    def test_lists_all(self):
        out = bp.packages_segment(Packages(apt=["jq"], pip=["ruff"],
                                           npm=["eslint"]))
        self.assertIn("# Installed packages", out)
        # Each manager renders under its own label, in apt/npm/pip order.
        self.assertIn("- System packages (apt): `jq`.", out)
        self.assertIn("- Node packages (npm): `eslint` —", out)
        self.assertIn("- Python packages (pip): `ruff` —", out)
        self.assertIn("venv", out)
        self.assertLess(out.index("(apt)"), out.index("(npm)"))
        self.assertLess(out.index("(npm)"), out.index("(pip)"))


class MergeTests(JailTestCase):
    def test_base_and_roots_without_project_prompt(self):
        root = self.tmpdir()
        out = bp.merge("BASE", {}, "cfg.json", self.roots(root), True,
                       "/workspace" + root)
        # The base and the roots section are separated by a blank line.
        self.assertIn("BASE\n\n# Project roots", out)

    def test_order_base_then_roots_then_extra(self):
        root = self.tmpdir()
        out = bp.merge("BASE", {"system_prompts": "EXTRA"}, "cfg.json",
                       self.roots(root), True, "/workspace" + root)
        self.assertLess(out.index("BASE"), out.index("# Project roots"))
        self.assertLess(out.index("# Project roots"), out.index("EXTRA"))
        # Each boundary is a real blank-line separator, not just concatenation.
        self.assertIn("BASE\n\n# Project roots", out)
        self.assertIn("\n\nEXTRA", out)

    def test_packages_section_between_roots_and_extra(self):
        root = self.tmpdir()
        out = bp.merge("BASE", {"system_prompts": "EXTRA"}, "cfg.json",
                       self.roots(root), True, "/workspace" + root,
                       Packages(apt=["jq"], pip=[], npm=[]))
        self.assertLess(out.index("# Project roots"),
                        out.index("# Installed packages"))
        self.assertLess(out.index("# Installed packages"), out.index("EXTRA"))
        self.assertIn("`jq`", out)

    def test_packages_section_present_when_empty(self):
        # The section always renders, even with nothing installed: it reports the
        # pip venv's absence so the base prompt's pointer to it is never broken.
        root = self.tmpdir()
        out = bp.merge("BASE", {}, "cfg.json", self.roots(root), True,
                       "/workspace" + root, Packages(apt=[], pip=[], npm=[]))
        self.assertIn("# Installed packages", out)
        self.assertIn("none requested", out)
