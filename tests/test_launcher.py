"""Tests for the `claude-jail` launcher: arg parsing, identity, docker glue."""
import contextlib
import io
import os
from unittest import mock

from jail_test_helpers import JailTestCase, load_launcher  # noqa: I001

L = load_launcher()


class ParseArgsTests(JailTestCase):
    def test_user_flag_and_remainder(self):
        args = L.parse_args(["-u", "me", "run", "--rm", "svc"])
        self.assertEqual(args.user, "me")
        self.assertIsNone(args.config)
        self.assertEqual(args.compose_args, ["run", "--rm", "svc"])

    def test_bare_subcommand_without_our_flags(self):
        args = L.parse_args(["build", "--no-cache"])
        self.assertIsNone(args.user)
        self.assertIsNone(args.config)
        self.assertEqual(args.compose_args, ["build", "--no-cache"])

    def test_leading_dashdash_stripped(self):
        args = L.parse_args(["-u", "me", "--", "--progress", "plain", "run"])
        self.assertEqual(args.compose_args, ["--progress", "plain", "run"])

    def test_config_flag(self):
        args = L.parse_args(["--config", "/x/y.json", "build"])
        self.assertEqual(args.config, "/x/y.json")
        self.assertEqual(args.compose_args, ["build"])

    def test_no_compose_args(self):
        self.assertEqual(L.parse_args(["-u", "me"]).compose_args, [])

    def test_empty_user_rejected(self):
        # argparse prints usage to stderr then exits 2; swallow the noise.
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--user", ""])

    def test_empty_config_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--config", ""])


class ComposeSubcommandTests(JailTestCase):
    def test_plain(self):
        self.assertEqual(L.compose_subcommand(["run", "--rm"]), "run")

    def test_skips_value_global_flag(self):
        self.assertEqual(L.compose_subcommand(["--progress", "plain", "run"]),
                         "run")

    def test_skips_valueless_global_flag(self):
        self.assertEqual(L.compose_subcommand(["--dry-run", "ps"]), "ps")

    def test_skips_joined_value_global_flag(self):
        self.assertEqual(L.compose_subcommand(["--progress=plain", "run"]),
                         "run")

    def test_empty(self):
        self.assertIsNone(L.compose_subcommand([]))

    def test_dangling_value_flag(self):
        self.assertIsNone(L.compose_subcommand(["--progress"]))


class JailIdentityTests(JailTestCase):
    def test_slug_collapses_non_alphanumerics(self):
        sub = self.mkdir(os.path.join(self.tmpdir(), "proj.v1_2 x"))
        cfg = self.write(os.path.join(sub, ".claude-jail.json"), "{}")
        jail_id, project = L.jail_identity(cfg)
        # JAIL_ID slugs the whole config-file realpath (volumes stay unique).
        self.assertTrue(jail_id.endswith("proj-v1-2-x-claude-jail-json"),
                        f"unexpected slug tail: {jail_id}")
        self.assertRegex(jail_id, r"\A[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")
        # The compose project name slugs only the config's directory name.
        self.assertEqual(project, "proj-v1-2-x")

    def test_project_is_lowercased_dir_basename(self):
        sub = self.mkdir(os.path.join(self.tmpdir(), "MyProject"))
        cfg = self.write(os.path.join(sub, ".claude-jail.json"), "{}")
        jail_id, project = L.jail_identity(cfg)
        self.assertEqual(project, "myproject")
        self.assertNotEqual(project, jail_id)  # decoupled from the volume id

    def test_root_fallback(self):
        # A config that resolves to "/" slugs to nothing, falling back to "root".
        self.assertEqual(L.jail_identity("/"), ("root", "root"))


class EgressIdTests(JailTestCase):
    def test_short_stable_and_policy_specific(self):
        a = L.egress_id_of("http_access allow localnet jail_allow_dom")
        b = L.egress_id_of("http_access allow localnet jail_allow_dom")
        c = L.egress_id_of("http_access allow localnet")
        self.assertEqual(a, b)         # same policy -> shared proxy
        self.assertNotEqual(a, c)      # different policy -> separate proxy
        self.assertRegex(a, r"\A[0-9a-f]{8}\Z")


class ContainerWorkdirTests(JailTestCase):
    def test_workdir_is_config_dir_under_workspace(self):
        d = self.tmpdir()
        cfg = self.write(os.path.join(d, ".claude-jail.json"), "{}")
        self.assertEqual(L.container_workdir(cfg), "/workspace" + d)


class RequireDockerComposeTests(JailTestCase):
    def test_ok_when_plugin_present(self):
        with mock.patch.object(L.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            L.require_docker_compose()  # no raise

    def test_dies_when_docker_missing(self):
        with mock.patch.object(L.subprocess, "run", side_effect=OSError):
            with self.assertDies("docker not found"):
                L.require_docker_compose()

    def test_dies_when_plugin_missing(self):
        with mock.patch.object(L.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            with self.assertDies("docker compose plugin not found"):
                L.require_docker_compose()


class CleanupSideContainersTests(JailTestCase):
    def test_down_when_no_live_session(self):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ["docker", "ps", "-q"]:
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=0)

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run):
            L.cleanup_side_containers(["docker", "compose"], "proj")
        self.assertTrue(any("down" in c for c in calls))

    def test_no_down_when_session_still_live(self):
        def fake_run(cmd, *a, **k):
            if cmd[:3] == ["docker", "ps", "-q"]:
                return mock.Mock(returncode=0, stdout="abc123\n")
            self.fail("down must not run while a session is live")

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run):
            L.cleanup_side_containers(["docker", "compose"], "proj")

    def test_no_down_when_count_fails(self):
        def fake_run(cmd, *a, **k):
            if cmd[:3] == ["docker", "ps", "-q"]:
                return mock.Mock(returncode=1, stdout="")
            self.fail("down must not run when the count fails")

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run):
            L.cleanup_side_containers(["docker", "compose"], "proj")


class RunComposeTests(JailTestCase):
    def test_no_override_runs_plain_command(self):
        captured = {}

        def fake_run(cmd, *a, **k):
            captured["cmd"] = cmd
            return mock.Mock(returncode=7)

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run):
            rc = L.run_compose(["docker", "compose"], ["ps"], "")
        self.assertEqual(rc, 7)
        self.assertEqual(captured["cmd"], ["docker", "compose", "ps"])
        self.assertNotIn("-f", captured["cmd"])

    def test_override_passed_via_dev_fd_pipe(self):
        captured = {}

        def fake_run(cmd, *a, **k):
            captured["cmd"] = cmd
            captured["pass_fds"] = k.get("pass_fds")
            return mock.Mock(returncode=0)

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run):
            rc = L.run_compose(["docker", "compose"], ["up"], "services: {}\n")
        self.assertEqual(rc, 0)
        self.assertIn("-f", captured["cmd"])
        self.assertTrue(any(str(c).startswith("/dev/fd/")
                            for c in captured["cmd"]))
        self.assertTrue(captured["pass_fds"])  # read end inherited by docker
