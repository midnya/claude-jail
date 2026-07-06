"""Tests for src/review.py: bundle naming, classification, the unreviewed ->
accepted -> applied state machine, hazard detection, and launcher routing.

Each case builds genuine throwaway git repositories (a "user" repo and a
"workspace" clone standing in for the jailed agent's working copy) and real
bundles, so the git plumbing is exercised end to end rather than mocked. A fixed
identity and an isolated GIT_CONFIG keep commits reproducible and independent of
the host's git setup — and review.py shells out to the same inherited env.
"""
import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from jail_test_helpers import JailTestCase, load_launcher  # noqa: I001 (puts src/ on sys.path)

import jail_config
import jail_workspace
import review

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    # never drop into an editor for a merge commit message (apply's divergent path)
    "GIT_MERGE_AUTOEDIT": "no",
}

# Exactly the SGR codes _colorize emits (review._C_*): reset, bold, red, green,
# yellow, cyan. Stripping ONLY these — not any SGR — is what lets the security
# tests below prove "only our own colour": a foreign SGR that slipped through
# _sanitize (a different code, or 256-colour `38;5;n`) survives into `leftover`
# and trips the assertion, instead of being erased as if it were ours.
_OUR_SGR = r"\x1b\[(?:0|1|31|32|33|36)m"
_OUR_SGR_B = rb"\x1b\[(?:0|1|31|32|33|36)m"


class ReviewBase(JailTestCase):
    """Git scaffolding: a user repo, a workspace clone, commits and bundles."""

    def setUp(self):
        for key, value in _GIT_ENV.items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)

    def git(self, repo, *args, check=True, stdin=None):
        r = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, input=stdin)
        if check and r.returncode != 0:
            self.fail(f"git {' '.join(args)} in {repo} failed: {r.stderr}")
        return r.stdout.strip()

    def rev(self, repo, ref):
        return self.git(repo, "rev-parse", "--verify", ref)

    def commit(self, repo, name, content, message):
        self.write(Path(repo) / name, content)
        self.git(repo, "add", name)
        self.git(repo, "commit", "--no-gpg-sign", "-m", message)
        return self.rev(repo, "HEAD")

    def init_repo(self):
        """A fresh repo on `main` with one commit; returns (path, sha)."""
        repo = self.tmpdir()
        self.git(repo, "init", "-q", "-b", "main")
        return repo, self.commit(repo, "file", "0\n", "init")

    def clone(self, src):
        """A self-contained clone of `src`; returns its path."""
        dst = os.path.join(self.tmpdir(), "ws")
        r = subprocess.run(["git", "clone", "-q", src, dst],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.fail(f"git clone failed: {r.stderr}")
        return dst

    def materialize_workdir(self, original):
        """A live working tree (= original's HEAD) with an empty .git volume,
        standing in for the jail's live-tree mount + empty shadow .git."""
        wd = os.path.join(self.tmpdir(), "wd")
        subprocess.run(["git", "clone", "-q", original, wd],
                       capture_output=True, text=True, check=True)
        shutil.rmtree(os.path.join(wd, ".git"))
        os.mkdir(os.path.join(wd, ".git"))
        return wd

    def materialize_copy(self, repo):
        """Copy a worktree verbatim (keeps populated submodules) with an empty .git.

        A plain clone wouldn't check out submodules; the real jail mount is the
        host's live tree, so copy it instead and blank the .git into the empty
        shadow volume.
        """
        wd = os.path.join(self.tmpdir(), "wd")
        shutil.copytree(repo, wd, symlinks=True)
        shutil.rmtree(os.path.join(wd, ".git"))
        os.mkdir(os.path.join(wd, ".git"))
        return wd

    def make_bundle(self, workspace, exclude=()):
        """Bundle `workspace`'s refs, excluding history reachable from `exclude`."""
        path = os.path.join(self.tmpdir(), "work.bundle")
        args = ["bundle", "create", path, "--all"]
        if exclude:
            args += ["--not", *exclude]
        self.git(workspace, *args)
        return path

    def quarantine(self, repo, name, kind, ref, sha):
        """Plant a quarantine ref directly, standing in for an import."""
        self.git(repo, "update-ref",
                 f"{review.REF_PREFIX}/unreviewed/{name}/{kind}/{ref}", sha)

    def import_branch(self, repo, ws, base, branch="main", name="b"):
        """Bundle the workspace, import into `repo`, return the quarantined sha.

        Classification reads the incoming objects, so they must be in `repo`'s
        store — i.e. imported — first; this mirrors the real order (import then
        review) and returns the sha now living under the quarantine ref.
        """
        review.import_bundle(repo, self.make_bundle(ws, exclude=[base]), name)
        return review.head_sha(
            repo, f"{review.REF_PREFIX}/unreviewed/{name}/heads/{branch}")


# ---------------------------------------------------------------------------

class NamingTests(ReviewBase):
    def test_slug_sanitises_and_falls_back(self):
        self.assertEqual(review._slug("My Feature/2!"), "My-Feature-2")
        self.assertEqual(review._slug("///"), "session")

    def test_bundle_name_is_session_plus_content_sha1(self):
        path = os.path.join(self.tmpdir(), "b")
        Path(path).write_bytes(b"hello bundle")
        digest = hashlib.sha1(b"hello bundle").hexdigest()
        self.assertEqual(review.bundle_name("feat x", path), f"feat-x-{digest}")
        self.assertEqual(review.bundle_name(None, path), f"session-{digest}")


class ClassifyTests(ReviewBase):
    def test_new_branch(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")
        self.commit(ws, "n", "1\n", "feat")
        f1 = self.import_branch(repo, ws, base, branch="feature")
        info = review.classify(repo, "feature", f1)
        self.assertEqual(info["class"], "new")
        self.assertEqual(info["added"], 1)

    def test_fast_forward(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "file", "1\n", "more")
        m1 = self.import_branch(repo, ws, base)
        info = review.classify(repo, "main", m1, base)
        self.assertEqual(info["class"], "fast-forward")
        self.assertEqual(info["added"], 1)
        self.assertEqual(info["dropped"], 0)

    def test_unchanged_and_behind(self):
        repo, base = self.init_repo()
        m1 = self.commit(repo, "file", "1\n", "advance")  # user moves ahead
        self.assertEqual(review.classify(repo, "main", m1, base)["class"],
                         "unchanged")
        self.assertEqual(review.classify(repo, "main", base, base)["class"],
                         "behind")

    def test_rewrite_when_base_equals_current(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        amended = self.import_branch(repo, ws, base)
        info = review.classify(repo, "main", amended, base)
        self.assertEqual(info["class"], "rewrite")

    def test_rewrite_without_manifest_is_conservative(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        amended = self.import_branch(repo, ws, base)
        # No base sha supplied -> any non-fast-forward is reported as a rewrite.
        self.assertEqual(review.classify(repo, "main", amended)["class"],
                         "rewrite")

    def test_divergent_with_manifest(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "other", "ws\n", "agent side")    # agent advances
        wsha = self.import_branch(repo, ws, base)
        self.commit(repo, "file", "user\n", "user side")  # user advances too
        info = review.classify(repo, "main", wsha, base)
        self.assertEqual(info["class"], "divergent")


class StateMachineTests(ReviewBase):
    def _import_new_feature(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")
        f1 = self.commit(ws, "n", "1\n", "feat")
        bundle = self.make_bundle(ws, exclude=[base])
        review.import_bundle(repo, bundle, "b")
        return repo, base, f1

    def test_import_lands_only_in_unreviewed(self):
        repo, base, f1 = self._import_new_feature()
        self.assertEqual(review.bundle_list(repo, "unreviewed"), ["b"])
        self.assertEqual(review.bundle_list(repo, "accepted"), [])
        # The real feature branch must not exist yet.
        self.assertIsNone(review.head_sha(repo, "refs/heads/feature"))

    def test_accept_moves_unreviewed_to_accepted(self):
        repo, base, f1 = self._import_new_feature()
        review.accept(repo, "b")
        self.assertEqual(review.bundle_list(repo, "unreviewed"), [])
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])

    def test_reject_clears_refs_and_artifact(self):
        repo, base, f1 = self._import_new_feature()
        spool = Path(self.tmpdir())
        (spool / "b.bundle").write_bytes(b"x")
        review.reject(repo, "b", spool)
        self.assertEqual(review.bundle_list(repo, "unreviewed"), [])
        self.assertFalse((spool / "b.bundle").exists())

    def test_resolve_bundle_by_prefix(self):
        repo, base, f1 = self._import_new_feature()
        self.assertEqual(review.resolve_bundle(repo, "unreviewed", "b"), "b")

    def test_resolve_bundle_reports_no_match_and_ambiguity(self):
        repo, base = self.init_repo()
        # nothing imported: a plain "not found", no candidate list
        with self.assertDies("no unreviewed bundle matching 'x'"):
            review.resolve_bundle(repo, "unreviewed", "x")
        self.quarantine(repo, "feat-aaa", "heads", "main", base)
        self.quarantine(repo, "feat-bbb", "heads", "main", base)
        # a shared prefix is ambiguous and names the candidates
        with self.assertDies("ambiguous", "feat-aaa", "feat-bbb"):
            review.resolve_bundle(repo, "unreviewed", "feat-")
        # a miss now lists what is available
        with self.assertDies("no unreviewed bundle matching 'zzz'", "have:"):
            review.resolve_bundle(repo, "unreviewed", "zzz")
        with self.assertDies("no unreviewed bundle"):
            review.resolve_bundle(repo, "unreviewed", "zzz")


class ApplyTests(ReviewBase):
    def test_apply_creates_new_branch(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")
        f1 = self.commit(ws, "n", "1\n", "feat")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        review.apply_bundle(repo, "b")
        self.assertEqual(review.head_sha(repo, "refs/heads/feature"), f1)
        self.assertEqual(review.bundle_list(repo, "accepted"), [])

    def test_apply_fast_forwards_checked_out_branch(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        m1 = self.commit(ws, "file", "1\n", "more")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        review.apply_bundle(repo, "b")
        self.assertEqual(self.rev(repo, "main"), m1)

    def test_apply_rewrite_gated_by_allow_rewrite(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        amended = self.rev(ws, "HEAD")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        # Refused without the flag: real branch untouched, ref kept in accepted.
        review.apply_bundle(repo, "b", allow_rewrite=False)
        self.assertEqual(self.rev(repo, "main"), base)
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])
        # Allowed with the flag: the checked-out branch is hard-reset.
        review.apply_bundle(repo, "b", allow_rewrite=True)
        self.assertEqual(self.rev(repo, "main"), amended)
        self.assertEqual(review.bundle_list(repo, "accepted"), [])

    def test_apply_rewrite_backs_up_replaced_tip(self):
        # The guard-less reset --hard must first stash the pre-rewrite tip under
        # refs/jail-review/replaced/, so committed work it discards stays reachable
        # (not just in a gc-able reflog) — the fail-open no-manifest-base case.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", allow_rewrite=True)  # no manifest
        backup = f"{review.REF_PREFIX}/replaced/b/main"
        self.assertEqual(review.head_sha(repo, backup), base)  # old tip preserved
        self.assertTrue(any("previous tip" in line for line in log))

    def test_apply_fast_forwards_non_checked_out_branch(self):
        repo, base = self.init_repo()
        self.git(repo, "branch", "feature")           # feature at base, HEAD on main
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")
        f1 = self.commit(ws, "n", "1\n", "feat")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b")
        self.assertEqual(self.rev(repo, "feature"), f1)  # update-ref, not merge
        self.assertEqual(self.rev(repo, "main"), base)   # checked-out branch untouched
        self.assertEqual(review.bundle_list(repo, "accepted"), [])
        self.assertTrue(any("fast-forwarded" in line for line in log))

    def test_apply_divergent_checked_out_merges_clean(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        w1 = self.commit(ws, "a", "ws\n", "ws work")     # diverges on file a
        self.commit(repo, "b", "user\n", "user work")    # main advances on file b
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", manifest={"branches": {"main": base}})
        self.assertTrue(review._is_ancestor(repo, w1, self.rev(repo, "main")))
        parents = self.git(repo, "log", "-1", "--format=%P", "main").split()
        self.assertEqual(len(parents), 2)                # a real merge commit
        self.assertEqual(review.bundle_list(repo, "accepted"), [])
        self.assertTrue(any("merged" in line for line in log))

    def test_apply_divergent_checked_out_conflict_kept(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "file", "ws\n", "ws edit")       # same file, both sides
        u1 = self.commit(repo, "file", "user\n", "user edit")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", manifest={"branches": {"main": base}})
        self.assertTrue(any("conflict" in line for line in log))
        self.assertEqual(self.rev(repo, "main"), u1)     # left for the user to resolve
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])  # kept

    def test_apply_divergent_non_checked_out_kept(self):
        repo, base = self.init_repo()
        self.git(repo, "checkout", "-q", "-b", "feature")
        u1 = self.commit(repo, "u", "user\n", "user feat")
        self.git(repo, "checkout", "-q", "main")
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")  # branch from base, not u1
        self.commit(ws, "w", "ws\n", "ws feat")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", manifest={"branches": {"feature": base}})
        self.assertEqual(self.rev(repo, "feature"), u1)  # untouched while off-HEAD
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])  # kept
        self.assertTrue(any("check it out" in line for line in log))

    def test_apply_rewrite_refused_on_dirty_checked_out(self):
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        self.write(Path(repo) / "file", "uncommitted\n")  # dirty working tree
        log = review.apply_bundle(repo, "b", allow_rewrite=True)
        self.assertTrue(any("refusing" in line.lower() for line in log))
        self.assertEqual(self.rev(repo, "main"), base)    # not reset, ref kept
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])

    def test_apply_fast_forward_blocked_by_dirty_tree_keeps_bundle(self):
        # An uncommitted edit to a file the ff would touch must not die() the whole
        # apply (cf. the rewrite dirty-tree guard): the branch is left accepted.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "file", "1\n", "more")            # ff that touches `file`
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        self.write(Path(repo) / "file", "uncommitted\n")  # dirty the ff target
        log = review.apply_bundle(repo, "b")              # must not raise
        self.assertTrue(any("fast-forward blocked" in line for line in log))
        self.assertEqual(self.rev(repo, "main"), base)    # not advanced
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])  # kept

    def test_apply_rewrite_force_updates_non_checked_out(self):
        repo, base = self.init_repo()
        self.git(repo, "checkout", "-q", "-b", "feature")
        self.commit(repo, "f", "1\n", "feat")
        self.git(repo, "checkout", "-q", "main")
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "feature")        # DWIM to feature's tip
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        c1r = self.rev(ws, "feature")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", allow_rewrite=True)
        self.assertEqual(self.rev(repo, "feature"), c1r)  # forced, no working tree
        self.assertEqual(self.rev(repo, "main"), base)    # other branch untouched
        self.assertEqual(review.bundle_list(repo, "accepted"), [])
        self.assertTrue(any("force-updated" in line for line in log))

    def test_apply_unchanged_and_behind_are_noops(self):
        repo, base = self.init_repo()
        # unchanged: the quarantined tip equals the repo's current branch tip
        self.quarantine(repo, "same", "heads", "main", base)
        review.accept(repo, "same")
        log = review.apply_bundle(repo, "same")
        self.assertTrue(any("unchanged" in line for line in log))
        self.assertEqual(self.rev(repo, "main"), base)
        self.assertEqual(review.bundle_list(repo, "accepted"), [])
        # behind: the repo advances past a tip the quarantine still carries
        u1 = self.commit(repo, "file", "1\n", "advance")
        self.quarantine(repo, "old", "heads", "main", base)
        review.accept(repo, "old")
        log = review.apply_bundle(repo, "old")
        self.assertTrue(any("behind" in line for line in log))
        self.assertEqual(self.rev(repo, "main"), u1)      # not rewound
        self.assertEqual(review.bundle_list(repo, "accepted"), [])

    def test_apply_clears_non_branch_kinds(self):
        # apply lands only branches; non-branch refs (tags/replace/notes) are
        # skipped AND cleared from accepted, so a bundle made only of them resolves
        # instead of lingering forever and being re-applied on every `apply`.
        repo, base = self.init_repo()
        c2 = self.commit(repo, "file", "2\n", "second")
        self.quarantine(repo, "b", "tags", "v9", c2)
        self.quarantine(repo, "b", "replace", base, c2)
        self.quarantine(repo, "b", "notes", "commits", c2)
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b")
        self.assertEqual(sum("lands only branches" in line for line in log), 3)
        self.assertIsNone(review.head_sha(repo, "refs/tags/v9"))       # not created
        self.assertEqual(review.bundle_list(repo, "accepted"), [])     # cleared

    def test_apply_rewrite_refuses_to_drop_post_accept_commits(self):
        # The user advances a branch AFTER accepting a rewrite. classify() is
        # recomputed at apply time so its CAS old-value always matches — the
        # manifest-base guard is what stops the reset --hard from discarding the
        # new commit. It must refuse and keep the bundle accepted.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        u1 = self.commit(repo, "mine", "after\n", "user work after accepting")
        log = review.apply_bundle(repo, "b", allow_rewrite=True,
                                  manifest={"branches": {"main": base}})
        self.assertTrue(any("discard commit" in line for line in log))
        self.assertEqual(self.rev(repo, "main"), u1)                   # not reset
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])  # kept

    def test_apply_stops_after_midloop_merge_conflict(self):
        # A divergent checked-out branch (main) that conflicts must stop the apply
        # loop so the remaining branches aren't landed atop a mid-merge tree (the
        # entry op-in-progress guard only runs once). A second branch sorted after
        # main must therefore stay un-applied.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "file", "ws\n", "ws edit")       # conflicts with user edit
        self.git(ws, "checkout", "-q", "-b", "zzz")      # sorts after main
        self.commit(ws, "newf", "x\n", "zzz work")
        self.commit(repo, "file", "user\n", "user edit")  # main diverges -> conflict
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", manifest={"branches": {"main": base}})
        self.assertTrue(any("conflict" in line for line in log))
        self.assertTrue(any("stopping" in line for line in log))
        self.assertIsNone(review.head_sha(repo, "refs/heads/zzz"))     # not reached
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])  # both kept


