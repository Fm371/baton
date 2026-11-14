"""env-limited results, services with health checks, the Codex network setting and the on_event hook."""

import json
import os
import socket
import sys
import unittest
from pathlib import Path

from baton import envlimit, notify, services
from baton.config import ConfigError, load_config
from baton.preflight import preflight
from baton.providers import CODEX_NETWORK_ARGS, build_command
from baton.report import ReportMixin
from baton.state import RunState, TaskState, save_state
from baton.runner import Runner
from baton.state import load_state
from tests.test_lanes import LanesFixture
from tests.test_runner import FakeClock, RunnerFixture, claude_limit, codex_limit, git

HERE = Path(__file__).resolve().parent
HOOK = HERE / "fake_hook.py"

TESTCLIENT_TRACEBACK = """Traceback (most recent call last):
  File "/usr/lib/python3.12/asyncio/selector_events.py", line 63, in __init__
    self._ssock, self._csock = socket.socketpair()
                               ^^^^^^^^^^^^^^^^^^
PermissionError: [Errno 1] Operation not permitted
"""


class DetectTest(unittest.TestCase):
    def test_a_socketpair_traceback(self):
        self.assertIn("socketpair", envlimit.detect(TESTCLIENT_TRACEBACK))

    def test_the_same_traceback_inside_a_json_event(self):
        event = json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": TESTCLIENT_TRACEBACK}})
        self.assertIn("socketpair", envlimit.detect(event))

    def test_a_blocked_connection(self):
        text = "httpx.ConnectError: connect to 127.0.0.1:5432 failed: [Errno 1] Operation not permitted"
        self.assertIn("connections", envlimit.detect(text))

    def test_a_permission_error_after_a_socket_call(self):
        text = "  File x.py, line 3\n    s = socket.socket(socket.AF_INET)\nPermissionError: [Errno 1] Operation not permitted"
        self.assertIn("sockets", envlimit.detect(text))

    def test_a_bwrap_failure(self):
        self.assertIn("bwrap", envlimit.detect("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"))

    def test_ordinary_failures_are_not_env_limited(self):
        for text in ("FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2",
                     "PermissionError: [Errno 13] Permission denied: '/etc/shadow'",
                     "chmod: changing permissions of 'x': Operation not permitted",
                     "", None):
            self.assertIsNone(envlimit.detect(text), text)


class StatusLabelTest(unittest.TestCase):
    def test_labels(self):
        label = ReportMixin.status_label
        self.assertEqual(label(TaskState(status="blocked", reason="env")), "blocked:env")
        self.assertEqual(label(TaskState(status="blocked", reason="finding")), "blocked:finding")
        self.assertEqual(label(TaskState(status="blocked")), "blocked")
        self.assertEqual(label(TaskState(status="partial", reason="env")), "partial (env-limited)")
        self.assertEqual(label(TaskState(status="done", reason="env")), "done")


