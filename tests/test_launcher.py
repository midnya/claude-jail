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

    def test_new_keeps_claude_args(self):
        args = L.parse_args(["-u", "me", "new", "-p", "hi"])
        self.assertEqual(args.command, "new")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_new_dashdash_stripped(self):
        # new forwards to claude like run, so it strips the -- separator too.
        args = L.parse_args(["-u", "me", "new", "--", "--help"])
        self.assertEqual(args.command, "new")
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

    def test_mem_limit_flag(self):
        self.assertEqual(L.parse_args(["--mem-limit", "8g"]).mem_limit, "8g")
        self.assertIsNone(L.parse_args(["-u", "me"]).mem_limit)

    def test_empty_mem_limit_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            L.parse_args(["--mem-limit", ""])

    def test_claude_dir_base_flag(self):
        args = L.parse_args(["--claude-dir-base", "/data/jh", "run", "-p", "hi"])
        self.assertEqual(args.claude_dir_base, "/data/jh")
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_claude_dir_base_defaults_to_none(self):
        self.assertIsNone(L.parse_args(["-u", "me"]).claude_dir_base)

    def test_system_prompt_segments_default_none(self):
        self.assertIsNone(L.parse_args(["-u", "me", "run"]).system_prompt_segments)

    def test_system_prompt_flags_ordered_and_tagged(self):
        # Both flags feed one list that preserves command-line order and tags
        # each value inline/path; they must precede the command like our other flags.
        args = L.parse_args([
            "--system-prompt", "A", "--system-prompt-file", "p.md",
            "--system-prompt", "B", "run", "-p", "hi"])
        self.assertEqual(args.system_prompt_segments,
                         [("inline", "A"), ("path", "p.md"), ("inline", "B")])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_system_prompt_flag_coexists_with_dashdash(self):
        # Our flag is parsed; -- still forwards a leading-dash arg to claude.
        args = L.parse_args(["--system-prompt", "X", "--", "--help"])
        self.assertEqual(args.system_prompt_segments, [("inline", "X")])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["--help"])

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

    def test_new_shares_run_launch_line(self):
        # new resolves to the exact run launch line; it diverges only via the
        # CLAUDE_JAIL_NEW_SESSION env signal main() pins, not the compose args.
        self.assertEqual(
            L.resolve_command("new", []),
            (["run", "--rm", "claude"], True, True),
        )
        self.assertEqual(
            L.resolve_command("new", ["-p", "hi"]),
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

    def test_dind_variants_removed_children_first(self):
        # The suffixed dind image is a package variant too (removed), while the
        # bare claude-jail/claude-jail-dind singletons are left alone. A dind image
        # derives FROM its like-suffixed base, so it must be listed (and removed)
        # before that base, or `docker image rm` refuses the base for having a
        # dependent child — the sort key puts every `-dind-` ref first.
        rc, rm = self._prune([
            "claude-jail", "claude-jail-dind",
            "claude-jail-deadbeef", "claude-jail-dind-deadbeef",
        ])
        self.assertEqual(len(rm), 1)
        self.assertEqual(rm[0][3:],
                         ["claude-jail-dind-deadbeef", "claude-jail-deadbeef"])
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


class PruneVolumesTests(JailTestCase):
    def _prune(self, volumes, rm_returncode=0):
        """Run prune_volumes() against a fake `docker volume ls` listing."""
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ["docker", "volume", "ls"]:
                return mock.Mock(returncode=0, stdout="\n".join(volumes) + "\n")
            return mock.Mock(returncode=rm_returncode)

        with mock.patch.object(L.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = L.prune_volumes()
        rm = [c for c in calls if c[:3] == ["docker", "volume", "rm"]]
        return rc, rm

    def test_removes_only_dind_data_volumes(self):
        # Only the claude-jail-dind-<...> data-root volumes are removed; the base
        # jail-tmp/squid-logs/dns-logs volumes (and anything else) are left alone.
        rc, rm = self._prune([
            "claude-jail-tmp-home-me-proj",
            "claude-jail-squid-logs-home-me-proj-abc1",
            "claude-jail-dns-logs-home-me-proj-abc1",
            "some-other-volume",
            "claude-jail-dind-home-me-proj",
            "claude-jail-dind-home-me-proj-deadbeef",
        ])
        self.assertEqual(len(rm), 1)
        self.assertEqual(rm[0][3:],
                         ["claude-jail-dind-home-me-proj",
                          "claude-jail-dind-home-me-proj-deadbeef"])
        self.assertEqual(rc, 0)

    def test_noop_when_nothing_matches(self):
        rc, rm = self._prune(["claude-jail-tmp-x", "claude-jail-squid-logs-y"])
        self.assertEqual(rm, [])  # never invokes `docker volume rm`
        self.assertEqual(rc, 0)

    def test_propagates_rm_failure(self):
        # A volume backing a live session is kept (docker rm fails); rc surfaces it.
        rc, rm = self._prune(["claude-jail-dind-proj"], rm_returncode=1)
        self.assertEqual(len(rm), 1)
        self.assertEqual(rc, 1)

    def test_dies_when_listing_fails(self):
        with mock.patch.object(L.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            with self.assertDies("could not list"):
                L.prune_volumes()


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


class MainEnvSignalTests(JailTestCase):
    """main() pins the env signals that differentiate the launch commands.

    CLAUDE_JAIL_NEW_SESSION is the *only* thing that makes `new` differ from `run`
    (resolve_command is identical for the two), and the parallel COMPOSE_PROFILES
    pin keeps a value inherited from the shell from silently activating the ide
    bridge — or suppressing the auto --continue — on a default run. resolve_command
    and the whole launch path run for real here; only the docker-touching
    collaborators are stubbed, and the signal is read back from os.environ at the
    point run_compose would hand it to `docker compose run`.
    """

    def _run_main(self, argv, preset=None, env_overrides=None, expect_die=None):
        captured = {}

        def fake_run_compose(base_cmd, compose_args, override):
            captured["new_session"] = L.os.environ.get("CLAUDE_JAIL_NEW_SESSION")
            captured["profiles"] = L.os.environ.get("COMPOSE_PROFILES")
            captured["base_cmd"] = list(base_cmd)
            return 0

        # The few env keys main() reads back out of build_jail's result.
        fake_env = {
            "JAIL_CLAUDE_DIR_BASE": "/x",
            "JAIL_USER": "me",
            "COMPOSE_PROJECT_NAME": "proj",
            "JAIL_UID": "1000",
        }
        if env_overrides:
            fake_env.update(env_overrides)
        with contextlib.ExitStack() as stack:
            # patch.dict restores os.environ (incl. the keys main() adds) on exit.
            stack.enter_context(mock.patch.dict(L.os.environ, clear=False))
            if preset:
                L.os.environ.update(preset)
            stack.enter_context(
                mock.patch.object(L.sys, "argv", ["claude-jail", *argv]))
            stack.enter_context(mock.patch.object(L, "require_docker_compose"))
            stack.enter_context(mock.patch.object(
                L, "build_jail", return_value=(dict(fake_env), "", [], None)))
            stack.enter_context(mock.patch.object(L, "ensure_user_config"))
            stack.enter_context(
                mock.patch.object(L.build_mounts, "seed_masked_configs"))
            stack.enter_context(mock.patch.object(L, "cleanup_side_containers"))
            stack.enter_context(
                mock.patch.object(L, "run_compose", side_effect=fake_run_compose))
            # Stub the docker-touching dind builders so --dind paths don't shell
            # out; record that they fired.
            stack.enter_context(mock.patch.object(
                L, "ensure_dind_image",
                side_effect=lambda env: captured.__setitem__("ensure_dind", True)))
            stack.enter_context(mock.patch.object(
                L, "dind_build_stack",
                side_effect=lambda env, flags=(): (
                    captured.__setitem__("dind_build", True)
                    or captured.__setitem__("dind_build_flags", list(flags))
                    or 0)))
            with self.assertRaises(SystemExit) as cm:
                L.main()
        if expect_die is not None:
            message = str(cm.exception.code)
            self.assertTrue(message.startswith("Error:"),
                            f"expected a die() message, got {message!r}")
            for needle in expect_die:
                self.assertIn(needle, message)
        else:
            self.assertEqual(cm.exception.code, 0)
        return captured

    def test_dind_run_adds_overlay_and_builds_on_demand(self):
        # --dind run inserts the overlay as a second -f, after the base compose
        # file and before run_compose appends the mount override — so it merges
        # onto the real `claude` service — and ensures the image is built first.
        captured = self._run_main(["--dind", "run"])
        base_cmd = captured["base_cmd"]
        self.assertEqual(base_cmd[:4],
                         ["docker", "compose", "-f", str(L.COMPOSE_FILE)])
        self.assertEqual(base_cmd[-2:], ["-f", str(L.DIND_OVERRIDE)])
        self.assertTrue(captured.get("ensure_dind"))

    def test_no_dind_omits_override_and_build(self):
        # A normal run never mentions the overlay or the dind build: prod
        # behaviour is untouched.
        captured = self._run_main(["run"])
        self.assertNotIn(str(L.DIND_OVERRIDE), captured["base_cmd"])
        self.assertNotIn("ensure_dind", captured)

    def test_dind_run_rejects_root_uid(self):
        # The rootless daemon in the dind image refuses to run as root, so a uid-0
        # config makes --dind unusable: die with a clear message before the
        # on-demand build rather than let dockerd abort opaquely at startup.
        captured = self._run_main(["--dind", "run"], env_overrides={"JAIL_UID": "0"},
                                  expect_die=["--dind", "uid 0"])
        # Died before the on-demand build fired.
        self.assertNotIn("ensure_dind", captured)

    def test_dind_build_builds_dind_image(self):
        # --dind build is its own path: it builds the dind stack and never reaches
        # run_compose (no base_cmd captured), so the overlay's unbuildable FROM is
        # never handed to `docker compose build`.
        captured = self._run_main(["--dind", "build"])
        self.assertTrue(captured.get("dind_build"))
        self.assertNotIn("base_cmd", captured)

    def test_dind_build_forwards_build_flags(self):
        # The --no-cache/--pull that resolve_command validated must reach
        # dind_build_stack, not be silently dropped by the short-circuit.
        captured = self._run_main(["--dind", "build", "--no-cache"])
        self.assertEqual(captured.get("dind_build_flags"), ["--no-cache"])

    def test_dind_rejected_for_non_dind_command(self):
        # --dind is fail-closed to the run/new/build/down/logs/ps allow-list: a
        # host-side command (prune / the review family) or the raw compose hatch
        # is rejected up front, never silently stripped of the flag — and dies
        # before any build or launch machinery runs.
        for command in ("prune", "accept", "compose"):
            captured = self._run_main(["--dind", command],
                                      expect_die=["--dind", command])
            self.assertNotIn("base_cmd", captured)
            self.assertNotIn("ensure_dind", captured)
            self.assertNotIn("dind_build", captured)

    def test_dind_read_only_command_adds_overlay_without_build(self):
        # logs/ps/down are on the allow-list but don't launch: the overlay is
        # layered (so the dind service is what's inspected/torn down), yet no
        # on-demand build fires.
        captured = self._run_main(["--dind", "logs"])
        self.assertEqual(captured["base_cmd"][-2:], ["-f", str(L.DIND_OVERRIDE)])
        self.assertNotIn("ensure_dind", captured)

    def test_new_sets_session_signal(self):
        self.assertEqual(self._run_main(["new"])["new_session"], "1")

    def test_run_pins_session_signal_empty(self):
        # Both the explicit `run` and the bare default pin it to "" (resume).
        self.assertEqual(self._run_main(["run"])["new_session"], "")
        self.assertEqual(self._run_main([])["new_session"], "")

    def test_run_clobbers_inherited_session_signal(self):
        # A value inherited from the shell can't silently suppress the resume on a
        # normal run: main() pins it to "" regardless.
        captured = self._run_main(["run"],
                                  preset={"CLAUDE_JAIL_NEW_SESSION": "1"})
        self.assertEqual(captured["new_session"], "")

    def test_non_ide_run_pins_profiles_empty(self):
        # The parallel pin: a default (non-ide) run forces COMPOSE_PROFILES to ""
        # even when the shell exported one.
        captured = self._run_main(["new"], preset={"COMPOSE_PROFILES": "ide"})
        self.assertEqual(captured["profiles"], "")


class MemLimitBuildTests(JailTestCase):
    """✦ build_jail renders mem_limit into the override, never the env."""

    def _build(self, body, flag=None):
        proj = self.tmpdir()
        cfg = self.write(os.path.join(proj, ".claude-jail.json"), body)
        env, override, _seeds, _ctx = L.build_jail(
            None, cfg, None, self.tmpdir(), store_must_exist=True,
            mem_limit_flag=flag)
        return env, override

    def test_unlimited_by_default(self):
        _env, override = self._build('{"user": "t"}')
        self.assertNotIn("mem_limit", override)

    def test_config_key_rendered(self):
        env, override = self._build('{"user": "t", "mem_limit": "8g"}')
        self.assertIn("    mem_limit: 8g\n", override)
        self.assertNotIn("8g", env.values())

    def test_flag_overrides_config(self):
        _env, override = self._build('{"user": "t", "mem_limit": "8g"}', "2g")
        self.assertIn("    mem_limit: 2g\n", override)
        self.assertNotIn("8g", override)


class ReviewIsolationBuildTests(JailTestCase):
    """build_jail wires git-review isolation into the env, override and ctx."""

    def _config(self, body):
        proj = self.tmpdir()
        # review requires a normal .git directory to shadow (build_jail rejects a
        # linked-worktree gitfile or non-git root), so give the project one.
        self.mkdir(os.path.join(proj, ".git"))
        cfg = self.write(os.path.join(proj, ".claude-jail.json"), body)
        return proj, cfg

    def test_review_isolation_wired(self):
        base = self.tmpdir()
        proj, cfg = self._config('{"user": "t", "review": true}')
        env, override, _seeds, review_ctx = L.build_jail(
            None, cfg, None, base, store_must_exist=True)
        self.assertEqual(review_ctx["repo"], proj)
        self.assertEqual(env["JAIL_REVIEW"], "1")
        self.assertTrue(env["JAIL_GIT_ORIGINAL"].endswith(proj))
        self.assertEqual(env["JAIL_REVIEW_SPOOL"], "/vcs/spool")
        # the override carries the isolation mounts (real .git, gitdir, spool)
        self.assertIn(f"/vcs/git{proj}", override)
        self.assertIn(review_ctx["gitdir"], override)
        self.assertIn("/vcs/spool", override)

    def test_review_off_by_default(self):
        base = self.tmpdir()
        _proj, cfg = self._config('{"user": "t"}')
        env, override, _seeds, review_ctx = L.build_jail(
            None, cfg, None, base, store_must_exist=True)
        self.assertIsNone(review_ctx)
        self.assertEqual(env["JAIL_REVIEW"], "")
        self.assertNotIn("/vcs/git", override)

    def test_review_flag_forces_isolation_without_config_key(self):
        base = self.tmpdir()
        proj, cfg = self._config('{"user": "t"}')
        env, _override, _seeds, review_ctx = L.build_jail(
            None, cfg, None, base, store_must_exist=True, review_flag=True)
        self.assertEqual(review_ctx["repo"], proj)
        self.assertEqual(env["JAIL_REVIEW"], "1")

    def test_inherited_jail_review_does_not_leak_in(self):
        # The defensive pin: a JAIL_REVIEW exported in the shell must not switch
        # the entrypoint's git-review path on for a non-review run.
        base = self.tmpdir()
        _proj, cfg = self._config('{"user": "t"}')
        with mock.patch.dict(L.os.environ, {"JAIL_REVIEW": "1"}, clear=False):
            env, _o, _s, review_ctx = L.build_jail(
                None, cfg, None, base, store_must_exist=True)
        self.assertIsNone(review_ctx)
        self.assertEqual(env["JAIL_REVIEW"], "")

    def test_review_requires_config_dir_among_roots(self):
        # review isolates the config's own directory; if that dir isn't a root,
        # there's nothing to isolate and the config is rejected.
        base = self.tmpdir()
        proj, cfg = self._config(
            '{"user": "t", "review": true, "roots": ["sub"]}')
        self.mkdir(os.path.join(proj, "sub"))
        with self.assertDies(
                "review needs the config's own directory to be a jail root"):
            L.build_jail(None, cfg, None, base, store_must_exist=True)

    def test_review_isolation_deferred_when_base_unresolved(self):
        # review on for a launch (store_must_exist=True) but no store base
        # resolvable (no flag, no config key, no $HOME): the `if claude_dir_base:`
        # guard leaves isolation off so main()'s launch path reports the base.
        # store_must_exist MUST be True: with False the whole isolation block is
        # short-circuited earlier, so the test would pass without ever reaching the
        # guard it names (and would stay green even if that guard were removed).
        _proj, cfg = self._config('{"user": "t", "review": true}')
        with mock.patch.dict(L.os.environ, clear=False):
            L.os.environ.pop("HOME", None)
            env, _o, _s, review_ctx = L.build_jail(
                None, cfg, None, None, store_must_exist=True)
        self.assertIsNone(review_ctx)
        self.assertEqual(env["JAIL_REVIEW"], "")
        self.assertNotIn("JAIL_GIT_ORIGINAL", env)

    def test_review_rejects_linked_worktree_gitfile_root(self):
        # A linked-worktree root has .git as a *file*; the shadow-.git overmount
        # needs a directory, so build_jail refuses up front rather than producing
        # an opaque docker mount error later.
        base = self.tmpdir()
        proj = self.tmpdir()
        self.write(os.path.join(proj, ".git"), "gitdir: /elsewhere/.git\n")
        cfg = self.write(os.path.join(proj, ".claude-jail.json"),
                         '{"user": "t", "review": true}')
        with self.assertDies("normal git directory"):
            L.build_jail(None, cfg, None, base, store_must_exist=True)

    def test_review_rejects_read_only_root(self):
        # A read-only working tree can't take the agent's commits, so review on a
        # root the config pins read_only is refused.
        base = self.tmpdir()
        proj, cfg = self._config(
            '{"user": "t", "review": true, '
            '"roots": [{"path": ".", "read_only": ["."]}]}')
        with self.assertDies("writable project root"):
            L.build_jail(None, cfg, None, base, store_must_exist=True)

    def test_review_rejects_whole_git_hidden(self):
        # A `hidden: [".git"]` mask can't be honoured in review mode (the real .git
        # is exposed read-only at /vcs/git for object borrowing), so it would leak
        # the .git the user meant to hide — build_jail refuses rather than leak.
        base = self.tmpdir()
        proj, cfg = self._config(
            '{"user": "t", "review": true, '
            '"roots": [{"path": ".", "hidden": [".git"]}]}')
        with self.assertDies("whole .git is hidden"):
            L.build_jail(None, cfg, None, base, store_must_exist=True)

    def test_review_rejects_whole_git_hidden_noncanonical(self):
        # The refusal must normalize like the mask machinery: a non-canonical
        # spelling such as ".git/." still masks the entire .git in a normal run, so
        # review must refuse it too — a raw string compare would let it slip past
        # and leave the real .git exposed read-only at /vcs/git to the agent.
        base = self.tmpdir()
        proj, cfg = self._config(
            '{"user": "t", "review": true, '
            '"roots": [{"path": ".", "hidden": [".git/."]}]}')
        with self.assertDies("whole .git is hidden"):
            L.build_jail(None, cfg, None, base, store_must_exist=True)

    def test_review_allows_git_subpath_hidden(self):
        # Hiding a specific .git sub-path is fine — override() re-masks it over
        # /vcs/git — so review is NOT refused for it.
        base = self.tmpdir()
        proj, cfg = self._config(
            '{"user": "t", "review": true, '
            '"roots": [{"path": ".", "hidden": [".git/config"]}]}')
        # The masked .git sub-path must exist to be re-masked over /vcs/git; an
        # absent one is a hard error (a typo), just as in a normal run.
        self.write(os.path.join(proj, ".git", "config"), "x")
        _env, _o, _s, review_ctx = L.build_jail(
            None, cfg, None, base, store_must_exist=True)
        self.assertIsNotNone(review_ctx)

    def test_review_rejects_absent_git_subpath_hidden(self):
        # A mistyped `.git/...` hidden entry must fail loudly in review mode, not
        # be silently dropped — otherwise the real .git sub-path it meant to hide
        # stays readable to the agent through the read-only /vcs/git mount.
        base = self.tmpdir()
        _proj, cfg = self._config(
            '{"user": "t", "review": true, '
            '"roots": [{"path": ".", "hidden": [".git/nonexistent"]}]}')
        with self.assertDies("hidden path not found"):
            L.build_jail(None, cfg, None, base, store_must_exist=True)


class ReviewCommandRoutingTests(JailTestCase):
    """main() sends review commands to review.dispatch and never touches docker."""

    def test_review_command_routes_to_dispatch_before_docker(self):
        seen = {}

        def fake_dispatch(command, command_args, config_path, user, base):
            seen.update(command=command, args=list(command_args))
            return 7

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                L.sys, "argv", ["claude-jail", "review", "b"]))
            stack.enter_context(mock.patch.object(
                L, "require_docker_compose",
                side_effect=AssertionError("docker must not be touched")))
            stack.enter_context(
                mock.patch.object(L.review, "dispatch", fake_dispatch))
            with self.assertRaises(SystemExit) as cm:
                L.main()
        self.assertEqual(cm.exception.code, 7)
        self.assertEqual(seen, {"command": "review", "args": ["b"]})

    def test_review_refused_on_persistent_launch(self):
        # `compose -- up` launches but never tears down, so review capture could
        # never finalize: main() refuses it before launching anything.
        review_ctx = {"repo": "/r", "spool": "/s", "gitdir": "/g"}
        fake_env = {"JAIL_CLAUDE_DIR_BASE": "/x", "JAIL_USER": "me",
                    "COMPOSE_PROJECT_NAME": "proj", "JAIL_WORKDIR": "/workspace"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                L.sys, "argv", ["claude-jail", "compose", "--", "up"]))
            stack.enter_context(mock.patch.object(L, "require_docker_compose"))
            stack.enter_context(mock.patch.object(
                L, "build_jail",
                return_value=(dict(fake_env), "", [], review_ctx)))
            stack.enter_context(mock.patch.object(
                L, "run_compose",
                side_effect=AssertionError("must not launch")))
            with self.assertDies("only supported for one-shot"):
                L.main()