class HazardTests(ReviewBase):
    def test_replace_ref_is_flagged(self):
        repo, base = self.init_repo()
        c2 = self.commit(repo, "file", "2\n", "second")
        self.quarantine(repo, "b", "replace", base, c2)
        report = review.analyze(repo, "unreviewed", "b")
        self.assertTrue(any("replace ref" in h for h in report["hazards"]))
        self.assertTrue(review._has_rewrite(report))

    def test_repointed_tag_is_flagged(self):
        repo, base = self.init_repo()
        c2 = self.commit(repo, "file", "2\n", "second")
        self.git(repo, "tag", "v1", base)          # existing immutable tag
        self.quarantine(repo, "b", "tags", "v1", c2)  # bundle moves it
        report = review.analyze(repo, "unreviewed", "b")
        self.assertTrue(any("re-pointed" in h for h in report["hazards"]))

    def test_matching_tag_is_not_flagged(self):
        repo, base = self.init_repo()
        self.git(repo, "tag", "v1", base)
        self.quarantine(repo, "b", "tags", "v1", base)  # same object
        report = review.analyze(repo, "unreviewed", "b")
        self.assertEqual(report["hazards"], [])

    def test_deleted_branch_surfaces_as_hazard(self):
        # A branch present in the setup manifest but absent from the session's
        # end_heads was deleted — a fact a bundle can't carry, surfaced only via
        # the manifest analyze() folds in.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "n", "1\n", "agent work")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        manifest = {"branches": {"main": base, "doomed": base},
                    "end_heads": ["main"]}
        report = review.analyze(repo, "unreviewed", "b", manifest)
        self.assertTrue(any("doomed deleted" in h for h in report["hazards"]))


class ConfigKeyTests(ReviewBase):
    def test_review_key_defaults_off_and_parses_bool(self):
        self.assertFalse(jail_config.review_enabled({}, "cfg"))
        self.assertTrue(jail_config.review_enabled({"review": True}, "cfg"))
        with self.assertDies("must be true or false"):
            jail_config.review_enabled({"review": "yes"}, "cfg")


