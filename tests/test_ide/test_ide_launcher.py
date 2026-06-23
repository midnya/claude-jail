"""The --ide flag, the `ide` facade env, and prepare()/cleanup() host state."""
import contextlib
import io
from pathlib import Path
from unittest import mock

import ide
from jail_test_helpers import JailTestCase, load_launcher  # noqa: I001

L = load_launcher()


class IdeFlagTests(JailTestCase):
    def test_defaults_off(self):
        self.assertFalse(L.parse_args(["-u", "me"]).ide)

    def test_flag_before_command(self):
        args = L.parse_args(["--ide", "run", "-p", "hi"])
        self.assertTrue(args.ide)
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["-p", "hi"])

    def test_flag_after_command_reaches_claude_not_us(self):
        # Like --subnet, --ide must precede the command; placed after, it is a
        # claude argument (REMAINDER), so our flag stays off.
        args = L.parse_args(["run", "--ide"])
        self.assertFalse(args.ide)
        self.assertEqual(args.command, "run")
        self.assertEqual(args.command_args, ["--ide"])


class IdeEnvironmentTests(JailTestCase):
    def test_core_keys(self):
        with mock.patch.object(ide, "_host_gateway_ip", return_value=None):
            env = ide.environment()
        # COMPOSE_PROFILES is owned by the launcher (main), not the facade.
        # The override is the real Claude Code var (set directly), so compose can
        # pass it through only when present and omit it on a default run.
        self.assertEqual(env, {
            "CLAUDE_CODE_IDE_HOST_OVERRIDE": "ide-relay",
            "JAIL_IDE_TARGET": "host.docker.internal",
            "JAIL_IDE_WORKSPACE": "/workspace",
            "JAIL_IDE_NO_PROXY": ",ide-relay",
        })

    def test_facade_does_not_set_compose_profiles(self):
        # Single owner: the profile pin lives in main(), so environment() must
        # not also carry COMPOSE_PROFILES (a second writer that could diverge).
        with mock.patch.object(ide, "_host_gateway_ip", return_value=None):
            self.assertNotIn("COMPOSE_PROFILES", ide.environment())

    def test_workspace_prefix_matches_mount_mapping(self):
        # The relay rewrites paths against this prefix; it must equal the single
        # host->container mapping the binds use (jail_config.container_path).
        from jail_config import container_path
        with mock.patch.object(ide, "_host_gateway_ip", return_value=None):
            self.assertEqual(ide.environment()["JAIL_IDE_WORKSPACE"],
                             container_path(""))

    def test_no_proxy_exemption_is_ide_only(self):
        # The compose default NO_PROXY appends ${JAIL_IDE_NO_PROXY:-}; without
        # --ide the var is unset, so the proxy bypass exists only for an --ide run.
        with mock.patch.object(ide, "_host_gateway_ip", return_value=None):
            self.assertEqual(ide.environment()["JAIL_IDE_NO_PROXY"], ",ide-relay")

    def test_gateway_included_when_known(self):
        with mock.patch.object(ide, "_host_gateway_ip", return_value="172.17.0.1"):
            env = ide.environment()
        self.assertEqual(env["JAIL_IDE_GATEWAY"], "172.17.0.1")

    def test_override_is_the_service_name(self):
        # The CLI dials whatever host this names; it must equal the relay compose
        # service so jail-internal DNS resolves it.
        with mock.patch.object(ide, "_host_gateway_ip", return_value=None):
            self.assertEqual(ide.environment()["CLAUDE_CODE_IDE_HOST_OVERRIDE"],
                             ide.SERVICE)


