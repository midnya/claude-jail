"""End-to-end git-review round-trip through a REAL nested Docker daemon.

Skipped unless CLAUDE_JAIL_E2E=1 (and a docker daemon is reachable), so the
default `python3 -m unittest discover` stays docker-free and fast. Meant to run
inside the claude-jail-dind image (docker/claude-jail-dind/), where `claude-jail`
can launch nested containers; see docker/claude-jail-dind/README.md.

The flow, driven through the launcher exactly as a user would:

  1. build a throwaway git repo with `review` on,
  2. `claude-jail run --review`, with a *scripted* stand-in agent
     (CLAUDE_JAIL_AGENT_CMD) that makes one commit instead of interactive claude,
  3. assert the commit comes back as a quarantined bundle, and
  4. assert it survives review -> accept -> apply into the real repo.

Unlike the rest of the suite (which mocks docker), this exercises the real
entrypoint, the in-container jail-workspace init/export, the mount isolation, and
the host-side review state machine together.

Alongside that accept -> apply round-trip, the suite drives the other terminal
outcomes through the same real stack: reject (discard a captured bundle), an
empty session (a stand-in that commits nothing, so nothing is captured), and two
sessions whose bundles coexist in the store and are applied selectively.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from jail_test_helpers import REPO_ROOT  # noqa: I001 (puts src/ on sys.path)

import build_mounts
from test_review import ReviewBase

_LAUNCHER = str(REPO_ROOT / "claude-jail")
# A nested stack bring-up (squid + dns healthchecks, then the session) is slow;
# give each launcher call generous headroom rather than a flaky short timeout.
_TIMEOUT = 900


def _docker_ok() -> bool:
    """True when a docker daemon answers, so the suite skips cleanly without one."""
    try:
        return subprocess.run(["docker", "version"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


# One gate, `and`-short-circuited so `_docker_ok()` (which shells out to docker)
# only runs when CLAUDE_JAIL_E2E is armed — the default `unittest discover` stays
# docker-free, never probing a (possibly hanging) daemon at import time. The gate
# is `== "1"`, matching the launcher's own semantics (claude-jail's agent-cmd pin),
# so an explicit CLAUDE_JAIL_E2E=0 reads as off rather than arming the docker probe.
@unittest.skipUnless(
    os.environ.get("CLAUDE_JAIL_E2E") == "1" and _docker_ok(),
    "set CLAUDE_JAIL_E2E=1 with a reachable docker daemon to run the e2e round-trip")
class ReviewRoundTripE2E(ReviewBase):
    """A jailed session's single commit, all the way to the real branch.

    Reuses ReviewBase (real-git scaffolding: git()/commit() plus an isolated,
    fixed committer identity) and its JailTestCase base (tmpdir()/mkdir()/write(),
    each realpath'd and cleaned up with ignore_errors — the launcher canonicalises
    roots, and the inner container writes bind-mounted files as a remapped subuid
    this host user can't remove, an expected leak; see the dind README).
    """

    def setUp(self) -> None:
        super().setUp()   # ReviewBase: pin a fixed, isolated git identity
        # The store base and the project repo as siblings under one temp root, so
        # the config's claude_dir_base and the repo never overlap.
        tmp = self.tmpdir()
        self.base = self.mkdir(os.path.join(tmp, "base"))
        self.repo = self.mkdir(os.path.join(tmp, "project"))

        # A real repo with one commit, so the session forks from actual history.
        self.git(self.repo, "init", "-q", "-b", "main")
        self.commit(self.repo, "README", "seed\n", "seed")

        # review on; an isolated per-test store base so nothing touches $HOME.
        # An explicit "user" so the run never depends on an ambient $USER/$USERNAME
        # — those are unset inside the claude-jail-dind image (the README's own run
        # target: `USER claude` sets the runtime user but not the env var), where a
        # $USER fallback would die("no user set") before the session even starts.
        # uid/gid 0: under the rootless nested daemon the inner container's user is
        # remapped into the 100000+ subuid range, so files it writes to the bind
        # mount would land as a subuid this (host) user can't read or remove —
        # breaking apply's ff-merge, review's gitdir wipe, and cleanup. Mapping the
        # inner uid 0 back to `claude` lines ownership up (claude-jail warns on uid
        # 0 — expected). See docker/claude-jail-dind/README.md item 1.
        self.write(Path(self.repo, ".claude-jail.json"),
                   json.dumps({"review": True, "claude_dir_base": self.base,
                               "user": "e2e", "uid": 0, "gid": 0}) + "\n")
        self.cfg = str(Path(self.repo, ".claude-jail.json"))

        # The default scripted agent, run *inside* the jail in place of claude:
        # make one commit in the review workspace, then exit. _install_agent lets
        # each test install its own stand-in (a different file/message, or a no-op)
        # so the suite can drive distinct capture outcomes, not just the happy path.
        self.agent_cmd = self._install_agent(
            "commit", "agent-artifact.txt", "e2e: agent commit")

    # -- helpers -----------------------------------------------------------

    def _agent_container_path(self, tag: str) -> str:
        """The in-container path of the scripted agent script named for `tag`."""
        return build_mounts.container_path(self.repo) + f"/.e2e-agent-{tag}.sh"

    def _write_agent(self, tag: str, body: str) -> str:
        """Write an executable stand-in agent script named for `tag` and return its
        container path. The script lives in the repo — a mounted root — so the
        entrypoint can exec it by that path; owns the naming/chmod boilerplate the
        two installers below share."""
        agent = Path(self.repo, f".e2e-agent-{tag}.sh")
        self.write(agent, body)
        agent.chmod(0o755)
        return self._agent_container_path(tag)

    def _install_agent(self, tag: str, filename: str, message: str) -> str:
        """Write a scripted stand-in agent that makes one commit of `filename` on
        the checked-out branch, and return its container path.

        `tag` names the script so several agents can coexist in the one repo (the
        multi-session test installs two).
        """
        return self._write_agent(
            tag,
            "#!/bin/sh\n"
            "set -e\n"
            'cd "${JAIL_WORKDIR:-/workspace}"\n'
            f"printf '{tag} was here\\n' > {filename}\n"
            f"git add {filename}\n"
            f"git commit -q -m '{message}'\n")

    def _install_noop_agent(self, tag: str = "noop") -> str:
        """Write a stand-in that exits without committing (an empty session), and
        return its container path — for the 'nothing is captured' path."""
        return self._write_agent(tag, "#!/bin/sh\nexit 0\n")

    def _jail(self, *args: str,
              agent_cmd: "str | None" = None) -> "subprocess.CompletedProcess":
        """Invoke the `claude-jail` launcher; capture output, no tty (so `accept`
        proceeds past its warning and `review` doesn't paginate).

        `agent_cmd` runs a scripted stand-in in place of claude — the default
        committing agent (`self.agent_cmd`), a second one, or the no-op. When set,
        CLAUDE_JAIL_E2E is armed alongside it, which is what gates the launcher's
        forwarding of the stand-in into the container; host-only commands
        (review/accept/reject/apply) pass no agent_cmd and need neither var.
        """
        env = dict(os.environ)
        if agent_cmd is not None:
            env["CLAUDE_JAIL_E2E"] = "1"
            env["CLAUDE_JAIL_AGENT_CMD"] = agent_cmd
        return subprocess.run([sys.executable, _LAUNCHER, "-c", self.cfg, *args],
                              capture_output=True, text=True, env=env,
                              timeout=_TIMEOUT)

    def _assert_run_ok(self, run: "subprocess.CompletedProcess", label: str) -> None:
        """Assert a launcher invocation exited 0, dumping both streams when it didn't."""
        self.assertEqual(run.returncode, 0,
                         f"{label} failed:\nSTDOUT{run.stdout}\nSTDERR{run.stderr}")

    def _store_glob(self, pattern: str) -> "list[Path]":
        """Sorted matches of `pattern` under the host-only review store dir(s)
        (`.claude-jail-<user>-review`) — the single place the store layout is
        spelled, so the three helpers below can't drift on it."""
        return sorted(Path(self.base).glob(f".claude-jail-*-review{pattern}"))

    def _review_store(self) -> "list[Path]":
        """The host-only review store dir(s). prepare() creates the store before the
        container starts, so it survives even a session that captures nothing — its
        presence is the positive signal that review actually engaged, telling
        'captured nothing' apart from 'review never ran'."""
        return self._store_glob("")

    def _bundles(self) -> "list[Path]":
        """Every captured bundle artifact in the host-only review store."""
        return self._store_glob("/*.bundle")

    def _manifests(self) -> "list[Path]":
        """Every captured manifest artifact in the host-only review store."""
        return self._store_glob("/*.manifest.json")

    def _bundle_names(self) -> "list[str]":
        """Every captured bundle's name — its filename minus the `.bundle` suffix."""
        return [b.name[:-len(".bundle")] for b in self._bundles()]

    def _bundle_name(self) -> str:
        """The single captured bundle's name; fails unless there's exactly one."""
        names = self._bundle_names()
        self.assertEqual(len(names), 1,
                         f"expected exactly one captured bundle, got {names}")
        return names[0]

    def _bundle_subject(self, name: str) -> str:
        """The subject of the commit a quarantined bundle puts on `main`, to tell
        two captured bundles apart — their names are only content hashes."""
        tip = self.rev(self.repo, f"refs/jail-review/unreviewed/{name}/heads/main")
        return self.git(self.repo, "log", "-1", "--format=%s", tip)

    # -- the round trip ----------------------------------------------------

    def test_review_round_trip(self) -> None:
        # `--review` before the command: it is a launcher flag, so a trailing
        # `run --review` would be swallowed into the agent's args (argparse
        # REMAINDER) and NOT force isolation. Placed here it exercises the flag
        # path itself, independently of the config's own "review": true.
        run = self._jail("--review", "run", agent_cmd=self.agent_cmd)
        self._assert_run_ok(run, "--review run")

        name = self._bundle_name()

        review = self._jail("review", name)
        self._assert_run_ok(review, "review")
        self.assertIn("agent-artifact.txt", review.stdout,
                      "review diff should show the agent's new file")

        accept = self._jail("accept", name)
        self._assert_run_ok(accept, "accept")

        # The session wrote agent-artifact.txt into the live tree AND committed it
        # to the bundle; review's export leaves the working tree untouched, so the
        # file now sits UNTRACKED in the real repo. apply fast-forwards main with
        # `git merge --ff-only`, which git refuses when an untracked file would be
        # overwritten — so clear the leftover first. apply then re-creates it from
        # the commit, which is what the is_file() assertion below actually proves.
        artifact = Path(self.repo, "agent-artifact.txt")
        os.remove(artifact)
        self.assertFalse(artifact.exists())

        apply = self._jail("apply")
        self._assert_run_ok(apply, "apply")

        # The agent's commit is now on the real branch, and apply re-materialised
        # its file (removed above) into the tree by fast-forwarding.
        self.assertIn("e2e: agent commit", self.git(self.repo, "log", "--oneline"))
        self.assertTrue(artifact.is_file(),
                        "apply should have written the agent's file into the tree")

    def test_reject_discards_bundle(self) -> None:
        """A captured bundle can be thrown away: reject drops its quarantine refs
        and on-disk artifacts, and nothing reaches the real branch."""
        run = self._jail("--review", "run", agent_cmd=self.agent_cmd)
        self._assert_run_ok(run, "run")
        name = self._bundle_name()

        reject = self._jail("reject", name)
        self._assert_run_ok(reject, "reject")
        self.assertIn("Rejected", reject.stdout)

        # The whole quarantine namespace is gone (no unreviewed/accepted/replaced
        # refs linger), and so are the store's .bundle/.manifest.json artifacts.
        self.assertEqual("", self.git(self.repo, "for-each-ref", "refs/jail-review/"))
        self.assertEqual(self._bundles(), [])
        self.assertEqual(self._manifests(), [])

        # The agent's commit never touched the real branch (main is still the seed).
        self.assertEqual("seed", self.git(self.repo, "log", "-1", "--format=%s"))

        # And review now reports an empty queue rather than a phantom bundle.
        review = self._jail("review")
        self._assert_run_ok(review, "review")
        self.assertIn("No unreviewed bundles", review.stdout)

    def test_empty_session_captures_nothing(self) -> None:
        """A session that commits nothing yields no bundle: the in-jail export runs,
        finds no new history, and the launcher completes cleanly with an empty
        review queue and an untouched repo."""
        run = self._jail("--review", "run", agent_cmd=self._install_noop_agent())
        self._assert_run_ok(run, "empty run")

        # Positive signal that review actually engaged rather than silently not
        # running: prepare() creates the store dir before the container starts, so
        # it is present even though this session drops no bundle into it. Without
        # this, every emptiness assertion below is equally true of a run in which
        # review never ran at all.
        self.assertEqual(len(self._review_store()), 1,
                         "review should have engaged and created its store dir")
        self.assertEqual(self._bundles(), [])
        self.assertEqual("", self.git(self.repo, "for-each-ref", "refs/jail-review/"))

        review = self._jail("review")
        self._assert_run_ok(review, "review")
        self.assertIn("No unreviewed bundles", review.stdout)

        # The real repo is exactly as it started: one commit, the seed.
        self.assertEqual("seed", self.git(self.repo, "log", "-1", "--format=%s"))

    def test_two_sessions_selective_apply(self) -> None:
        """Two review sessions leave two bundles side by side in the store; each is
        resolvable by name, and reject/accept/apply act on one without disturbing
        the other."""
        drop_agent = self._install_agent("drop", "drop.txt", "e2e: drop commit")
        keep_agent = self._install_agent("keep", "keep.txt", "e2e: keep commit")

        # Sequential (not concurrent) same-config runs: each is fully torn down
        # before the next starts, and per-run spools are pid-keyed, so the two
        # bundles coexist in the shared store under distinct content-hash names.
        for cmd in (drop_agent, keep_agent):
            self._assert_run_ok(self._jail("--review", "run", agent_cmd=cmd), "run")

        names = self._bundle_names()
        self.assertEqual(len(names), 2,
                         f"expected two captured bundles, got {names}")

        # Names are content hashes, so map each back to the commit it carries. Assert
        # the two subjects up front so a capture bug that collapses them onto one
        # fails here, not with an opaque KeyError on the lookups below.
        by_subject = {self._bundle_subject(n): n for n in names}
        self.assertEqual(set(by_subject), {"e2e: keep commit", "e2e: drop commit"},
                         f"expected two distinctly-subjected bundles, got {by_subject}")
        keep = by_subject["e2e: keep commit"]
        drop = by_subject["e2e: drop commit"]

        # The overview lists both bundles by name.
        overview = self._jail("review", "--list")
        self._assert_run_ok(overview, "review --list")
        self.assertIn(keep, overview.stdout)
        self.assertIn(drop, overview.stdout)

        # Reject one, accept the other — each resolves to the right bundle.
        self._assert_run_ok(self._jail("reject", drop), "reject drop")
        self._assert_run_ok(self._jail("accept", keep), "accept keep")

        # keep committed keep.txt into its bundle and left it untracked in the live
        # tree (review's export doesn't touch the working tree); clear it so apply's
        # ff-merge isn't blocked by the overwrite (see the round-trip test). Only
        # keep.txt matters — drop was rejected, so apply never re-materialises it.
        os.remove(Path(self.repo, "keep.txt"))

        apply = self._jail("apply")   # no name: applies every accepted bundle
        self._assert_run_ok(apply, "apply")

        # Only the kept bundle landed on main; the rejected one is nowhere — its
        # commit never reaches the log even though apply ran.
        log = self.git(self.repo, "log", "--oneline")
        self.assertIn("e2e: keep commit", log)
        self.assertNotIn("e2e: drop commit", log)
        self.assertTrue(Path(self.repo, "keep.txt").is_file())

        # reject removed the dropped bundle's artifact; apply keeps the applied
        # bundle's .bundle in the store (its refs are cleared, the artifact stays).
        self.assertEqual(self._bundle_names(), [keep])


if __name__ == "__main__":
    unittest.main()