class WorkspaceFlowTests(ReviewBase):
    """The container-side init/export (jail_workspace) meeting host-side finalize."""

    def test_init_attaches_reference_clone_preserving_dirty_tree(self):
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        self.write(Path(wd) / "file", "DIRTY\n")        # live edit before init
        jail_workspace.init(wd, f"{repo}/.git")
        self.assertEqual(self.rev(wd, "HEAD"), base)     # HEAD tracks the original
        self.assertIn("file", self.git(wd, "status", "--porcelain"))  # diff survives
        # objects are borrowed via an alternate, not copied in
        self.assertTrue((Path(wd) / ".git/objects/info/alternates").is_file())

    def test_prepare_resets_gitdir_and_seeds_base(self):
        repo, base = self.init_repo()
        root = self.tmpdir()
        ctx = {"repo": repo, "spool": os.path.join(root, "spool"),
               "gitdir": os.path.join(root, "spool", "gitdir")}
        stale = Path(ctx["gitdir"]) / "stale"
        stale.parent.mkdir(parents=True)
        stale.write_text("old")
        review.prepare(ctx)
        self.assertFalse(stale.exists())                 # gitdir wiped fresh
        self.assertTrue(Path(ctx["gitdir"]).is_dir())
        self.assertEqual(ctx["base"], {"main": base})    # base map recorded
        seeded = (Path(ctx["spool"]) / "pending.base").read_text().split()
        self.assertEqual(seeded, [base])                 # base tip seeded

    def test_export_then_finalize_imports_into_quarantine(self):
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.commit(wd, "n", "1\n", "agent work")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        self.assertTrue((spool / "pending.bundle").is_file())
        name = review.finalize(repo, spool, {"main": base}, "sess")
        self.assertTrue(name.startswith("sess-"))
        self.assertEqual(review.bundle_list(repo, "unreviewed"), [name])
        self.assertFalse((spool / "pending.base").exists())  # pending cleared
        self.assertFalse((spool / "pending.end").exists())
        review.accept(repo, name)
        review.apply_bundle(repo, name)
        self.assertEqual(self.rev(repo, "main"), self.rev(wd, "HEAD"))

    def test_finalize_writes_artifacts_to_shared_review_dir(self):
        # The per-run spool is `.../review/<jail_id>` (also bound into the jail);
        # finalize must drop the named bundle/manifest one level up in the shared
        # `.../review` dir — exactly where dispatch's _spool reads — not inside the
        # per-jail spool. (The bug the spool-path mismatch finding caught.)
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.commit(wd, "n", "1\n", "agent work")
        review_dir = Path(self.tmpdir()) / "review"
        spool = review_dir / "jail-id-slug"
        spool.mkdir(parents=True)
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        name = review.finalize(repo, spool, {"main": base}, "sess")
        self.assertIsNotNone(name)
        # artifacts in the shared dir...
        self.assertTrue((review_dir / f"{name}.bundle").is_file())
        self.assertTrue((review_dir / f"{name}.manifest.json").is_file())
        # ...not left inside the per-jail spool bound into the next session
        self.assertFalse((spool / f"{name}.bundle").exists())
        # and the loader dispatch builds over the shared dir finds the manifest
        self.assertIsNotNone(review._manifest_loader(review_dir)(name))

    def test_init_handles_jail_init_sentinel_branch_collision(self):
        # The original literally has a branch named __jail_init__ (the sentinel
        # HEAD parks on); --update-head-ok keeps the all-branches fetch from
        # refusing to update the "checked-out" sentinel, so init still succeeds.
        repo, base = self.init_repo()
        self.git(repo, "branch", "__jail_init__", base)
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.assertEqual(self.rev(wd, "HEAD"), base)
        self.assertEqual(self.git(wd, "symbolic-ref", "HEAD"), "refs/heads/main")

    def test_init_handles_default_branch_name_collision(self):
        # The original's branch matches git init's default (master); the sentinel
        # HEAD keeps the all-branches fetch from being refused on a checked-out
        # branch of the same name.
        repo = self.tmpdir()
        self.git(repo, "init", "-q", "-b", "master")
        base = self.commit(repo, "file", "0\n", "init")
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.assertEqual(self.rev(wd, "HEAD"), base)
        self.assertEqual(self.git(wd, "symbolic-ref", "HEAD"),
                         "refs/heads/master")

    def test_submodule_worktrees_keep_working(self):
        # A submodule origin embedded in a superproject. After init the shadow
        # .git must carry the submodule gitdir, or the live submodule worktree
        # reads as "not a git repository".
        sub = self.tmpdir()
        self.git(sub, "init", "-q", "-b", "main")
        sub_head = self.commit(sub, "s.txt", "s\n", "sub init")
        repo, _base = self.init_repo()
        self.git(repo, "-c", "protocol.file.allow=always",
                 "submodule", "add", "-q", sub, "sub")
        self.git(repo, "commit", "-q", "--no-gpg-sign", "-m", "add submodule")
        wd = self.materialize_copy(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        # superproject status no longer fatals and is clean
        self.assertEqual(self.git(wd, "status", "--porcelain"), "")
        # the submodule worktree is a real repo at the right commit
        self.assertEqual(self.git(os.path.join(wd, "sub"), "rev-parse", "HEAD"),
                         sub_head)
        # and it is registered, so `git submodule` tooling sees it (no '-' prefix)
        status = self.git(wd, "submodule", "status")
        self.assertIn("sub", status)
        self.assertFalse(status.lstrip().startswith("-"))

    def test_submodule_with_missing_original_gitdir_does_not_abort(self):
        # The live .git pointer exists but the original .git/modules is gone (a
        # submodule cloned then pruned, a never-fetched private submodule). The
        # superproject status must not abort — an empty repo is left in its place.
        sub = self.tmpdir()
        self.git(sub, "init", "-q", "-b", "main")
        self.commit(sub, "s.txt", "s\n", "sub init")
        repo, _base = self.init_repo()
        self.git(repo, "-c", "protocol.file.allow=always",
                 "submodule", "add", "-q", sub, "nested/dep")
        self.git(repo, "commit", "-q", "--no-gpg-sign", "-m", "add submodule")
        wd = self.materialize_copy(repo)
        # drop the original's modules dir, keeping the live worktree .git pointer
        shutil.rmtree(os.path.join(repo, ".git", "modules"))
        jail_workspace.init(wd, f"{repo}/.git")
        # the whole-superproject status no longer fatals
        self.git(wd, "status", "--porcelain")
        # and the submodule path resolves to a (now empty) repo, not a fatal
        self.assertEqual(self.git(os.path.join(wd, "nested", "dep"),
                                  "rev-parse", "--is-inside-work-tree"), "true")

    def test_workspace_export_is_noop_without_new_commits(self):
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 1)
        self.assertFalse((spool / "pending.bundle").exists())
        self.assertIsNone(review.finalize(repo, spool, {"main": base}, "sess"))

    def test_finalize_warns_when_bundle_touches_masked_path(self):
        # A masked (emptied-in-jail) tracked file the agent committed would, on
        # apply, overwrite the user's real file. finalize must flag it loudly.
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.commit(wd, ".claude-jail.json", "", "blank the masked config")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            name = review.finalize(repo, spool, {"main": base}, "s",
                                   masked=[".claude-jail.json"])
        self.assertIsNotNone(name)
        self.assertIn("masked", err.getvalue())
        self.assertIn(".claude-jail.json", err.getvalue())

    def test_finalize_no_masked_warning_when_path_untouched(self):
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.commit(wd, "ordinary", "x\n", "unrelated work")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            review.finalize(repo, spool, {"main": base}, "s",
                            masked=[".claude-jail.json"])
        self.assertNotIn("masked", err.getvalue())

    def test_finalize_distinguishes_killed_export_from_nothing_new(self):
        # No bundle: a missing pending.exported marker means export never ran
        # (session killed) -> warn that work may be uncaptured; the marker present
        # means it ran and found nothing new -> stay quiet.
        repo, base = self.init_repo()
        spool = Path(self.tmpdir())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(review.finalize(repo, spool, {"main": base}, "s"))
        self.assertIn("did not run", err.getvalue())
        (spool / "pending.exported").write_text("")     # export ran, nothing new
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            self.assertIsNone(review.finalize(repo, spool, {"main": base}, "s"))
        self.assertNotIn("did not run", err2.getvalue())

    def test_finalize_refuses_symlinked_pending_bundle(self):
        # The per-run spool is jail-writable, so a hostile session could swap
        # pending.bundle for a symlink; finalize must not follow it.
        repo, base = self.init_repo()
        spool = Path(self.tmpdir())
        target = Path(self.tmpdir()) / "evil"
        target.write_bytes(b"not a bundle")
        (spool / "pending.bundle").symlink_to(target)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(review.finalize(repo, spool, {}, "s"))
        self.assertIn("symlink", err.getvalue())


class SessionNameTests(ReviewBase):
    """session_name maps a container workdir to its most recent Claude session."""

    def test_reads_most_recent_jsonl(self):
        store = self.tmpdir()
        # the entrypoint's project slug: every non-alphanumeric run in the
        # container workdir collapsed to '-' (/workspace/proj -> -workspace-proj)
        project = Path(store) / "projects" / "-workspace-proj"
        project.mkdir(parents=True)
        (project / "old.jsonl").write_text("{}\n")
        (project / "new.jsonl").write_text("{}\n")
        os.utime(project / "old.jsonl", (1000, 1000))
        os.utime(project / "new.jsonl", (2000, 2000))
        self.assertEqual(review.session_name(store, "/workspace/proj"), "new")

    def test_none_when_project_dir_absent(self):
        store = self.tmpdir()
        self.assertIsNone(review.session_name(store, "/workspace/nope"))


class LauncherRoutingTests(ReviewBase):
    def test_commands_parse_as_known(self):
        L = load_launcher()
        for name in review.COMMANDS:
            self.assertIn(name, L.KNOWN_COMMANDS)
        args = L.parse_args(["apply", "--allow-rewrite"])
        self.assertEqual(args.command, "apply")
        self.assertEqual(args.command_args, ["--allow-rewrite"])
        args = L.parse_args(["review"])
        self.assertEqual(args.command, "review")
        self.assertEqual(args.command_args, [])
        # the --review launch flag is opt-in, parsed ahead of the command
        self.assertFalse(L.parse_args(["run"]).review)
        self.assertTrue(L.parse_args(["--review", "run"]).review)


