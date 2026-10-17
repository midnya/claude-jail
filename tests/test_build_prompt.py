"""Tests for build_prompt.py: system_prompts resolution and prompt assembly."""
import os
from pathlib import Path
from unittest import mock

from jail_test_helpers import REPO_ROOT, JailTestCase  # noqa: I001 (puts src/ on sys.path)

import build_prompt as bp
from build_packages import Packages
from jail_config import Root


class ResolveSegmentTests(JailTestCase):
    def seg(self, value, root, in_jail=True):
        return bp.resolve_segment(value, "'system_prompts' in cfg.json", root,
                                  self.roots(root), in_jail)

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
        with self.assertDies("'system_prompts'", "path must be a non-empty string"):
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

    def test_fifo_swapped_in_after_check_refused(self):
        root = self.tmpdir()
        os.mkfifo(os.path.join(root, "p.md"))
        with mock.patch.object(Path, "is_file", return_value=True), \
                self.assertDies("regular file"):
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


class CliPromptTests(JailTestCase):
    """--system-prompt / --system-prompt-file segments. A --system-prompt-file
    path resolves like a config system_prompts.path (relative to the config dir,
    no symlink) but is operator-supplied, so it is trusted anywhere and NOT
    confined to the roots."""

    def cli(self, segments, cfg_dir):
        return bp.cli_prompt(segments, cfg_dir, self.roots(cfg_dir))

    def test_none_when_absent(self):
        self.assertIsNone(self.cli(None, self.tmpdir()))
        self.assertIsNone(self.cli([], self.tmpdir()))

    def test_inline_verbatim(self):
        self.assertEqual(self.cli([("inline", "hi")], self.tmpdir()), "hi")

    def test_path_read_relative_to_config_dir(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "from file")
        self.assertEqual(self.cli([("path", "p.md")], root), "from file")

    def test_segments_joined_in_command_line_order(self):
        root = self.tmpdir()
        self.write(os.path.join(root, "p.md"), "MID")
        out = self.cli([("inline", "A"), ("path", "p.md"), ("inline", "B")], root)
        self.assertEqual(out, "A\n\nMID\n\nB")

    def test_inline_empty_rejected_naming_the_flag(self):
        # The error attributes the bad segment to the CLI flag, not the config
        # file. The space in "--system-prompt argument" is load-bearing: it keeps
        # the needle from matching --system-prompt-file's label, so this pins the
        # error to the inline flag, not --system-prompt-file's.
        with self.assertDies("--system-prompt argument", "must not be empty"):
            self.cli([("inline", "  ")], self.tmpdir())

    def test_path_outside_roots_allowed(self):
        # Unlike an in-jail config's own system_prompts.path, a --system-prompt-file
        # is operator-supplied, so it is trusted to live anywhere and is NOT
        # confined to the roots — an absolute path outside every root is read.
        root = self.tmpdir()
        outside = self.tmpdir()
        self.write(os.path.join(outside, "p.md"), "trusted")
        self.assertEqual(
            self.cli([("path", os.path.join(outside, "p.md"))], root), "trusted")

    def test_relative_path_climbing_out_of_roots_allowed(self):
        # A trusted path may also climb out with `..`, which confine_to_roots
        # would reject for a config's own path.
        base = self.tmpdir()
        root = self.mkdir(os.path.join(base, "proj"))
        self.write(os.path.join(base, "shared.md"), "climbed")
        self.assertEqual(self.cli([("path", "../shared.md")], root), "climbed")

    def test_path_via_symlink_refused(self):
        # The no-symlink invariant survives the trust relaxation: an agent-planted
        # symlink inside a root still cannot redirect the read at a host secret.
        root = self.tmpdir()
        secret = self.tmpdir()
        self.write(os.path.join(secret, "secret.md"), "SECRET")
        self.symlink(os.path.join(root, "link.md"),
                     os.path.join(secret, "secret.md"))
        with self.assertDies("--system-prompt-file", "symlink"):
            self.cli([("path", "link.md")], root)

    def test_path_missing_rejected(self):
        # A missing file is attributed to the flag the user passed, not to a
        # 'system_prompts.path' config key they never set.
        with self.assertDies("--system-prompt-file", "file not found"):
            self.cli([("path", "nope.md")], self.tmpdir())


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

    def test_cli_segments_appended_after_config_prompt(self):
        # CLI --system-prompt/--system-prompt-file segments come after the
        # config's own system_prompts and keep their command-line order. The cfg
        # sits inside the root so the "p.md" path confines to it.
        root = self.tmpdir()
        cfg = os.path.join(root, ".claude-jail.json")
        self.write(os.path.join(root, "p.md"), "FILESEG")
        out = bp.merge("BASE", {"system_prompts": "CONFIG"}, cfg,
                       self.roots(root), True, "/workspace" + root,
                       cli_segments=[("inline", "CLITEXT"), ("path", "p.md")])
        self.assertLess(out.index("CONFIG"), out.index("CLITEXT"))
        self.assertLess(out.index("CLITEXT"), out.index("FILESEG"))
        # A real blank-line separator, not bare concatenation.
        self.assertIn("\n\nCLITEXT", out)

    def test_cli_segments_without_config_prompt(self):
        # With no config system_prompts the CLI segments still append, after the
        # generated sections.
        root = self.tmpdir()
        out = bp.merge("BASE", {}, "cfg.json", self.roots(root), True,
                       "/workspace" + root, cli_segments=[("inline", "ONLYCLI")])
        self.assertLess(out.index("# Installed packages"), out.index("ONLYCLI"))


