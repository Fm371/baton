"""The Codex sandbox probe for socketpair and localhost TCP: reports only what the sandbox adds."""

from unittest import mock

from baton import sandboxprobe
from baton.config import load_config
from baton.preflight import preflight
from tests.test_runner import RunnerFixture

OK = {"socketpair": "ok", "tcp": "ok"}
BLOCKED = {"socketpair": "PermissionError: [Errno 1] Operation not permitted", "tcp": "ok"}


def fake_runner(host, inside):
    """A runner that answers `host` for the direct call and `inside` for the one through the sandbox."""
    def run(argv, env):
        return inside if "sandbox" in argv else host
    return run


class SandboxProbeTest(RunnerFixture):
    def codex(self):
        self.write_config()
        return load_config(self.run_dir / "baton.toml").providers["codex"]

    def test_a_restriction_the_host_does_not_have_is_reported_with_the_fix(self):
        warnings = sandboxprobe.check(self.codex(), fake_runner(OK, BLOCKED))
        self.assertEqual(len(warnings), 1)
        self.assertIn("socketpair", warnings[0])
        self.assertIn("[providers.codex] network = true", warnings[0])
        self.assertIn("opens the whole internet", warnings[0])

    def test_nothing_is_reported_when_both_sides_agree(self):
        self.assertEqual(sandboxprobe.check(self.codex(), fake_runner(OK, OK)), [])
        self.assertEqual(sandboxprobe.check(self.codex(), fake_runner(BLOCKED, BLOCKED)), [])

    def test_an_inconclusive_probe_says_nothing(self):
        self.assertEqual(sandboxprobe.check(self.codex(), fake_runner(OK, None)), [])
        self.assertEqual(sandboxprobe.check(self.codex(), fake_runner(None, BLOCKED)), [])

    def test_tcp_alone_is_reported_too(self):
        inside = {"socketpair": "ok", "tcp": "ConnectionRefusedError: [Errno 111] refused"}
        warnings = sandboxprobe.check(self.codex(), fake_runner(OK, inside))
        self.assertIn("tcp", warnings[0])

    def test_no_sandbox_or_a_turned_off_probe_skips_it(self):
        pc = self.codex()
        pc.sandbox = "danger-full-access"
        self.assertEqual(sandboxprobe.check(pc, fake_runner(OK, BLOCKED)), [])
        pc.sandbox = "workspace-write"
        pc.sandbox_probe = []
        self.assertEqual(sandboxprobe.check(pc, fake_runner(OK, BLOCKED)), [])

    def test_when_network_is_already_on_the_hint_changes(self):
        pc = self.codex()
        pc.network = True
        warnings = sandboxprobe.check(pc, fake_runner(OK, BLOCKED))
        self.assertIn("already set", warnings[0])

    def test_a_custom_probe_without_a_double_dash_cannot_be_used(self):
        pc = self.codex()
        pc.sandbox_probe = ["sandbox", "true"]
        self.assertEqual(sandboxprobe.check(pc, fake_runner(OK, BLOCKED)), [])

    def test_plan_prints_the_warning(self):
        self.write_config()
        self.add_task("a")
        with mock.patch("baton.sandboxprobe._run", side_effect=fake_runner(OK, BLOCKED)):
            _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertTrue(any("sandbox refuses" in w and "network = true" in w for w in warnings), warnings)

    def test_plan_with_the_fake_codex_is_quiet(self):
        self.write_config()
        self.add_task("a")
        _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertFalse([w for w in warnings if "sandbox refuses" in w], warnings)

    def test_run_script_parses_a_real_result(self):
        # the probe script itself, run directly on this machine
        result = sandboxprobe._run([__import__("sys").executable, "-c", sandboxprobe.SCRIPT], {})
        self.assertIsNotNone(result)
        self.assertEqual(result["socketpair"], "ok")


class ProbeUsesProviderSettings(SandboxProbeTest):
    def test_probe_command_carries_the_providers_sandbox_and_network(self):
        pc = self.codex()
        pc.network = True
        pc.sandbox = "workspace-write"
        prefix = sandboxprobe.sandbox_prefix(pc, "/bin/codex")
        self.assertEqual(prefix[-1], "--")
        self.assertIn("sandbox_workspace_write.network_access=true", prefix)
        self.assertIn('sandbox_mode="workspace-write"', prefix)

    def test_no_network_flag_when_not_configured(self):
        pc = self.codex()
        pc.network = False
        self.assertNotIn("sandbox_workspace_write.network_access=true",
                         sandboxprobe.sandbox_prefix(pc, "/bin/codex"))