class _Tty(io.StringIO):
    """A StringIO that reports as a terminal, to drive the colour/pager path."""

    def isatty(self):
        return True


class DispatchTests(ReviewBase):
    """review.dispatch end to end: a config-anchored repo, spool, and printed
    output for each host command — the surface the launcher routes `review`,
    `accept`, `reject` and `apply` to."""

    def make_repo(self):
        """A git repo that *is* its own config dir, plus a separate store base.

        dispatch anchors the repo at the config's directory, so the config lives
        inside the repo; the store base (where the spool lives) is a sibling, as
        parse_roots forbids a root that is or contains the base.
        """
        repo, base = self.init_repo()
        cfg = self.write(os.path.join(repo, ".claude-jail.json"), '{"user": "t"}')
        store = self.tmpdir()
        return repo, base, cfg, store

    def import_feature(self, repo, base, name="b"):
        """Import an additive new-branch bundle (`feature`); returns its tip.

        A non-checked-out branch so apply never has to touch the working tree —
        which carries the untracked config file and would otherwise read dirty.
        """
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")
        tip = self.commit(ws, "n", "1\n", "feat")
        review.import_bundle(repo, self.make_bundle(ws, [base]), name)
        return tip

    def invoke(self, command, command_args, cfg, store):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review.dispatch(command, command_args, cfg, None, store)
        return rc, buf.getvalue()

    def test_review_overview_empty(self):
        _repo, _base, cfg, store = self.make_repo()
        rc, out = self.invoke("review", [], cfg, store)
        self.assertEqual(rc, 0)
        self.assertIn("No unreviewed bundles.", out)

    def test_review_named_shows_full_patch(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        _, detail = self.invoke("review", ["b"], cfg, store)
        self.assertIn("Bundle: b", detail)
        # the detail view carries the actual diff, not just the diffstat
        self.assertIn("diff --git a/n b/n", detail)
        self.assertIn("+1", detail)

    def test_review_no_arg_single_bundle_shows_detail(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        # a lone pending bundle: the no-arg path jumps straight to its detail
        rc, out = self.invoke("review", [], cfg, store)
        self.assertEqual(rc, 0)
        self.assertIn("Bundle: b", out)
        self.assertIn("diff --git a/n b/n", out)

    def test_review_no_arg_multiple_bundles_shows_summary(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base, name="one")
        self.import_feature(repo, base, name="two")
        # ambiguous: fall back to the summary list, no patch
        rc, out = self.invoke("review", [], cfg, store)
        self.assertEqual(rc, 0)
        self.assertIn("one", out)
        self.assertIn("two", out)
        self.assertNotIn("diff --git", out)

    def test_review_list_flag_forces_summary(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        # --list keeps the summary even for a single bundle
        rc, out = self.invoke("review", ["--list"], cfg, store)
        self.assertEqual(rc, 0)
        self.assertIn("new branch", out)
        self.assertNotIn("diff --git", out)

    def test_review_detail_never_emits_escapes(self):
        # review_detail runs git --color=never and adds no colour itself, so its
        # output carries no escape sequences: colour is (re)generated only later,
        # in _page, over text _sanitize has already scrubbed. Keeping the producer
        # escape-free is what lets _page trust that the only SGR it emits are its
        # own (a control-byte-split CSI could otherwise defeat a pass-through).
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        self.assertNotIn("\x1b", review.review_detail(repo, "b", None))

    def test_review_detail_divergent_diffs_against_merge_base(self):
        # A divergent branch is merged, not replaced: its diff must be the
        # session's net change (merge-base..inc), not a two-dot u..inc that paints
        # the user's own concurrent commit as a deletion of their work.
        repo, base, cfg, store = self.make_repo()
        ws = self.clone(repo)
        self.commit(ws, "agent.txt", "agent\n", "agent work")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        self.commit(repo, "user.txt", "precious\n", "user work")  # main diverges
        detail = review.review_detail(
            repo, "b", {"branches": {"main": base}, "end_heads": ["main"]})
        self.assertIn("divergent", detail)
        self.assertIn("+agent", detail)             # the session's change shows
        self.assertNotIn("-precious", detail)       # user's work NOT shown deleted

    def test_page_writes_straight_through_without_pagination(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            review._page("hello\n", False)
        self.assertEqual(buf.getvalue(), "hello\n")

    def test_page_pins_less_and_ignores_pager_env(self):
        # The pager is a security boundary: $PAGER / $GIT_PAGER must not be able
        # to swap less out for something that won't sanitise untrusted escapes.
        os.environ["GIT_PAGER"] = "evil-pager"
        os.environ["PAGER"] = "evil-pager"
        self.addCleanup(os.environ.pop, "GIT_PAGER", None)
        self.addCleanup(os.environ.pop, "PAGER", None)
        calls = []
        with mock.patch.object(review.subprocess, "run",
                               lambda args, **kw: calls.append((args, kw))):
            review._page("hi\n", True)
        self.assertEqual(len(calls), 1)
        args, kw = calls[0]
        self.assertEqual(args[0], "less")            # pinned, not shelled out
        self.assertIn("-R", args)                    # renders our own SGR colour
        self.assertNotIn("evil-pager", args)
        self.assertEqual(kw["env"]["LESS"], "")      # inherited LESS=-r neutralised
        self.assertEqual(kw["env"]["LESSSECURE"], "1")

    def test_page_strips_escapes_when_less_is_unavailable(self):
        def boom(*a, **k):
            raise FileNotFoundError("no less")
        buf = io.StringIO()
        with mock.patch.object(review.subprocess, "run", boom):
            with contextlib.redirect_stdout(buf):
                review._page("a\x1b[2J\x1b[31mX\x1b[0m b\n", True)
        out = buf.getvalue()
        self.assertNotIn("\x1b", out)            # no raw escape survives to the tty
        self.assertIn("X", out)                  # but the visible text does

    def test_review_on_a_terminal_paginates_without_escapes(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        seen = {}
        def fake_page(text, paginate):
            seen.update(text=text, paginate=paginate)
        with mock.patch.object(review, "_page", fake_page):
            with contextlib.redirect_stdout(_Tty()):
                review.dispatch("review", ["b"], cfg, None, store)
        self.assertTrue(seen["paginate"])        # interactive -> page it
        self.assertNotIn("\x1b", seen["text"])   # no colour/escape passed through

    def test_review_detail_shows_patch_and_range_diff_for_rewrite(self):
        repo, base, cfg, store = self.make_repo()
        ws = self.clone(repo)
        # rewrite the one commit's content so the patch has a visible change
        self.write(Path(ws) / "file", "rewritten\n")
        self.git(ws, "commit", "-a", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        _, detail = self.invoke("review", ["b"], cfg, store)
        self.assertIn("REWRITE", detail)
        self.assertIn("+rewritten", detail)                 # the patch
        self.assertIn("range-diff (what the rewrite changed)", detail)

    def test_accept_then_apply_lands_branch(self):
        repo, base, cfg, store = self.make_repo()
        tip = self.import_feature(repo, base)
        rc, out = self.invoke("accept", ["b"], cfg, store)
        self.assertEqual(rc, 0)
        self.assertIn("Accepted b.", out)
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])
        _, out = self.invoke("apply", [], cfg, store)
        self.assertIn("Applying b:", out)
        self.assertEqual(review.head_sha(repo, "refs/heads/feature"), tip)
        self.assertEqual(review.bundle_list(repo, "accepted"), [])

    def test_accept_warns_on_rewrite(self):
        repo, base, cfg, store = self.make_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        _, out = self.invoke("accept", ["b"], cfg, store)
        self.assertIn("Accepted b.", out)
        self.assertIn("rewrites history", out)

    def test_accept_and_reject_require_a_name(self):
        _repo, _base, cfg, store = self.make_repo()
        with self.assertDies("accept needs a bundle name"):
            review.dispatch("accept", [], cfg, None, store)
        with self.assertDies("reject needs a bundle name"):
            review.dispatch("reject", [], cfg, None, store)

    def test_reject_clears_refs_and_spool_artifact(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        spool = review._spool(store, "t")
        spool.mkdir(parents=True, exist_ok=True)
        bundle = spool / "b.bundle"
        manifest = spool / "b.manifest.json"
        bundle.write_bytes(b"x")
        manifest.write_text("{}")
        _, out = self.invoke("reject", ["b"], cfg, store)
        self.assertIn("Rejected b.", out)
        self.assertEqual(review.bundle_list(repo, "unreviewed"), [])
        self.assertFalse(bundle.exists())     # artifact swept with the refs
        self.assertFalse(manifest.exists())

    def test_apply_with_no_accepted_bundles(self):
        _repo, _base, cfg, store = self.make_repo()
        rc, out = self.invoke("apply", [], cfg, store)
        self.assertEqual(rc, 0)
        self.assertIn("No accepted bundles to apply.", out)

    def test_apply_allow_rewrite_flag_flows_through(self):
        repo, base, cfg, store = self.make_repo()
        # a rewrite on a non-checked-out branch: gated, then forced via the flag
        self.git(repo, "checkout", "-q", "-b", "feature")
        c1 = self.commit(repo, "f", "1\n", "feat")
        self.git(repo, "checkout", "-q", "main")
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "feature")
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        c1r = self.rev(ws, "feature")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        _, out = self.invoke("apply", ["b"], cfg, store)           # no flag
        self.assertIn("pass --allow-rewrite", out)
        self.assertEqual(self.rev(repo, "feature"), c1)            # not forced
        _, out = self.invoke("apply", ["b", "--allow-rewrite"], cfg, store)
        self.assertIn("force-updated", out)
        self.assertEqual(self.rev(repo, "feature"), c1r)
        self.assertEqual(review.bundle_list(repo, "accepted"), [])

    def test_accept_prints_strong_untrusted_warning(self):
        # accept warns strongly that the bundle is untrusted agent output before
        # taking it on (a non-tty caller still gets the warning and proceeds).
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with contextlib.redirect_stdout(io.StringIO()):
                review.dispatch("accept", ["b"], cfg, None, store)
        self.assertIn("UNTRUSTED", err.getvalue())
        self.assertIn("review", err.getvalue())

    def test_apply_warns_on_old_git(self):
        # apply checks out untrusted trees; on a git without the CVE-2021-21300
        # checkout hardening it warns before applying.
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        review.accept(repo, "b")
        err = io.StringIO()
        with mock.patch.object(review, "_git_version", lambda _repo: (2, 20, 0)):
            with contextlib.redirect_stderr(err):
                with contextlib.redirect_stdout(io.StringIO()):
                    review.dispatch("apply", [], cfg, None, store)
        self.assertIn("CVE-2021-21300", err.getvalue())

    def test_apply_no_warning_on_current_git(self):
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        review.accept(repo, "b")
        err = io.StringIO()
        with mock.patch.object(review, "_git_version", lambda _repo: (2, 43, 0)):
            with contextlib.redirect_stderr(err):
                with contextlib.redirect_stdout(io.StringIO()):
                    review.dispatch("apply", [], cfg, None, store)
        self.assertNotIn("CVE", err.getvalue())

    def test_apply_warns_on_unparseable_git_version(self):
        # Fail closed: an unparseable `git --version` (_git_version -> None) must
        # still warn about CVE-2021-21300, since it may be an old/exotic git.
        repo, base, cfg, store = self.make_repo()
        self.import_feature(repo, base)
        review.accept(repo, "b")
        err = io.StringIO()
        with mock.patch.object(review, "_git_version", lambda _repo: None):
            with contextlib.redirect_stderr(err):
                with contextlib.redirect_stdout(io.StringIO()):
                    review.dispatch("apply", [], cfg, None, store)
        self.assertIn("CVE-2021-21300", err.getvalue())
        self.assertIn("unknown version", err.getvalue())


class QuarantineIntegrityTests(ReviewBase):
    """Regression cover for the review-isolation fixes."""

    def test_import_does_not_leak_tags_into_live_refs(self):
        # A session tag must land ONLY under the quarantine prefix, never in the
        # live refs/tags/ (git's tag auto-following, defused with --no-tags).
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.commit(ws, "f", "1\n", "work")
        self.git(ws, "tag", "v9")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        self.assertEqual(self.git(repo, "for-each-ref", "refs/tags/"), "")
        self.assertIsNotNone(review.head_sha(
            repo, f"{review.REF_PREFIX}/unreviewed/b/tags/v9"))

    def test_failed_import_leaves_no_partial_quarantine(self):
        # A bundle whose prerequisite the target lacks must not leave dangling
        # quarantine refs behind (atomic rollback in import_bundle).
        donor, _dbase = self.init_repo()
        c1 = self.commit(donor, "f", "1\n", "c1")
        ws = self.clone(donor)
        self.commit(ws, "f", "2\n", "c2")
        bundle = self.make_bundle(ws, [c1])   # prerequisite is c1
        fresh, _ = self.init_repo()           # shares the base but has no c1
        self.assertFalse(review.import_bundle(fresh, bundle, "b", fatal=False))
        self.assertEqual(review.bundle_list(fresh, "unreviewed"), [])

    def test_spool_is_sibling_not_inside_the_claude_bind(self):
        # The named store must sit beside .claude-jail-<user>, not inside it, so a
        # jailed agent (which has .claude-jail-<user> mounted rw) can't reach it.
        base = self.tmpdir()
        spool = review._spool(base, "t")
        cfg = jail_config.user_config_dir(Path(base), "t")
        self.assertEqual(spool, cfg.parent / f"{cfg.name}-review")
        self.assertNotIn(cfg, spool.parents)

    def test_accept_is_idempotent_when_reaccepting(self):
        # Re-importing the same bundle while a prior accepted ref lingers (apply
        # didn't land it) must not collide and abort the accept transaction.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "checkout", "-q", "-b", "feature")
        self.commit(ws, "n", "1\n", "feat")
        bundle = self.make_bundle(ws, [base])
        review.import_bundle(repo, bundle, "b")
        review.accept(repo, "b")                      # accepted/b/ now exists
        review.import_bundle(repo, bundle, "b")        # re-import into unreviewed/b/
        review.accept(repo, "b")                       # must not raise
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])

    def test_apply_refuses_with_merge_in_progress(self):
        repo, base = self.init_repo()
        self.quarantine(repo, "b", "heads", "main", base)
        review.accept(repo, "b")
        # Leave the tree mid-merge, the state a conflicted divergent apply leaves.
        Path(repo, ".git", "MERGE_HEAD").write_text(base + "\n")
        with self.assertDies("unfinished merge"):
            review.apply_bundle(repo, "b")


class DetachedHeadTests(ReviewBase):
    """A session launched on a detached HEAD must still reach review (no loss)."""

    def test_detached_head_work_is_captured(self):
        repo, base = self.init_repo()
        self.git(repo, "checkout", "-q", "--detach")   # original on a detached HEAD
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        # init parks HEAD on a branch instead of reproducing the detached HEAD
        self.assertEqual(self.git(wd, "symbolic-ref", "HEAD"),
                         f"refs/heads/{jail_workspace._DETACHED_BRANCH}")
        tip = self.commit(wd, "n", "1\n", "work on detached HEAD")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        name = review.finalize(repo, spool, {}, "sess")
        # the work is reachable from a quarantine ref, not silently dropped
        quar = self.git(repo, "for-each-ref", "refs/jail-review/",
                        "--format=%(objectname)")
        self.assertIn(tip, quar)
        self.assertIsNotNone(name)

    def test_head_detached_during_session_is_captured(self):
        # The original is on a branch, so init does NOT park; the agent then
        # detaches HEAD *mid-session* and commits. export must park that onto a
        # branch so the commit rides a refs/heads/* ref import fetches — a bare
        # detached HEAD is written into the bundle only under the HEAD ref, which
        # the host's heads/tags/replace/notes refspecs never pull, so it would be
        # bundled yet unreviewable while export still reported success.
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.git(wd, "checkout", "-q", "--detach")      # detached DURING the session
        tip = self.commit(wd, "n", "1\n", "work on a mid-session detached HEAD")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        # the parking branch is a jail artifact, filtered from the end-state list so
        # it can't surface as a spurious "added jail-detached-head" ref-only change
        self.assertNotIn(jail_workspace._DETACHED_BRANCH,
                         (spool / "pending.end").read_text().split())
        name = review.finalize(repo, spool, {"main": base}, "sess")
        self.assertIsNotNone(name)
        quar = self.git(repo, "for-each-ref", "refs/jail-review/",
                        "--format=%(objectname)")
        self.assertIn(tip, quar)                         # captured, not lost

    def test_export_survives_non_utf8_branch_name(self):
        # A branch whose name carries invalid-UTF-8 bytes (an untrusted session can
        # create one) must not crash export's spool writes: Path.write_text would
        # re-encode strict UTF-8 and raise UnicodeEncodeError, which the entrypoint's
        # `export || true` swallows — silently dropping the whole capture.
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        weird = b"weird-\xff".decode("utf-8", "surrogateescape")
        self.git(wd, "branch", weird)                    # points at base (no new work)
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 1)   # does not raise
        self.assertIn(b"weird-\xff", (spool / "pending.end").read_bytes())


class PageSanitizeTests(ReviewBase):
    def test_strips_backspace_and_c1_controls(self):
        # The non-pager path must drop raw control bytes (backspace, the 0x9b C1
        # CSI, carriage return) that could overwrite or hide text on a terminal.
        buf = io.BytesIO()

        class _B:
            def __init__(self): self.buffer = buf
            def flush(self): pass
            def write(self, s): buf.write(s.encode())
        with mock.patch.object(review.sys, "stdout", _B()):
            review._page("a\x08\x08b\x9b31mc\rd\te\n", False)
        out = buf.getvalue()
        # the dangerous control bytes are gone; tab and newline survive (the C1
        # CSI's "31m" payload is left inert as plain text, which is harmless)
        for bad in (b"\x08", b"\x9b", b"\r"):
            self.assertNotIn(bad, out)
        self.assertEqual(out, b"ab31mcd\te\n")

    def test_pager_path_strips_control_split_csi(self):
        # Regression for the control-byte-split CSI (e.g. ESC '[' DEL '2' 'J'): the
        # old pass-through colour path let it reassemble into a live clear-screen
        # escape once the wedged byte was stripped, past `less -R`. _page now
        # sanitises BEFORE it re-colours, so the split escape is inert text by the
        # time colour is added — the ONLY escapes the pager sees are our own SGR.
        captured = {}
        with mock.patch.object(review.subprocess, "run",
                               lambda args, **kw: captured.update(kw)):
            review._page("a\x1b[\x7f2J\x1b[\x081Hb\n", True)   # two split escapes
        inp = captured["input"]
        leftover = re.sub(_OUR_SGR_B, b"", inp)  # drop our own SGR colour, nothing else
        self.assertNotIn(b"\x1b", leftover)      # no attacker escape reaches the pager
        self.assertIn(b"a", inp)                 # visible text still does
        self.assertIn(b"2J", inp)                # the split CSI is now inert plain text


class ColorizeTests(ReviewBase):
    """Host-side re-colouring: colour is regenerated from the diff's structure,
    over already-sanitised text, so no escape is ever carried through from git."""

    # SGR the colouriser emits, keyed by git's diff palette (see review._C_*).
    GREEN, RED, CYAN, BOLD, YELLOW = "\x1b[32m", "\x1b[31m", "\x1b[36m", "\x1b[1m", "\x1b[33m"

    def test_colours_follow_git_diff_defaults(self):
        text = ("abc1234 subject line\n"
                "diff --git a/f b/f\n"
                "index 111..222 100644\n"
                "--- a/f\n"
                "+++ b/f\n"
                "@@ -1,2 +1,2 @@ def region():\n"
                " context stays plain\n"
                "-removed\n"
                "+added\n")
        out = review._colorize(text)
        self.assertIn(self.YELLOW + "abc1234" + "\x1b[0m subject", out)  # sha only
        self.assertIn(self.BOLD + "diff --git a/f b/f", out)            # meta header
        self.assertIn(self.BOLD + "--- a/f", out)                       # old-file header
        self.assertIn(self.BOLD + "+++ b/f", out)                       # new-file header
        self.assertIn(self.CYAN + "@@ -1,2 +1,2 @@\x1b[0m def region():", out)  # frag + heading plain
        self.assertIn(self.RED + "-removed", out)                       # removal
        self.assertIn(self.GREEN + "+added", out)                       # addition
        self.assertIn("\n context stays plain\n", out)                  # context uncoloured

    def test_colorize_is_purely_additive(self):
        # Stripping the SGR codes back out must reproduce the input byte for byte:
        # colour changes presentation only, never the reviewed text.
        text = ("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n context\n"
                "\\ No newline at end of file\n")
        out = review._colorize(text)
        self.assertNotEqual(out, text)                       # colour was added
        self.assertEqual(re.sub(_OUR_SGR, "", out), text)    # ...and only our colour

    def test_hunk_body_markers_win_over_file_headers(self):
        # A removed line whose content is literally "--- x" (rendered "----- x"?) or
        # an added "+++ y" must colour as removal/addition, NOT as a file header:
        # in-hunk state is checked before the meta patterns.
        text = "@@ -1 +1 @@\n---three dashes\n+++three pluses\n"
        out = review._colorize(text)
        self.assertIn(self.RED + "---three dashes", out)     # removal, not meta
        self.assertIn(self.GREEN + "+++three pluses", out)   # addition, not meta

    def test_context_and_unknown_lines_stay_uncoloured(self):
        text = "Bundle: b\nplain summary line\n"
        self.assertEqual(review._colorize(text), text)       # nothing to colour

    def test_hazard_lines_are_yellow(self):
        # The indented `[!]` hazard/rewrite notes review_overview/review_detail emit
        # start with `[!]` after their leading whitespace, and are tinted yellow.
        text = "  [!] rewrites history\n    [!] force-push detected\n"
        out = review._colorize(text)
        self.assertIn(self.YELLOW + "  [!] rewrites history", out)
        self.assertIn(self.YELLOW + "    [!] force-push detected", out)

    def test_bare_or_padded_hex_is_not_mistaken_for_a_sha(self):
        # _COMMIT_RE only tints a hex run FOLLOWED by ` <subject>` (a real
        # log --oneline row). A bare hex token, or a hex-looking bundle name padded
        # with two spaces before a `[!]` note, has no such subject and stays plain.
        self.assertIn(self.YELLOW + "abc1234" + "\x1b[0m subj",
                      review._colorize("abc1234 subj\n"))          # real log row: tinted
        self.assertNotIn("\x1b", review._colorize("deadbeef\n"))   # bare name: plain
        self.assertNotIn("\x1b", review._colorize("deadbeef  [!] rewrites\n"))

    def test_rangediff_commit_rows_tint_shas_body_stays_plain(self):
        # A `git range-diff` commit-pair row has both abbreviated shas tinted (like
        # a log row); the nested diff-of-diffs body under it is left uncoloured, and
        # a `-:  -------` placeholder (a one-sided commit) is not tinted as a sha.
        text = ("1:  0ca9519 ! 1:  559b0a5 change on main\n"
                "    @@ f.txt\n"
                "     -b\n"
                "    -+B\n"
                "    ++B2\n"
                "-:  ------- > 2:  abcdef0 only on the right\n")
        out = review._colorize(text)
        self.assertIn(self.YELLOW + "0ca9519" + "\x1b[0m ! ", out)     # left sha
        self.assertIn(self.YELLOW + "559b0a5" + "\x1b[0m change", out) # right sha
        self.assertIn(self.YELLOW + "abcdef0" + "\x1b[0m only", out)   # one-sided sha
        self.assertNotIn(self.YELLOW + "----", out)                   # placeholder plain
        self.assertIn("\n    @@ f.txt\n", out)                        # inner hunk plain
        self.assertIn("\n    -+B\n", out)                             # diff-of-diff plain
        self.assertEqual(re.sub(_OUR_SGR, "", out), text)             # purely additive

    def test_combined_diff_uses_the_hunk_header_column_count(self):
        # A combined (merge) diff has one +/- column per parent; _colorize takes the
        # column count from the `@@@` header, so a line added/removed in only the
        # second parent (a space in column 1) still colours — not just column-1 marks.
        text = ("@@@ -1,2 -1,2 +1,2 @@@\n"
                "  context both\n"
                " +added in one parent\n"
                " -removed in one parent\n"
                "++added in both\n")
        out = review._colorize(text)
        self.assertIn(self.GREEN + " +added in one parent", out)
        self.assertIn(self.RED + " -removed in one parent", out)
        self.assertIn(self.GREEN + "++added in both", out)
        self.assertIn("\n  context both\n", out)             # both-space column = plain
        self.assertEqual(re.sub(_OUR_SGR, "", out), text)    # purely additive

    def test_prose_at_at_line_does_not_open_a_hunk(self):
        # A non-diff line that merely starts with `@@` (no closing `@@`) must not
        # enter hunk state: the following `+`/`-` prose stays plain, not diff-body.
        text = "@@ just prose, no closing marker\n+not an addition\n-not a removal\n"
        out = review._colorize(text)
        self.assertNotIn(self.GREEN + "+not an addition", out)
        self.assertNotIn(self.RED + "-not a removal", out)
        self.assertEqual(re.sub(_OUR_SGR, "", out), text)    # additive; nothing miscoloured

    def test_only_our_sgr_survives_a_hostile_diff(self):
        # The core security property: given a maximally hostile diff, sanitise then
        # colourise, and the ONLY escapes in the result are our own SGR codes — no
        # cursor-move, clear, OSC, or C1 sequence derived from the untrusted bytes.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        payload = ("start\x1b[31mRED\x1b[0m\x1b[2J\x1b[H"      # SGR, clear, home
                   "\x1b]0;title\x07"                          # OSC set-title
                   "mid\x08\x08GOT\x7fend\n")                  # BS, DEL
        self.write(Path(ws) / "evil.txt", payload)
        self.git(ws, "add", "-A")
        self.git(ws, "commit", "--no-gpg-sign", "-m",
                 "subj \x1b[2J\x1b]0;x\x07 done")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        coloured = review._colorize(review._sanitize(review.review_detail(repo, "b", None)))
        leftover = re.sub(_OUR_SGR, "", coloured)            # remove ONLY our fixed SGR
        self.assertNotIn("\x1b", leftover)                    # no foreign escape left
        self.assertNotIn("\x9b", leftover)                    # nor an 8-bit C1 CSI
        self.assertNotIn("\udc9b", leftover)                  # nor its surrogate form
        self.assertIn("\x1b[32m", coloured)                   # our green WAS added
        self.assertIn("start", coloured)                      # visible text preserved


