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

        # The scripted agent, run *inside* the jail in place of claude: make one
        # commit in the review workspace, then exit. Lives in the repo (a mounted
        # root), referenced by its container path.
        agent = Path(self.repo, ".e2e-agent.sh")
        self.write(agent,
                   "#!/bin/sh\n"
                   "set -e\n"
                   'cd "${JAIL_WORKDIR:-/workspace}"\n'
                   "printf 'agent was here\\n' > agent-artifact.txt\n"
                   "git add agent-artifact.txt\n"
                   "git commit -q -m 'e2e: agent commit'\n")
        agent.chmod(0o755)
        self.agent_cmd = build_mounts.container_path(self.repo) + "/.e2e-agent.sh"

    # -- helpers -----------------------------------------------------------

    def _jail(self, *args: str, agent: bool = False) -> "subprocess.CompletedProcess":
        """Invoke the `claude-jail` launcher; capture output, no tty (so `accept`
        proceeds past its warning and `review` doesn't paginate)."""
        env = dict(os.environ)
        if agent:
            # CLAUDE_JAIL_E2E gates the launcher's forwarding of the agent stand-in.
            env["CLAUDE_JAIL_E2E"] = "1"
            env["CLAUDE_JAIL_AGENT_CMD"] = self.agent_cmd
        return subprocess.run([sys.executable, _LAUNCHER, "-c", self.cfg, *args],
                              capture_output=True, text=True, env=env,
                              timeout=_TIMEOUT)

    def _bundle_name(self) -> str:
        """The single captured bundle's name, from the host-only review store."""
        bundles = sorted(Path(self.base).glob(".claude-jail-*-review/*.bundle"))
        self.assertEqual(len(bundles), 1,
                         f"expected exactly one captured bundle, got {bundles}")
        return bundles[0].name[:-len(".bundle")]

    # -- the round trip ----------------------------------------------------

    def test_review_round_trip(self) -> None:
        # `--review` before the command: it is a launcher flag, so a trailing
        # `run --review` would be swallowed into the agent's args (argparse
        # REMAINDER) and NOT force isolation. Placed here it exercises the flag
        # path itself, independently of the config's own "review": true.
        run = self._jail("--review", "run", agent=True)
        self.assertEqual(run.returncode, 0,
                         f"--review run failed:\nSTDOUT{run.stdout}\nSTDERR{run.stderr}")

        name = self._bundle_name()

        review = self._jail("review", name)
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertIn("agent-artifact.txt", review.stdout,
                      "review diff should show the agent's new file")

        accept = self._jail("accept", name)
        self.assertEqual(accept.returncode, 0, accept.stderr)

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
        self.assertEqual(apply.returncode, 0,
                         f"apply failed:\n{apply.stdout}\n{apply.stderr}")

        # The agent's commit is now on the real branch, and apply re-materialised
        # its file (removed above) into the tree by fast-forwarding.
        self.assertIn("e2e: agent commit", self.git(self.repo, "log", "--oneline"))
        self.assertTrue(artifact.is_file(),
                        "apply should have written the agent's file into the tree")


if __name__ == "__main__":
    unittest.main()
