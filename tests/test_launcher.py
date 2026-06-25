"""Tests for the `claude-jail` launcher: arg parsing, identity, docker glue."""
import contextlib
import io
import os
from unittest import mock

from jail_test_helpers import JailTestCase, load_launcher  # noqa: I001

L = load_launcher()


class ParseArgsTests(JailTestCase):
    def test_default_command_is_run(self):
        args = L.parse_args(["-u", "me"])
        self.assertEqual(args.user, "me")
        self.assertIsNone(args.config)
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, [])

    def test_explicit_run_keeps_claude_args(self):
        args = L.parse_args(["-u", "me", "run", "-p", "hi"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_default_run_dashdash_passes_claude_args(self):
        # No command word: the remainder is claude's, and -- lets a leading-dash
        # arg through (argparse would otherwise reject it).
        args = L.parse_args(["-u", "me", "--", "--help"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["--help"])

    def test_explicit_run_dashdash_stripped(self):
        args = L.parse_args(["-u", "me", "run", "--", "--help"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["--help"])

    def test_dashdash_kept_for_strict_command(self):
        # -- is a passthrough separator only for run/compose; on the other
        # commands it stays an argument, so `down --` is rejected like `down x`.
        args = L.parse_args(["-u", "me", "down", "--"])
        self.assertEqual(args.command, "down")
        self.assertEqual(args.command_args, ["--"])
        with self.assertDies("down accepts only"):
            L.resolve_command(args.command, args.command_args)

    def test_build_with_flag(self):
        args = L.parse_args(["-u", "me", "build", "--no-cache"])
        self.assertEqual(args.command, "build")
        self.assertEqual(args.command_args, ["--no-cache"])

    def test_down(self):
        args = L.parse_args(["-u", "me", "down"])
        self.assertEqual(args.command, "down")
        self.assertEqual(args.command_args, [])

    def test_logs_takes_service(self):
        args = L.parse_args(["-u", "me", "logs", "squid"])
        self.assertEqual(args.command, "logs")
        self.assertEqual(args.command_args, ["squid"])

    def test_prune(self):
        args = L.parse_args(["prune"])
        self.assertEqual(args.command, "prune")
        self.assertEqual(args.command_args, [])

    def test_compose_escape_hatch(self):
        args = L.parse_args(["-u", "me", "compose", "--", "ps", "-a"])
        self.assertEqual(args.command, "compose")
        self.assertEqual(args.command_args, ["ps", "-a"])

    def test_command_without_our_flags(self):
        args = L.parse_args(["build", "--no-cache"])
        self.assertIsNone(args.user)
        self.assertIsNone(args.config)
        self.assertEqual(args.command, "build")
        self.assertEqual(args.command_args, ["--no-cache"])

    def test_config_flag(self):
        args = L.parse_args(["--config", "/x/y.json", "build"])
        self.assertEqual(args.config, "/x/y.json")
        self.assertEqual(args.command, "build")

    def test_subnet_flag_precedes_command(self):
        args = L.parse_args(["--subnet", "10.1.2.0/24", "run", "-p", "hi"])
        self.assertEqual(args.subnet, "10.1.2.0/24")
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_subnet_defaults_to_none(self):
        self.assertIsNone(L.parse_args(["-u", "me"]).subnet)

    def test_claude_dir_base_flag(self):
        args = L.parse_args(["--claude-dir-base", "/data/jh", "run", "-p", "hi"])
        self.assertEqual(args.claude_dir_base, "/data/jh")
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_claude_dir_base_defaults_to_none(self):
        self.assertIsNone(L.parse_args(["-u", "me"]).claude_dir_base)

    def test_empty_user_rejected(self):
        # argparse prints usage to stderr then exits 2; swallow the noise.
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--user", ""])

    def test_empty_subnet_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--subnet", ""])

    def test_empty_config_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--config", ""])

    def test_empty_claude_dir_base_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--claude-dir-base", ""])


class ResolveCommandTests(JailTestCase):
    # resolve_command returns (compose_args, launches, teardown).
    def test_run_builds_strict_launch_line(self):
        # Every claude arg lands after the service name, so it can never be a
        # docker run flag — the core safety property. A run always tears down.
        self.assertEqual(
            L.resolve_command("run", ["-p", "hi"]),
            (["run", "--rm", "claude", "-p", "hi"], True, True),
        )

    def test_build_plain_and_flags(self):
        # No service is named, so `docker compose build` builds both images.
        self.assertEqual(L.resolve_command("build", []),
                         (["build"], False, False))
        self.assertEqual(
            L.resolve_command("build", ["--no-cache", "--pull"]),
            (["build", "--no-cache", "--pull"], False, False),
        )

    def test_build_unknown_flag_rejected(self):
        with self.assertDies("build accepts only"):
            L.resolve_command("build", ["-v", "/etc:/x"])

    def test_down_activates_ide_profile(self):
        # down must activate the ide profile so it reaps ide-relay/ide-host left
        # by an earlier --ide run; a plain down skips inactive-profile services.
        p = L.ide.profile_args()
        self.assertEqual(L.resolve_command("down", []),
                         ([*p, "down", "--timeout", "0"], False, False))

    def test_down_accepts_volumes(self):
        p = L.ide.profile_args()
        self.assertEqual(L.resolve_command("down", ["-v"]),
                         ([*p, "down", "--timeout", "0", "-v"], False, False))
        self.assertEqual(L.resolve_command("down", ["--volumes"]),
                         ([*p, "down", "--timeout", "0", "--volumes"], False, False))

    def test_down_rejects_args(self):
        with self.assertDies("down accepts only"):
            L.resolve_command("down", ["claude"])

    def test_logs_and_ps_forward_args_read_only(self):
        self.assertEqual(L.resolve_command("logs", ["squid"]),
                         (["logs", "squid"], False, False))
        self.assertEqual(L.resolve_command("ps", []), (["ps"], False, False))

    def test_compose_run_launches_and_tears_down(self):
        self.assertEqual(
            L.resolve_command("compose", ["run", "--rm", "claude"]),
            (["run", "--rm", "claude"], True, True),
        )
        self.assertEqual(L.resolve_command("compose", ["ps"]),
                         (["ps"], False, False))

    def test_compose_up_and_watch_launch_without_teardown(self):
        # up / start / watch start containers (so they need the override) but are
        # left running deliberately, so they must NOT trigger the post-run
        # teardown. This is the half the production comment calls out.
        self.assertEqual(L.resolve_command("compose", ["up", "-d"]),
                         (["up", "-d"], True, False))
        self.assertEqual(L.resolve_command("compose", ["watch"]),
                         (["watch"], True, False))

    def test_compose_without_subcommand_dies(self):
        with self.assertDies("compose needs"):
            L.resolve_command("compose", [])
        with self.assertDies("compose needs"):
            L.resolve_command("compose", ["--progress", "plain"])

    def test_unhandled_command_is_not_misrouted_to_compose(self):
        # prune (and any future non-compose command) is dispatched in main()
        # before reaching here; if it ever leaks through, fail loudly rather than
        # silently treating it as a verbatim compose invocation.
        with self.assertRaises(AssertionError):
            L.resolve_command("prune", [])


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


class DnsEndpointTests(JailTestCase):
    def test_stable_project_specific_and_well_formed(self):
        import ipaddress
        a_net, a_ip = L.dns_endpoint("proj-aabbccdd")
        b_net, b_ip = L.dns_endpoint("proj-aabbccdd")
        c_net, _ = L.dns_endpoint("other-11223344")
        self.assertEqual((a_net, a_ip), (b_net, b_ip))   # same project -> same net
        self.assertNotEqual(a_net, c_net)                # different project -> different net
        # The resolver IP is the .53 host of its /24 subnet.
        net = ipaddress.ip_network(a_net)
        self.assertEqual(net.prefixlen, 24)
        self.assertIn(ipaddress.ip_address(a_ip), net)
        self.assertTrue(a_ip.endswith(".53"))

    def test_subnet_override_used_with_dot53_host(self):
        # An explicit subnet wins over the derived one; resolver keeps the .53.
        self.assertEqual(L.dns_endpoint("proj", "10.123.45.0/24"),
                         ("10.123.45.0/24", "10.123.45.53"))
        self.assertEqual(L.dns_endpoint("proj", "172.20.0.0/22"),
                         ("172.20.0.0/22", "172.20.0.53"))

    def test_subnet_override_rejects_bad_values(self):
        # (value, expected message fragment): pinning the reason keeps a
        # regression that dies for the wrong cause from passing silently.
        cases = [
            ("nonsense", "not a valid CIDR"),
            ("10.0.0.1/24", "not a valid CIDR"),  # host bits set (strict=True)
            ("fd00::/64", "IPv4"),                # IPv6
            ("8.8.8.0/24", "private"),            # public range
            ("100.64.0.0/24", "private"),         # CGNAT, not RFC1918
            ("0.0.0.0/0", "private"),             # /0, also not RFC1918
            ("10.0.0.0/27", "too small"),         # RFC1918 but no room for .53
        ]
        for value, needle in cases:
            with self.assertDies(needle):
                L.dns_endpoint("proj", value)


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
        # The reap activates the ide profile so it also removes ide-relay/ide-host.
        down = next(c for c in calls if "down" in c)
        self.assertEqual(down, ["docker", "compose", *L.ide.profile_args(),
                                "down", "--timeout", "0"])

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


class PruneImagesTests(JailTestCase):
    def _prune(self, repos, rm_returncode=0):
        """Run prune_images() against a fake `docker images` listing of `repos`."""
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:2] == ["docker", "images"]:
                return mock.Mock(returncode=0, stdout="\n".join(repos) + "\n")
            return mock.Mock(returncode=rm_returncode)

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = L.prune_images()
        rm = [c for c in calls if c[:3] == ["docker", "image", "rm"]]
        return rc, rm

    def test_removes_only_digest_variants(self):
        # The base image and the squid side service are left alone; only the
        # claude-jail-<8 hex> package variants are removed, sorted/deduped.
        rc, rm = self._prune([
            "claude-jail", "claude-jail-squid", "node", "<none>",
            "claude-jail-deadbeef", "claude-jail-0badf00d",
        ])
        self.assertEqual(len(rm), 1)
        self.assertEqual(rm[0][3:],
                         ["claude-jail-0badf00d", "claude-jail-deadbeef"])
        self.assertEqual(rc, 0)

    def test_noop_when_nothing_matches(self):
        rc, rm = self._prune(["claude-jail", "claude-jail-squid", "node"])
        self.assertEqual(rm, [])  # never invokes `docker image rm`
        self.assertEqual(rc, 0)

    def test_near_miss_names_are_left_alone(self):
        # Not 8 hex: a too-short/too-long digest or a non-hex char must not match.
        rc, rm = self._prune([
            "claude-jail-deadbee",     # 7 chars
            "claude-jail-deadbeeff",   # 9 chars
            "claude-jail-deadbeeg",    # 'g' is not hex
            "claude-jail-tmp-foo",
        ])
        self.assertEqual(rm, [])
        self.assertEqual(rc, 0)

    def test_propagates_rm_failure(self):
        # A live session keeps its image (docker rm fails); the rc surfaces it.
        rc, rm = self._prune(["claude-jail-deadbeef"], rm_returncode=1)
        self.assertEqual(len(rm), 1)
        self.assertEqual(rc, 1)

    def test_dies_when_listing_fails(self):
        with mock.patch.object(L.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            with self.assertDies("could not list"):
                L.prune_images()

    def test_dies_when_docker_missing(self):
        # prune drives the bare `docker` CLI directly (no compose preflight), so
        # an absent binary surfaces as a clean die, not a raw OSError traceback.
        with mock.patch.object(L.subprocess, "run", side_effect=OSError):
            with self.assertDies("docker not found"):
                L.prune_images()


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