class PageColorTests(ReviewBase):
    """_page adds colour for the terminal, but never when piped."""

    _DIFF = ("diff --git a/f b/f\n@@ -0,0 +1 @@\n+hello\n")

    def setUp(self):
        super().setUp()
        # Decouple the colour-positive assertions from the runner's environment: an
        # ambient NO_COLOR would otherwise suppress the colour they check for. Saved
        # and restored so a real NO_COLOR isn't clobbered for later tests.
        self._saved_no_color = os.environ.pop("NO_COLOR", None)
        self.addCleanup(self._restore_no_color)

    def _restore_no_color(self):
        if self._saved_no_color is not None:
            os.environ["NO_COLOR"] = self._saved_no_color

    def test_no_colour_when_piped(self):
        # paginate=False is the piped case (stdout not a tty): no colour, like
        # git's --color=auto. The bytes out must be exactly the sanitised text.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            review._page(self._DIFF, False)
        self.assertNotIn("\x1b", buf.getvalue())
        self.assertEqual(buf.getvalue(), review._sanitize(self._DIFF))  # exactly, unchanged

    def test_less_missing_falls_back_to_coloured_text(self):
        # When `less` is absent the paginate path can't page, so it writes the same
        # post-scrub text straight out — still carrying the colour added above,
        # since paginate implies a tty where our own SGR renders (see _page).
        buf = io.StringIO()
        with mock.patch.object(review.subprocess, "run", side_effect=OSError):
            with contextlib.redirect_stdout(buf):
                review._page(self._DIFF, True)
        self.assertIn("\x1b[32m+hello", buf.getvalue())   # coloured, straight to stdout

    def test_colour_added_on_paginate_path(self):
        captured = {}
        with mock.patch.object(review.subprocess, "run",
                               lambda args, **kw: captured.update(kw)):
            review._page(self._DIFF, True)
        self.assertIn(b"\x1b[32m+hello", captured["input"])   # addition is green

    def test_no_color_env_disables_colour(self):
        # The NO_COLOR convention (no-color.org): a non-empty value suppresses it
        # even on the paginate path.
        os.environ["NO_COLOR"] = "1"
        self.addCleanup(os.environ.pop, "NO_COLOR", None)
        captured = {}
        with mock.patch.object(review.subprocess, "run",
                               lambda args, **kw: captured.update(kw)):
            review._page(self._DIFF, True)
        self.assertNotIn(b"\x1b", captured["input"])          # colour suppressed

    def test_hostile_bundle_through_full_page_path_cannot_spoof(self):
        # The end-to-end task: a REAL bundle whose file bytes AND commit message
        # carry every terminal-spoofing primitive (SGR, screen-clear, cursor move,
        # OSC title-set, a raw 8-bit C1 CSI, DEL, backspace), driven through the
        # actual interactive _page path, must reach the pager carrying NO escape
        # but our own SGR colour — the guarantee that review output can't spoof a
        # terminal, made a permanent regression rather than a one-off check.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        # raw bytes (write_bytes, not write): a lone 0x9b exercises the
        # surrogateescape -> U+DC9B strip path a str "\x9b" (2-byte UTF-8) wouldn't.
        payload = (b"start\x1b[31mRED\x1b[0m\x1b[2J\x1b[H"   # SGR, clear, home
                   b"\x1b]0;title\x07"                        # OSC set-title
                   b"\x9b2J"                                   # raw 8-bit C1 CSI
                   b"mid\x08\x08GOT\x7fend\n")                 # backspace, DEL
        (Path(ws) / "evil.txt").write_bytes(payload)
        self.git(ws, "add", "-A")
        self.git(ws, "commit", "--no-gpg-sign", "-m", "subj \x1b[2J\x1b]0;x\x07 done")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        detail = review.review_detail(repo, "b", None)        # carries raw escapes

        captured = {}
        with mock.patch.object(review.subprocess, "run",
                               lambda args, **kw: captured.update(kw)):
            review._page(detail, True)                        # the real tty path
        out = captured["input"]                                # bytes handed to less -R
        leftover = re.sub(_OUR_SGR_B, b"", out)               # strip ONLY our fixed SGR
        self.assertNotIn(b"\x1b", leftover)                    # no foreign escape survives
        self.assertNotIn(b"\x9b", leftover)                    # nor an 8-bit C1 CSI
        self.assertIn(b"\x1b[32m", out)                        # our green colour WAS added
        self.assertIn(b"start", out)                           # visible text preserved