class ReviewBaseTests(JailTestCase):
    """The git-review rewrite drops the base prompt's read-only-git claims."""

    def base(self):
        return (REPO_ROOT / "system-prompt.md").read_text(encoding="utf-8")

    def test_real_base_has_the_anchors(self):
        # Guards drift: if the base file is reworded, review_base() can no longer
        # find its bullets and would silently leave stale text, so it die()s —
        # here we assert it does NOT exit on the shipping base, proving the
        # anchors still match. (The failure mode is exercised separately below.)
        out = bp.review_base(self.base())
        self.assertNotEqual(out, self.base())

    def test_drops_no_git_writes_bullet(self):
        out = bp.review_base(self.base())
        self.assertNotIn("No git write operations", out)
        # The wrapped continuation line is indented with two spaces in the base;
        # match it exactly so this asserts the wrapped body went too, not a
        # substring that was never present regardless of review_base().
        self.assertNotIn("mounted\n  read-only, so these fail anyway", out)

    def test_drops_dot_git_from_read_only_paths_bullet(self):
        out = bp.review_base(self.base())
        # The read-only-paths bullet survives, but without its ".git" clause.
        self.assertIn("Some paths are mounted read-only", out)
        self.assertNotIn("Besides each root's `.git`", out)

    def test_unknown_base_dies_rather_than_noop(self):
        with self.assertRaises(SystemExit):
            bp.review_base("nothing git-related in here")


class MergeReviewTests(JailTestCase):
    def test_review_true_drops_git_readonly_claims(self):
        root = self.tmpdir()
        base = (REPO_ROOT / "system-prompt.md").read_text(encoding="utf-8")
        out = bp.merge(base, {}, "cfg.json", self.roots(root), True,
                       "/workspace" + root, review=True)
        self.assertNotIn("No git write operations", out)
        self.assertNotIn("Besides each root's `.git`", out)

    def test_review_false_keeps_git_readonly_claims(self):
        root = self.tmpdir()
        base = (REPO_ROOT / "system-prompt.md").read_text(encoding="utf-8")
        out = bp.merge(base, {}, "cfg.json", self.roots(root), True,
                       "/workspace" + root)
        self.assertIn("No git write operations", out)
        self.assertIn("Besides each root's `.git`", out)