class EnvLimitedRunTest(RunnerFixture):
    def summary(self):
        return (self.run_dir / "results" / "SUMMARY.md").read_text()

    def test_a_partial_caused_by_the_sandbox_is_marked_env_limited(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial", "stderr": TESTCLIENT_TRACEBACK}])
        self.run_baton()
        ts = self.state.tasks["a"]
        self.assertEqual((ts.status, ts.reason), ("partial", "env"))
        self.assertTrue(ts.note.startswith("env-limited: claude: the sandbox forbids socketpair"), ts.note)
        self.assertIn("| a | repo | partial (env-limited) |", self.summary())
        status = json.loads((self.run_dir / "results" / "a" / "status.json").read_text())
        self.assertEqual((status["status"], status["reason"]), ("partial", "env"))
        self.assertIn("environment-limited, not a code failure", self.log)

    def test_a_plain_partial_is_not(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial", "stderr": "FAILED test_x - AssertionError"}])
        self.run_baton()
        self.assertEqual((self.state.tasks["a"].status, self.state.tasks["a"].reason), ("partial", ""))
        self.assertIn("| a | repo | partial |", self.summary())

    def test_a_blocked_finding_is_told_apart(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "blocked"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["a"].reason, "finding")
        self.assertIn("| a | repo | blocked:finding |", self.summary())

    def test_a_blocked_by_the_sandbox_is_blocked_env(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "blocked", "stderr": TESTCLIENT_TRACEBACK}])
        self.run_baton()
        self.assertIn("| a | repo | blocked:env |", self.summary())

    def test_a_done_task_is_never_marked(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "stderr": TESTCLIENT_TRACEBACK}])
        self.run_baton()
        self.assertEqual((self.state.tasks["a"].status, self.state.tasks["a"].reason), ("done", ""))
        self.assertNotIn("env-limited", self.state.tasks["a"].note)

    def test_by_default_there_is_no_retry(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial", "stderr": TESTCLIENT_TRACEBACK}],
                         codex=[{"dirty": "b.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(len(self.calls()), 1)

    def test_retry_other_provider_runs_the_task_once_more_elsewhere(self):
        self.write_config(env_limited_action="retry_other_provider")
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial", "stderr": TESTCLIENT_TRACEBACK}],
                         codex=[{"dirty": "b.txt", "status": "done", "commit_message": "feat: b"}])
        self.run_baton()
        self.assertEqual([c["provider"] for c in self.calls()], ["claude", "codex"])
        ts = self.state.tasks["a"]
        self.assertEqual((ts.status, ts.reason, ts.providers), ("done", "", ["claude", "codex"]))
        self.assertIn("retrying once on codex", self.log)
        self.assertIn("blocked part of its checks", self.calls()[1]["prompt"])

    def test_the_retry_is_spent_once(self):
        self.write_config(env_limited_action="retry_other_provider")
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial", "stderr": TESTCLIENT_TRACEBACK}],
                         codex=[{"dirty": "b.txt", "status": "partial", "stderr": TESTCLIENT_TRACEBACK,
                                 "commit_message": "feat: b"}])
        self.run_baton()
        self.assertEqual(len(self.calls()), 2)
        ts = self.state.tasks["a"]
        self.assertEqual((ts.status, ts.reason), ("partial", "env"))

    def test_a_task_limited_to_one_provider_is_not_moved(self):
        self.write_config(env_limited_action="retry_other_provider")
        self.add_task("a", extra='providers = ["claude"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial", "stderr": TESTCLIENT_TRACEBACK}])
        self.run_baton()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.state.tasks["a"].reason, "env")


class ConfigTest(RunnerFixture):
    def load(self, extra="", **run):
        self.write_config(extra=extra, **run)
        return load_config(self.run_dir / "baton.toml")

    def test_defaults(self):
        cfg = self.load()
        self.assertEqual((cfg.env_limited_action, cfg.services, cfg.on_event, cfg.services_for_all_tasks),
                         ("note", {}, None, False))
        self.assertFalse(cfg.providers["codex"].network)

    def test_services(self):
        cfg = self.load('[services.mongo]\ncheck = "mongosh --eval 1"\nrestart = ["docker", "start", "m"]\n'
                        'endpoint = "127.0.0.1:27017"\ntimeout_s = 5\n')
        svc = cfg.services["mongo"]
        self.assertEqual((svc.check, svc.restart, svc.endpoint, svc.timeout_s),
                         ("mongosh --eval 1", ["docker", "start", "m"], "127.0.0.1:27017", 5.0))

    def test_service_errors(self):
        for extra, needle in (
                ('[services.mongo]\nrestart = "x"\n', "needs a `check`"),
                ('[services.mongo]\ncheck = 5\n', "services.mongo.check must be a command"),
                ('[services.mongo]\ncheck = "x"\nchek = "y"\n', "did you mean"),
                ('[services."bad name"]\ncheck = "x"\n', "may only contain"),
                ('[services.mongo]\ncheck = "x"\ntimeout_s = 0\n', "timeout_s"),
        ):
            with self.assertRaises(ConfigError, msg=extra) as cm:
                self.load(extra)
            self.assertIn(needle, str(cm.exception))

    def test_env_limited_action_is_checked(self):
        with self.assertRaises(ConfigError) as cm:
            self.load(env_limited_action="sometimes")
        self.assertIn("env_limited_action", str(cm.exception))

    def test_on_event(self):
        self.assertEqual(self.load(on_event="ntfy publish 'my topic'").on_event, ["ntfy", "publish", "my topic"])
        self.assertEqual(self.load(on_event=["curl", "-d", "x"]).on_event, ["curl", "-d", "x"])
        for bad in ("", [], 5, "echo 'unclosed"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.load(on_event=bad)

    def test_network_belongs_to_codex(self):
        cfg = self.load()
        path = self.run_dir / "baton.toml"
        path.write_text(path.read_text().replace('[providers.codex]\n', '[providers.codex]\nnetwork = true\n'))
        self.assertTrue(load_config(path).providers["codex"].network)
        path.write_text(path.read_text().replace('[providers.claude]\n', '[providers.claude]\nnetwork = true\n'))
        with self.assertRaises(ConfigError) as cm:
            load_config(path)
        self.assertIn("only applies to codex", str(cm.exception))
        path.write_text(path.read_text().replace("network = true", 'network = "yes"'))
        with self.assertRaises(ConfigError):
            load_config(path)
        del cfg

    def test_the_network_flag_reaches_the_codex_command(self):
        cfg = self.load()
        pc = cfg.providers["codex"]
        spec = cfg.routing.spec("r3", "codex")
        self.assertNotIn(CODEX_NETWORK_ARGS[1], build_command(pc, spec, None)[0])
        pc.network = True
        argv = build_command(pc, spec, None)[0]
        i = argv.index(CODEX_NETWORK_ARGS[1])
        self.assertEqual(argv[i - 1], "-c")
        self.assertEqual(argv[-1], "-")        # the prompt still comes from stdin

    def test_services_tcp(self):
        path = self.run_dir / "baton.toml"
        self.write_config()
        path.write_text(path.read_text() + '\n[preflight]\nservices_tcp = ["127.0.0.1:5432"]\n')
        self.assertEqual(load_config(path).preflight.services_tcp, ["127.0.0.1:5432"])
        path.write_text(path.read_text().replace("127.0.0.1:5432", "localhost"))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_a_task_cannot_need_an_undeclared_service(self):
        self.add_task("a", extra='needs = ["mongo"]\n')
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, _ = preflight(cfg)
        self.assertTrue(any("needs 'mongo'" in e and "not a [services.*] entry" in e for e in errors), errors)


class ServicesRunTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.up = self.root / "mongo.up"
        self.kill = self.root / "mongo.killed"

    def declare(self, restart=True, check=None, **run):
        extra = (f'[services.mongo]\ncheck = "{check or f"test -f {self.up}"}"\nendpoint = "127.0.0.1:27017"\n'
                 f'wait_s = 0\n' + (f'restart = "touch {self.up}"\n' if restart else ""))
        self.write_config(extra=extra, **run)

    def test_a_healthy_service_is_named_in_the_prompt(self):
        self.up.write_text("")
        self.declare()
        self.add_task("a", extra='needs = ["mongo"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        prompt = self.calls()[0]["prompt"]
        self.assertIn("mongo at 127.0.0.1:27017", prompt)
        self.assertIn("do not start other instances", prompt)

    def test_a_task_without_needs_hears_nothing_about_services(self):
        self.up.write_text("")
        self.declare()
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertNotIn("Services available", self.calls()[0]["prompt"])

    def test_a_dead_service_is_restarted_once(self):
        self.declare()
        self.add_task("a", extra='needs = ["mongo"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertTrue(self.up.exists())
        self.assertIn("service mongo is down", self.log)
        self.assertIn("healthy again after a restart", self.log)
        self.assertEqual(self.state.tasks["a"].status, "done")

    def run_raw(self):
        """Like run_baton, for runs where no attempt may happen (no metrics file to read)."""
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        self.clock = FakeClock()
        rc = Runner(cfg, clock=self.clock, echo=False).run(tasks)
        self.state = load_state(cfg.state_dir / "run.json")
        self.log = (cfg.logs_dir / "run.log").read_text()
        return rc

    def dies_after_a(self, **kw):
        """Healthy at plan time; task a (which needs nothing) is what takes it down."""
        self.declare(check=f"test ! -f {self.kill}", **kw)
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "touch": str(self.kill)},
                                 {"commit": "b.txt", "status": "done"}])

    def test_a_service_that_stays_down_stops_the_run_before_any_attempt(self):
        self.dies_after_a(restart=False)
        self.add_task("b", extra='needs = ["mongo"]\n')
        self.add_task("c")
        self.assertEqual(self.run_raw(), 3)
        self.assertEqual([c["cwd"] for c in self.calls()].count(str(self.repo)), 1)     # only a ran
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("task b: service 'mongo' is down", self.log)
        self.assertIn("the run stops here so no attempt is spent on it", self.log)
        self.assertNotIn(self.state.tasks["b"].status, ("failed", "blocked", "done", "partial"))
        self.assertEqual(self.state.tasks["b"].attempts, 0)
        self.assertIn("waiting for the environment", self.state.tasks["b"].note)
        self.assertNotIn("c", [t for t, s in self.state.tasks.items() if s.attempts])

    def test_a_restart_that_does_not_help(self):
        self.dies_after_a(restart=False)
        path = self.run_dir / "baton.toml"
        path.write_text(path.read_text().replace("wait_s = 0\n", 'wait_s = 0\nrestart = "false"\n'))
        self.add_task("b", extra='needs = ["mongo"]\n')
        self.assertEqual(self.run_raw(), 3)
        self.assertIn("even after a restart", self.log)
        self.assertEqual(self.state.tasks["b"].attempts, 0)

    def test_services_for_all_tasks(self):
        self.dies_after_a(restart=False, services_for_all_tasks=True)
        self.add_task("b")
        self.assertEqual(self.run_raw(), 3)
        self.assertEqual(self.state.tasks["a"].status, "done")      # the check passed before a started
        self.assertEqual(self.state.tasks["b"].attempts, 0)

    def test_a_verifier_blocked_while_the_service_died_is_blocked_env(self):
        # healthy before the verifier starts, gone when it ends: its verdict is about the database
        self.declare(restart=False, check=f"test ! -f {self.kill}")
        self.add_task("a")
        self.add_verifier("v", deps=("a",), extra='needs = ["mongo"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "tests/test_v.py", "status": "blocked", "touch": str(self.kill)}])
        self.run_baton()
        ts = self.state.tasks["v"]
        self.assertEqual((ts.status, ts.reason), ("blocked", "env"))
        self.assertIn("env-limited: service 'mongo' is down", ts.note)
        self.assertIn("| v (verify) | repo | blocked:env |", (self.run_dir / "results" / "SUMMARY.md").read_text())

    def test_plan_reports_a_service_that_is_down(self):
        self.declare(restart=False)
        self.add_task("a", extra='needs = ["mongo"]\n')
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, _ = preflight(cfg)
        self.assertTrue(any("service 'mongo' is down" in e and "no `restart`" in e for e in errors), errors)

    def test_plan_with_a_restart_only_warns(self):
        self.declare()
        self.add_task("a", extra='needs = ["mongo"]\n')
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, _ = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertTrue(any("will run its `restart` once" in w for w in warnings), warnings)

    def test_an_unused_service_is_pointed_out(self):
        self.up.write_text("")
        self.declare()
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        _, warnings, _ = preflight(cfg)
        self.assertTrue(any("declared but no task needs it" in w for w in warnings), warnings)


class TcpProbeTest(RunnerFixture):
    def listener(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return f"127.0.0.1:{s.getsockname()[1]}"

    def closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return f"127.0.0.1:{port}"

    def configure(self, addr, network=False):
        self.write_config()
        path = self.run_dir / "baton.toml"
        text = path.read_text()
        if network:
            text = text.replace('[providers.codex]\n', '[providers.codex]\nnetwork = true\n')
        path.write_text(text + f'\n[preflight]\nservices_tcp = ["{addr}"]\n')
        self.add_task("a")
        return load_config(path)

    def test_an_endpoint_that_is_down_fails_plan(self):
        errors, _, _ = preflight(self.configure(self.closed_port()))
        self.assertTrue(any("is not reachable from this machine" in e for e in errors), errors)

    def test_a_sandbox_that_cannot_reach_it_fails_plan_with_the_fix(self):
        os.environ["BATON_FAKE_SANDBOX"] = "net-blocked"
        self.addCleanup(os.environ.pop, "BATON_FAKE_SANDBOX", None)
        errors, _, _ = preflight(self.configure(self.listener()))
        msg = next(e for e in errors if "codex's sandbox cannot reach" in e)
        self.assertIn("[providers.codex] network = true", msg)

    def test_still_blocked_with_network_on_says_so(self):
        os.environ["BATON_FAKE_SANDBOX"] = "net-blocked"
        self.addCleanup(os.environ.pop, "BATON_FAKE_SANDBOX", None)
        errors, _, _ = preflight(self.configure(self.listener(), network=True))
        self.assertTrue(any("even with providers.codex.network = true" in e for e in errors), errors)

    def test_a_sandbox_that_can_reach_it_is_quiet(self):
        os.environ["BATON_FAKE_SANDBOX"] = "net-ok"
        self.addCleanup(os.environ.pop, "BATON_FAKE_SANDBOX", None)
        errors, warnings, _ = preflight(self.configure(self.listener()))
        self.assertEqual(errors, [])
        self.assertFalse(any("sandbox" in w and "services_tcp" in w for w in warnings), warnings)

    def test_a_probe_that_gives_no_answer_only_warns(self):
        errors, warnings, _ = preflight(self.configure(self.listener()))       # the fake prints nothing
        self.assertEqual(errors, [])
        self.assertTrue(any("could not check" in w and "inside codex's sandbox" in w for w in warnings), warnings)

    def test_the_probe_command_carries_the_network_flag(self):
        reached, _ = services.sandbox_can_connect(["env"], self.listener())
        self.assertTrue(reached)
        blocked, why = services.sandbox_can_connect(["env"], self.closed_port())
        self.assertFalse(blocked)
        self.assertTrue(why)


class OnEventTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.out = self.root / "events.jsonl"

    def hook(self, *extra, **run):
        self.write_config(on_event=[sys.executable, str(HOOK), str(self.out), *extra], **run)

    def events(self):
        return [json.loads(x) for x in self.out.read_text().splitlines()] if self.out.exists() else []

    def test_task_and_run_events_carry_what_happened(self):
        self.hook()
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "partial"}])
        self.assertEqual(self.run_baton(), 0)
        events = self.events()
        by_event = {e["BATON_EVENT"]: e for e in events}
        self.assertEqual([e["BATON_EVENT"] for e in events if e["BATON_TASK"]], ["task_done", "task_partial"])
        self.assertEqual((by_event["task_done"]["BATON_TASK"], by_event["task_done"]["BATON_STATUS"]), ("a", "done"))
        self.assertEqual(by_event["task_partial"]["BATON_TASK"], "b")
        end = by_event["run_end"]
        self.assertEqual((end["BATON_TASK"], end["BATON_STATUS"]), ("", ""))
        self.assertIn("2 tasks", end["BATON_NOTE"])
        self.assertTrue(all(e["BATON_RUN_ID"] == self.metrics[0]["run"] for e in events))

    def test_the_note_is_one_line(self):
        self.assertEqual(notify.one_line("a\n\n  b\tc"), "a b c")
        self.assertEqual(len(notify.one_line("x" * 2000)), notify.NOTE_MAX)

    def test_a_failing_hook_is_logged_and_changes_nothing(self):
        self.hook("fail")
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "done")
        self.assertIn("the hook exited 1", self.log)

    def test_a_missing_hook_is_logged_and_changes_nothing(self):
        self.write_config(on_event=[str(self.root / "no-such-binary")])
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIn("could not run the hook", self.log)

    def test_a_hook_that_hangs_is_stopped(self):
        self.hook("hang", on_event_timeout_seconds=0.5)
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertIn("did not finish in 0.5s and was stopped", self.log)

    def test_the_hook_is_not_run_through_a_shell(self):
        # shell syntax in the command is passed as plain arguments
        self.write_config(on_event=f"{sys.executable} {HOOK} {self.out} ';' touch {self.root / 'pwned'}")
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertTrue(self.out.exists())
        self.assertFalse((self.root / "pwned").exists())

    def test_a_long_sleep_is_an_event(self):
        self.hook(on_event_sleep_min=1)
        self.add_task("a")
        self.scenario_is(claude=[claude_limit("11:59pm (Asia/Tehran)")],
                         codex=[codex_limit("10:45 PM"), {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        sleeps = [e for e in self.events() if e["BATON_EVENT"] == "sleep"]
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0]["BATON_TASK"], "a")
        self.assertIn("sleeping until", sleeps[0]["BATON_NOTE"])

    def test_a_short_sleep_is_not(self):
        self.hook(on_event_sleep_min=100000)
        self.add_task("a")
        self.scenario_is(claude=[claude_limit("11:59pm (Asia/Tehran)")],
                         codex=[codex_limit("10:45 PM"), {"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertFalse([e for e in self.events() if e["BATON_EVENT"] == "sleep"])

    def test_a_stopped_run_is_an_event(self):
        kill = self.root / "killed"
        self.write_config(extra=f'[services.mongo]\ncheck = "test ! -f {kill}"\n',
                          on_event=[sys.executable, str(HOOK), str(self.out)])
        self.add_task("a")
        self.add_task("b", extra='needs = ["mongo"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "touch": str(kill)}])
        self.assertEqual(self.run_baton(), 3)
        stopped = [e for e in self.events() if e["BATON_EVENT"] == "run_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertIn("service 'mongo' is down", stopped[0]["BATON_NOTE"])

    def test_baton_stop_ends_with_run_end(self):
        self.hook()
        self.add_task("a")
        self.add_task("b", deps=["a"])
        (self.run_dir / "state").mkdir()
        stop = self.run_dir / "state" / "stop"
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "touch": str(stop)},
                                 {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        ends = [e for e in self.events() if e["BATON_EVENT"] == "run_end"]
        self.assertEqual(len(ends), 1)
        self.assertIn("stop requested", ends[0]["BATON_NOTE"])
        self.assertFalse([e for e in self.events() if e["BATON_EVENT"] == "run_stopped"])

    def test_work_left_mid_way_by_a_dead_run_is_reported(self):
        self.hook()
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        st = RunState()
        st.tasks["a"] = TaskState(status="running", attempts=1, start_head=git(self.repo, "rev-parse", "HEAD"))
        save_state(cfg.state_dir / "run.json", st)
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        died = [e for e in self.events() if e["BATON_EVENT"] == "run_died"]
        self.assertEqual(len(died), 1)
        self.assertIn("a", died[0]["BATON_NOTE"])
        self.assertIn("previous run ended without finishing", died[0]["BATON_NOTE"])

    def test_no_hook_no_events(self):
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertFalse(self.out.exists())


class ServicesLanesTest(LanesFixture):
    def test_a_service_that_dies_stops_the_lanes_run_instead_of_looping(self):
        kill = self.root / "killed"
        self.write_config(lanes=2, extra=f'[services.mongo]\ncheck = "test ! -f {kill}"\n')
        self.lane_task("a", "repo", providers=["claude"])
        self.lane_task("b", "repo2", deps=["a"], providers=["claude"])
        path = self.run_dir / "tasks" / "b.md"
        path.write_text(path.read_text().replace('providers = ["claude"]', 'providers = ["claude"]\nneeds = ["mongo"]'))
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done", "touch": str(kill)},
                                 {"commit": "b.txt", "status": "done"}])
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        rc = Runner(cfg, clock=FakeClock(), echo=False).run(tasks)
        state = load_state(cfg.state_dir / "run.json")
        self.assertEqual(rc, 3)
        self.assertEqual(state.tasks["a"].status, "done")
        self.assertEqual(state.tasks["b"].attempts, 0)
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