class SecondRoundRegressionTests(ReviewBase):
    """Cover for bugs found reviewing the first round of review-isolation fixes."""

    def test_export_unresolvable_base_is_loud_not_silent(self):
        # A base sha that doesn't resolve makes `rev-list --not <sha>` exit non-zero
        # with empty stdout; that must NOT be read as "nothing new" (which would
        # drop the session's commits) — it falls through to a loud failure (2).
        repo, _base = self.init_repo()
        self.commit(repo, "n", "1\n", "work")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text("0" * 40 + "\n")
        self.assertEqual(jail_workspace.export(repo, str(spool)), 2)

    def test_tag_sharing_branch_name_keeps_plain_base_key(self):
        # With both refs/heads/foo and refs/tags/foo, %(refname:short) would key the
        # branch as 'heads/foo'; branch_tips uses lstrip=2 so the key stays 'foo',
        # matching the name analyze() derives from the quarantine refspec.
        repo, base = self.init_repo()
        self.git(repo, "branch", "foo", base)
        self.git(repo, "tag", "foo", base)
        self.assertEqual(review.branch_tips(repo).get("foo"), base)

    def test_review_detail_new_branch_already_live_shows_nothing_new(self):
        # A 'new' branch pointing at an already-live commit must not be diffed
        # against the empty tree (which would paint the whole repo as additions).
        repo, base = self.init_repo()
        self.quarantine(repo, "b", "heads", "newb", base)
        detail = review.review_detail(repo, "b", None)
        self.assertIn("nothing new", detail)
        self.assertNotIn("diff --git", detail)

    def test_detached_head_off_branch_no_commits_captures_nothing(self):
        # Original detached at an off-branch commit, no session work: existing_commits
        # excludes HEAD, so export reports nothing rather than re-capturing it.
        repo, _base = self.init_repo()
        self.git(repo, "checkout", "-q", "--detach")
        self.commit(repo, "side", "x\n", "off-branch")    # detached HEAD advances
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(
            "".join(f"{c}\n" for c in sorted(review.existing_commits(repo))))
        self.assertEqual(jail_workspace.export(wd, str(spool)), 1)
        self.assertFalse((spool / "pending.bundle").exists())

    def test_detached_head_branch_collision_uses_free_name(self):
        # The original is detached AND already has a real `jail-detached-head`
        # branch; init must not clobber it — it parks HEAD on a free -2 name.
        repo, base = self.init_repo()
        self.git(repo, "branch", jail_workspace._DETACHED_BRANCH, base)
        self.git(repo, "checkout", "-q", "--detach")
        self.commit(repo, "side", "x\n", "off-branch")
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.assertEqual(self.git(wd, "symbolic-ref", "HEAD"),
                         f"refs/heads/{jail_workspace._DETACHED_BRANCH}-2")
        self.assertEqual(self.rev(wd, jail_workspace._DETACHED_BRANCH), base)

    def test_print_strips_c1_and_survives_non_utf8_refname(self):
        # apply/accept output may embed a non-UTF-8 branch name; _print must neither
        # crash on a strict encode nor relay a raw C1 control byte to the terminal.
        buf = io.BytesIO()

        class _B:
            buffer = buf
            def flush(self): pass
        name = "feat-" + b"\x9b\xe9".decode("utf-8", "surrogateescape")
        with mock.patch.object(review.sys, "stdout", _B()):
            review._print(f"  {name}: created")
        out = buf.getvalue()
        self.assertNotIn(b"\x9b", out)         # C1 CSI stripped
        self.assertIn(b"\xe9", out)            # printable Latin-1 byte round-trips
        self.assertIn(b"feat-", out)

    def test_existing_commits_skips_non_commit_refs(self):
        # A tag pointing at a tree must not contribute its non-commit sha to the
        # exclusion set (it would make rev-list/bundle --not error and fail capture).
        repo, base = self.init_repo()
        tree = self.git(repo, "rev-parse", "HEAD^{tree}")
        self.git(repo, "tag", "treetag", tree)            # lightweight tag -> tree
        commits = review.existing_commits(repo)
        self.assertIn(base, commits)
        self.assertNotIn(tree, commits)

    def test_apply_new_branch_on_unborn_checkout_populates_worktree(self):
        # Fresh repo (unborn HEAD -> main); a session makes the first commit. apply
        # must populate the working tree, not just move the ref (which would leave
        # every committed file as a staged deletion, lost on the next commit).
        real = self.tmpdir()
        self.git(real, "init", "-q", "-b", "main")
        ws = self.tmpdir()
        self.git(ws, "init", "-q", "-b", "main")
        tip = self.commit(ws, "f", "hello\n", "first")
        review.import_bundle(real, self.make_bundle(ws), "x")
        review.accept(real, "x")
        review.apply_bundle(real, "x")
        self.assertEqual(self.rev(real, "main"), tip)
        self.assertEqual(self.git(real, "status", "--porcelain"), "")   # not "D  f"
        self.assertTrue(os.path.exists(os.path.join(real, "f")))

    def test_finalize_missing_pending_end_skips_deletion_hazard(self):
        # export writes pending.bundle then pending.end; a kill between them leaves
        # no pending.end. finalize must record end_heads=None (skip the deletion
        # check), not [] (which flags every base branch as deleted).
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.commit(wd, "n", "1\n", "work")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")
        self.assertEqual(jail_workspace.export(wd, str(spool)), 0)
        (spool / "pending.end").unlink()                  # truncated capture
        name = review.finalize(repo, spool, {"main": base, "other": base}, "sess")
        manifest = review._manifest_loader(spool.parent)(name)
        self.assertIsNone(manifest["end_heads"])
        report = review.analyze(repo, "unreviewed", name, manifest)
        self.assertFalse(any("deleted" in h for h in report["hazards"]))

    def test_apply_aborts_on_unfinished_cherry_pick(self):
        # apply must refuse when a cherry-pick/revert/rebase is in progress, not
        # just a merge, so `reset --hard` can't silently discard it.
        repo, base = self.init_repo()
        self.quarantine(repo, "b", "heads", "main", base)
        review.accept(repo, "b")
        Path(repo, ".git", "CHERRY_PICK_HEAD").write_text(base + "\n")
        with self.assertDies("unfinished cherry-pick"):
            review.apply_bundle(repo, "b")

    def test_existing_commits_excludes_remote_and_quarantine_refs(self):
        # existing_commits is the delta-exclusion: it must cover only branches and
        # tags (+ HEAD), not refs/remotes or a prior run's refs/jail-review/* — a
        # stale quarantine tip there would mask a later session's identical work as
        # "nothing new".
        repo, base = self.init_repo()
        extra = self.commit(repo, "f", "1\n", "extra")
        self.git(repo, "update-ref",
                 "refs/jail-review/unreviewed/old/heads/main", extra)
        self.git(repo, "update-ref", "refs/remotes/origin/main", extra)
        self.git(repo, "reset", "--hard", base)   # extra now only via the odd refs
        commits = review.existing_commits(repo)
        self.assertIn(base, commits)
        self.assertNotIn(extra, commits)