class IdePrepareCleanupTests(JailTestCase):
    def _home(self):
        return Path(self.tmpdir())

    @contextlib.contextmanager
    def _quiet_linux(self):
        with mock.patch.object(ide.sys, "platform", "linux"), \
                contextlib.redirect_stderr(io.StringIO()):
            yield

    def test_prepare_creates_dirs_and_returns_mirror(self):
        home = self._home()
        with self._quiet_linux():
            mirror = ide.prepare(home, "me")
        self.assertEqual(mirror, home / ".claude-jail-me" / "ide")
        self.assertTrue((home / ".claude" / "ide").is_dir())          # source
        self.assertTrue((home / ".claude-jail-me" / "ide").is_dir())  # dest

    def test_prepare_clears_stale_mirror_locks(self):
        home = self._home()
        mirror = home / ".claude-jail-me" / "ide"
        mirror.mkdir(parents=True)
        (mirror / "9999.lock").write_text("{}")
        with self._quiet_linux():
            ide.prepare(home, "me")
        self.assertEqual(list(mirror.glob("*.lock")), [])

    def test_prepare_spawns_no_host_process(self):
        # The bridge runs entirely in docker now; prepare() must not start a
        # host process via any spawn primitive.
        home = self._home()
        with self._quiet_linux(), \
                mock.patch.object(ide.subprocess, "run") as run, \
                mock.patch.object(ide.subprocess, "Popen") as popen:
            ide.prepare(home, "me")
        run.assert_not_called()
        popen.assert_not_called()

    def test_cleanup_clears_mirror(self):
        home = self._home()
        mirror = home / ".claude-jail-me" / "ide"
        mirror.mkdir(parents=True)
        (mirror / "1234.lock").write_text("{}")
        ide.cleanup(mirror)
        self.assertEqual(list(mirror.glob("*.lock")), [])

    def test_cleanup_missing_dir_is_safe(self):
        ide.cleanup(Path(self.tmpdir()) / "nope" / "ide")  # no raise

    def test_start_services_brings_up_the_profiled_services(self):
        calls = []

        def fake_run(c, *a, **k):
            calls.append(c)
            return ide.subprocess.CompletedProcess(c, 0)

        with mock.patch.object(ide.subprocess, "run", side_effect=fake_run):
            ide.start_services(["docker", "compose", "-f", "compose.yml"])
        # --build so an edited relay.py/forward.py isn't masked by a stale image.
        self.assertEqual(calls, [["docker", "compose", "-f", "compose.yml",
                                  "up", "-d", "--build", "ide-relay",
                                  "ide-host"]])

    def test_start_services_warns_on_failure(self):
        # A non-zero `up -d` must surface, not look healthy.
        result = ide.subprocess.CompletedProcess(["up"], 1)
        err = io.StringIO()
        with mock.patch.object(ide.subprocess, "run", return_value=result), \
                contextlib.redirect_stderr(err):
            ide.start_services(["docker", "compose"])
        self.assertIn("will not connect", err.getvalue())


class IdeProfileArgsTests(JailTestCase):
    def test_activates_the_ide_profile(self):
        # down must carry these so it reaps profiled ide-relay/ide-host; a plain
        # down skips inactive-profile services.
        self.assertEqual(ide.profile_args(), ["--profile", ide.PROFILE])


class IdeBridgeViableTests(JailTestCase):
    def test_linux_with_gateway_is_viable(self):
        with mock.patch.object(ide.sys, "platform", "linux"), \
                mock.patch.dict(ide.os.environ,
                                {"JAIL_IDE_GATEWAY": "172.17.0.1"}):
            self.assertTrue(ide.bridge_viable())

    def test_linux_without_gateway_is_not_viable(self):
        with mock.patch.object(ide.sys, "platform", "linux"), \
                mock.patch.dict(ide.os.environ, {"JAIL_IDE_GATEWAY": ""}):
            self.assertFalse(ide.bridge_viable())

    def test_non_linux_is_not_viable_even_with_gateway(self):
        # A docker bridge gateway can be read on Desktop too, but ide-host's
        # network_mode: host only shares the real host network on Linux.
        with mock.patch.object(ide.sys, "platform", "darwin"), \
                mock.patch.dict(ide.os.environ,
                                {"JAIL_IDE_GATEWAY": "172.17.0.1"}):
            self.assertFalse(ide.bridge_viable())


class IdeHostGatewayTests(JailTestCase):
    def _run(self, stdout, returncode=0):
        return mock.patch.object(
            ide.subprocess, "run",
            return_value=ide.subprocess.CompletedProcess([], returncode,
                                                         stdout=stdout))

    def test_first_ipv4_picked_from_dual_stack(self):
        # `println` prints one gateway per line; a dual-stack bridge lists IPv6
        # too. We must pick the IPv4, not concatenate them into garbage.
        with self._run("fe80::1\n172.17.0.1\n"):
            self.assertEqual(ide._host_gateway_ip(), "172.17.0.1")

    def test_nonzero_returncode_is_none(self):
        with self._run("172.17.0.1\n", returncode=1):
            self.assertIsNone(ide._host_gateway_ip())

    def test_no_gateway_is_none(self):
        with self._run("\n"):
            self.assertIsNone(ide._host_gateway_ip())
