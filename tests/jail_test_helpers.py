"""Shared bootstrap and helpers for the claude-jail unittest suite (stdlib only).

Puts src/ on sys.path so the jail modules import by bare name (as the launcher
does at runtime), loads the extension-less `claude-jail` launcher as an
importable module, and provides a JailTestCase base with a die()-aware assertion
and small on-disk scaffolding helpers (the config parsers do real realpath /
isdir / symlink checks, so most cases need genuine files and directories).
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from jail_config import Root  # noqa: E402 (needs src/ on sys.path first)


def load_launcher():
    """Load the extension-less `claude-jail` launcher as a module.

    spec_from_file_location can't infer a loader for a name with no recognised
    suffix, so drive a SourceFileLoader explicitly.
    """
    loader = SourceFileLoader("claude_jail_launcher", str(REPO_ROOT / "claude-jail"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class JailTestCase(unittest.TestCase):
    """TestCase with a die()-aware assertion and per-test temp scaffolding."""

    @contextlib.contextmanager
    def assertDies(self, *needles):
        """Assert the block calls die(): SystemExit whose message holds every needle.

        die() does ``sys.exit("Error: ...")``, so the exit code is that string.
        argparse's own errors exit with the int 2 instead, so this also pins the
        "Error:" prefix to keep the two apart.
        """
        with self.assertRaises(SystemExit) as cm:
            yield
        message = str(cm.exception.code)
        self.assertTrue(
            message.startswith("Error:"),
            f"expected a die() message starting with 'Error:', got {message!r}",
        )
        for needle in needles:
            self.assertIn(needle, message)

    def tmpdir(self):
        """A fresh temp directory, realpath'd, removed when the test ends.

        realpath because parse_roots/classify_config canonicalise root paths, so
        comparisons only hold against the resolved form (e.g. /tmp symlinks).
        """
        path = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def write(self, path, text=""):
        """Write a file (creating parent dirs); returns the path as a str."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return str(p)

    def mkdir(self, path):
        """Create a directory (and parents); returns its realpath as a str.

        realpath'd like tmpdir(): parse_roots/classify_config canonicalise root
        paths, so a created dir compared against them must be in resolved form
        too (e.g. when the temp root is reached through a symlink).
        """
        Path(path).mkdir(parents=True, exist_ok=True)
        return os.path.realpath(path)

    def symlink(self, link, target):
        """Create symlink `link` -> `target`; returns the link path as a str."""
        os.symlink(target, link)
        return str(link)

    def roots(self, *dirs):
        """Build jail Roots with empty read_only/hidden lists, one per dir."""
        return [Root(d, [], []) for d in dirs]