class ReviewFixWorkspaceTests(ReviewBase):
    """Cover for the second-round review fixes in jail_workspace/finalize."""

    def test_export_excludes_read_only_original_floor(self):
        # A commit the *user* lands on the real repo during the session (present in
        # the original at export time but absent from the pre-session pending.base)
        # must NOT be attributed to the agent's bundle. The read-only original is
        # the trusted exclusion floor.
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        agent = self.commit(wd, "n", "1\n", "agent work")
        host = self.commit(repo, "h", "u\n", "user work during session")
        spool = Path(self.tmpdir())
        (spool / "pending.base").write_text(base + "\n")   # stale: pre-host-commit
        self.assertEqual(jail_workspace.export(wd, str(spool), f"{repo}/.git"), 0)
        name = review.finalize(repo, spool, {"main": base}, "s")
        self.assertIsNotNone(name)
        # the bundle carries the agent commit but not the concurrent user commit
        carried = self.git(repo, "rev-list", "--all",
                           f"^refs/heads/main").splitlines()
        quar = self.git(repo, "for-each-ref", "refs/jail-review/",
                        "--format=%(objectname)")
        self.assertIn(agent, quar)
        self.assertNotIn(host, quar)

    def test_export_floor_survives_deleted_pending_base(self):
        # Even if the (jail-writable) pending.base is deleted, the read-only
        # original floor keeps export from bundling the whole pre-existing history.
        repo, base = self.init_repo()
        self.commit(repo, "more", "x\n", "pre-existing history")
        wd = self.materialize_workdir(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        spool = Path(self.tmpdir())      # no pending.base at all
        self.assertEqual(jail_workspace.export(wd, str(spool), f"{repo}/.git"), 1)
        self.assertFalse((spool / "pending.bundle").exists())

    def test_finalize_reports_export_failure_loudly(self):
        # export failed (bundle create errored) -> pending.failed marker, no bundle.
        # finalize must say so loudly, not fall through to the silent nothing-new
        # path (the return-2 vs return-1 conflation).
        repo, _base = self.init_repo()
        spool = Path(self.tmpdir())
        (spool / "pending.exported").write_text("")
        (spool / "pending.failed").write_text("disk full\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(review.finalize(repo, spool, {}, "s"))
        self.assertIn("FAILED", err.getvalue())
        self.assertIn("disk full", err.getvalue())

    def test_finalize_warns_ref_only_branch_change(self):
        # A branch created/deleted with no new commit produces no bundle; finalize
        # must surface it rather than let it vanish silently.
        repo, base = self.init_repo()
        spool = Path(self.tmpdir())
        (spool / "pending.exported").write_text("")
        (spool / "pending.end").write_text("main\nfeature\n")   # feature is new
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(review.finalize(repo, spool, {"main": base}, "s"))
        self.assertIn("without adding commits", err.getvalue())
        self.assertIn("feature", err.getvalue())

    def test_finalize_symlinked_exported_marker_treated_missing(self):
        # A symlinked pending.exported (the jail-writable spool) must be treated as
        # absent, so a hostile session can't fake "export completed".
        repo, base = self.init_repo()
        spool = Path(self.tmpdir())
        target = Path(self.tmpdir()) / "real"
        target.write_text("")
        (spool / "pending.exported").symlink_to(target)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(review.finalize(repo, spool, {"main": base}, "s"))
        self.assertIn("did not run", err.getvalue())

    def test_nested_submodules_keep_working(self):
        # A submodule that itself contains a submodule: both live worktrees must be
        # real repos after init (the recursion), not dangling pointers that abort
        # the superproject's status.
        allow = ("-c", "protocol.file.allow=always")
        inner = self.tmpdir()
        self.git(inner, "init", "-q", "-b", "main")
        inner_head = self.commit(inner, "i.txt", "i\n", "inner init")
        mid = self.tmpdir()
        self.git(mid, "init", "-q", "-b", "main")
        self.commit(mid, "m.txt", "m\n", "mid init")
        self.git(mid, *allow, "submodule", "add", "-q", inner, "inner")
        self.git(mid, "commit", "-q", "--no-gpg-sign", "-m", "mid adds inner")
        repo, _base = self.init_repo()
        self.git(repo, *allow, "submodule", "add", "-q", mid, "mid")
        self.git(repo, "commit", "-q", "--no-gpg-sign", "-m", "super adds mid")
        self.git(repo, *allow, "submodule", "update", "-q", "--init", "--recursive")
        wd = self.materialize_copy(repo)
        jail_workspace.init(wd, f"{repo}/.git")
        self.assertEqual(self.git(wd, "status", "--porcelain"), "")
        # the nested submodule worktree is a real repo at the right commit
        self.assertEqual(self.git(os.path.join(wd, "mid", "inner"),
                                  "rev-parse", "HEAD"), inner_head)

    def test_shallow_original_does_not_abort(self):
        # A shallow original: init copies the shallow boundary so history traversal
        # stops cleanly instead of chasing parents the shallow clone never fetched.
        up = self.tmpdir()
        self.git(up, "init", "-q", "-b", "main")
        for i in range(4):
            self.commit(up, "f", f"{i}\n", f"c{i}")
        orig = os.path.join(self.tmpdir(), "orig")
        r = subprocess.run(["git", "clone", "-q", "--depth", "1",
                            f"file://{up}", orig], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(orig, ".git", "shallow")))
        wd = os.path.join(self.tmpdir(), "wd")
        subprocess.run(["git", "clone", "-q", orig, wd], check=True,
                       capture_output=True)
        shutil.rmtree(os.path.join(wd, ".git"))
        os.mkdir(os.path.join(wd, ".git"))
        jail_workspace.init(wd, f"{orig}/.git")
        self.assertTrue(os.path.exists(os.path.join(wd, ".git", "shallow")))
        self.assertEqual(self.git(wd, "status", "--porcelain"), "")
        self.assertEqual(self.git(wd, "rev-list", "--count", "HEAD"), "1")


class ReviewFixApplyTests(ReviewBase):
    def test_apply_rewrite_refuses_when_base_unresolvable(self):
        # A rewrite whose manifest base no longer resolves (gc'd between accept and
        # apply): _drops_new_work can't compute the delta and must fail *closed* —
        # refuse the reset --hard rather than run blind on a swallowed error.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        bogus = "0" * 40
        log = review.apply_bundle(repo, "b", allow_rewrite=True,
                                  manifest={"branches": {"main": bogus}})
        self.assertTrue(any("discard" in ln for ln in log))
        self.assertEqual(self.rev(repo, "main"), base)                 # not reset
        self.assertEqual(review.bundle_list(repo, "accepted"), ["b"])  # kept


class ThirdRoundReviewFixTests(ReviewBase):
    """Cover for the fixes applied after the max-effort review round."""

    def test_init_survives_non_utf8_head_branch(self):
        # A repo checked out on a branch whose name carries non-UTF-8 bytes: init
        # reads the original's HEAD (`ref: refs/heads/<bytes>`). A strict read
        # raised UnicodeDecodeError (a ValueError the `except OSError` never caught)
        # and aborted the whole session; surrogateescape round-trips it so init
        # still succeeds.
        repo, base = self.init_repo()
        wd = self.materialize_workdir(repo)          # clone while still on main
        weird = b"br-\xff".decode("utf-8", "surrogateescape")
        self.git(repo, "update-ref", f"refs/heads/{weird}", base)
        self.git(repo, "symbolic-ref", "HEAD", f"refs/heads/{weird}")
        jail_workspace.init(wd, f"{repo}/.git")       # must not raise
        self.assertEqual(self.rev(wd, "HEAD"), base)  # HEAD resolved -> session usable
        self.assertEqual(self.rev(wd, f"refs/heads/{weird}"), base)  # branch fetched

    def test_analyze_ignores_parking_named_user_branch(self):
        # A real user branch sharing the reserved parking name is filtered out of
        # pending.end by export, so the host must drop it from the base side too —
        # otherwise it reads as a spurious 'deleted' hazard.
        repo, base = self.init_repo()
        self.quarantine(repo, "b", "heads", "main", base)
        manifest = {"branches": {"main": base,
                                 jail_workspace._DETACHED_BRANCH: base},
                    "end_heads": ["main"]}            # parking branch absent from end
        report = review.analyze(repo, "unreviewed", "b", manifest)
        self.assertFalse(any("deleted" in h for h in report["hazards"]))

    def test_parking_branch_flagged_in_summary(self):
        # The synthetic detached-HEAD parking branch does apply (it carries the
        # work), but the summary must flag it as a jail artifact so it doesn't read
        # as a mysterious user "new branch" at the review gate.
        b = {"branch": jail_workspace._DETACHED_BRANCH, "class": "new",
             "added": 1, "dropped": 0, "current": None, "incoming": "x"}
        self.assertIn("jail parking branch", review.summary_line(b))
        plain = {"branch": "feature", "class": "new", "added": 1, "dropped": 0,
                 "current": None, "incoming": "x"}
        self.assertNotIn("jail parking branch", review.summary_line(plain))

    def test_ref_only_warning_ignores_parking_named_branch(self):
        # Same reserved-name filter on the ref-only (no-bundle) warning path.
        repo, base = self.init_repo()
        spool = Path(self.tmpdir())
        (spool / "pending.exported").write_text("")
        (spool / "pending.end").write_text("main\n")   # parking branch absent
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            review.finalize(repo, spool,
                            {"main": base, jail_workspace._DETACHED_BRANCH: base},
                            "s")
        self.assertNotIn("without adding commits", err.getvalue())

    def test_prepare_neutralises_symlinked_pending_base(self):
        # A leaked, pid-reused spool could carry a pending.base swapped for a
        # symlink; prepare must unlink it before writing so write_text can't be
        # redirected through it to an arbitrary host file.
        repo, base = self.init_repo()
        spool = Path(self.tmpdir()) / "spool"
        spool.mkdir(parents=True)
        victim = Path(self.tmpdir()) / "victim"
        victim.write_text("PRECIOUS")
        (spool / "pending.base").symlink_to(victim)
        review.prepare({"repo": repo, "spool": str(spool),
                        "gitdir": str(spool / "gitdir")})
        self.assertEqual(victim.read_text(), "PRECIOUS")         # not written through
        self.assertFalse((spool / "pending.base").is_symlink())  # replaced by a file
        self.assertIn(base, (spool / "pending.base").read_text().split())

    def test_prepare_survives_dangling_symlink_gitdir(self):
        # A leaked, pid-reused spool could carry `gitdir` swapped for a dangling
        # symlink: rmtree won't recurse it and exists() reads it as absent, so
        # without the explicit unlink the mkdir would crash with FileExistsError.
        repo, base = self.init_repo()
        spool = Path(self.tmpdir()) / "spool"
        spool.mkdir(parents=True)
        gitdir = spool / "gitdir"
        gitdir.symlink_to(Path(self.tmpdir()) / "does-not-exist")  # dangling
        review.prepare({"repo": repo, "spool": str(spool), "gitdir": str(gitdir)})
        self.assertTrue(gitdir.is_dir())            # a real, fresh directory now
        self.assertFalse(gitdir.is_symlink())

    def test_apply_rewrite_without_manifest_warns_guard_disabled(self):
        # With no manifest base, _drops_new_work fails open (can't tell post-accept
        # work from the rewrite); apply --allow-rewrite must warn the guard is off.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", allow_rewrite=True)     # no manifest
        self.assertTrue(any("no review manifest" in ln for ln in log))

    def test_apply_rewrite_with_manifest_no_guard_warning(self):
        # With a trusted manifest base the guard runs, so the warning is absent.
        repo, base = self.init_repo()
        ws = self.clone(repo)
        self.git(ws, "commit", "--amend", "--no-gpg-sign", "-m", "reworded")
        review.import_bundle(repo, self.make_bundle(ws, [base]), "b")
        review.accept(repo, "b")
        log = review.apply_bundle(repo, "b", allow_rewrite=True,
                                  manifest={"branches": {"main": base}})
        self.assertFalse(any("no review manifest" in ln for ln in log))


class WorkspaceSanitizeTests(ReviewBase):
    """The in-jail export/init half must scrub terminal escapes from untrusted git
    output before it reaches stderr (the host terminal during `docker compose
    run`), mirroring review.py's host-side boundary."""

    def test_sanitize_strips_escapes_and_c1(self):
        # CSI clear-screen, an OSC title, and a C1 8-bit CSI surrogate all go.
        dirty = "a\x1b[2Jb\x1b]0;pwn\x07c\udc9bd"
        clean = jail_workspace._sanitize(dirty)
        self.assertEqual(clean, "abcd")

    def test_eprint_neutralises_untrusted_bytes(self):
        # A refname with an embedded escape must not reach stderr raw, and a
        # non-UTF-8 (surrogateescape) byte must not crash the write.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            jail_workspace._eprint("branch \x1b[31m\udcff evil")
        out = buf.getvalue()
        self.assertNotIn("\x1b", out)
        self.assertIn("branch", out)


if __name__ == "__main__":
    unittest.main()
