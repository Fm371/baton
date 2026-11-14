"""browser-smoke: the verdicts (with a fake driver) and, when Node and
Playwright are available, the real driver against a small local site."""

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from baton.checks import CheckContext, run_template
from baton.config import load_config
from baton.tasks import load_tasks
from tests.test_runner import RunnerFixture

HERE = Path(__file__).parent
FAKE_DRIVER = HERE / "fake_browser_driver.py"

ROUTES = """
[[params.routes]]
path = "/join"
logged_out_redirect = "/login"
reuse = "/invitations"
expect_endpoints = ["/api/plans"]
[params.routes.invalid_submit]
steps = [{ fill = "#email", value = "x" }, { click = "button" }]
message_selector = "[role=alert]"
expect_field = "email"
"""


def toml_value(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(v)


class BrowserSmokeTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.state_file = self.root / "qa" / "state.json"
        self.state_file.parent.mkdir()
        self.state_file.write_text("{}")
        self.fake = self.root / "fake-browser.json"
        os.environ["BATON_FAKE_BROWSER"] = str(self.fake)
        self.addCleanup(os.environ.pop, "BATON_FAKE_BROWSER", None)
        self.configure()

    def configure(self, storage=True, **over):
        opts = {"driver_cmd": [sys.executable, str(FAKE_DRIVER)], "base_url": "http://127.0.0.1:3000",
                "api_origins": ["http://127.0.0.1:8000"], "spinner_selector": ".spinner",
                **({"storage_state": str(self.state_file)} if storage else {}), **over}
        opts = {k: v for k, v in opts.items() if v is not None}
        toml = "\n".join(f"{k} = {toml_value(v)}" for k, v in opts.items())
        self.write_config(extra=f"[browser_smoke]\n{toml}\n")

    def fake_is(self, **per_job):
        self.fake.write_text(json.dumps(per_job))

    def verifier(self, routes=ROUTES, deps=()):
        (self.run_dir / "verifiers").mkdir(exist_ok=True)
        # the routes are TOML, so they belong in the front matter
        (self.run_dir / "verifiers" / "smoke.md").write_text(
            f'+++\nkind = "verify"\ntemplate = "browser-smoke"\ndepends_on = {json.dumps(list(deps))}\n'
            f'{routes}+++\n')

    def check(self, routes=ROUTES):
        self.verifier(routes)
        cfg = load_config(self.run_dir / "baton.toml")
        task = next(t for t in load_tasks(cfg) if t.id == "smoke")
        return cfg, run_template(CheckContext(cfg, task, cfg.results_dir / "smoke"))

    LOGGED_OUT = "/join [logged_out]"
    LOGGED_IN = "/join [logged_in]"

    def healthy(self):
        return {self.LOGGED_OUT: {"final_url": "http://127.0.0.1:3000/login"}}

    def test_healthy_route_passes_and_saves_screenshots(self):
        self.fake_is(**self.healthy())
        cfg, res = self.check()
        self.assertEqual(res.gaps, [])
        self.assertEqual(res.status, "done")
        names = sorted(p.name for p in res.files)
        self.assertEqual(names, ["00-join-logged_in.png", "00-join-logged_out.png", "00-reuse-invitations.png"])
        report = res.markdown(next(t for t in load_tasks(cfg) if t.id == "smoke"), cfg.results_dir)
        self.assertIn("![00-reuse-invitations.png](smoke/00-reuse-invitations.png)", report)

    def gaps_with(self, **per_job):
        self.fake_is(**{**self.healthy(), **per_job})
        return self.check()[1].gaps

    def test_an_endless_spinner_is_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"spinner_stuck": True}})
        self.assertEqual(gaps, ["/join [logged_in]: endless spinner: still loading after 10s"])

    def test_console_errors_are_a_gap_but_failed_resource_logs_are_not(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"console_errors": [
            "TypeError: x is undefined", "Failed to load resource: the server responded with a status of 401"]}})
        self.assertEqual(len(gaps), 1)
        self.assertIn("1 console error(s): TypeError: x is undefined", gaps[0])

    def test_api_calls_to_the_wrong_host_are_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"requests": [
            {"url": "http://127.0.0.1:3000/v1/plans", "method": "GET", "status": 404,
             "resource_type": "fetch"}]}})
        self.assertTrue(any("goes to http://127.0.0.1:3000, expected http://127.0.0.1:8000" in g for g in gaps), gaps)
        self.assertTrue(any("expected a call to /api/plans; none was made" in g for g in gaps), gaps)

    def test_server_errors_from_the_api_are_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"requests": [
            {"url": "http://127.0.0.1:8000/api/plans", "method": "POST", "status": 500, "resource_type": "fetch"}]}})
        self.assertEqual(gaps, ["/join [logged_in]: API call POST http://127.0.0.1:8000/api/plans -> 500"])

    def test_an_unexpected_redirect_is_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"final_url": "http://127.0.0.1:3000/login"}})
        self.assertEqual(gaps, ["/join [logged_in]: unexpected redirect to http://127.0.0.1:3000/login"])

    def test_a_declared_redirect_that_does_not_happen_is_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_OUT: {"final_url": "http://127.0.0.1:3000/join"}})
        self.assertEqual(gaps, ["/join [logged_out]: expected a redirect to /login, ended at "
                                "http://127.0.0.1:3000/join"])

    def test_a_generic_submit_error_is_a_gap_and_a_specific_one_passes(self):
        bad = {"message": "Something went wrong, try again", "status": 422, "error": None}
        gaps = self.gaps_with(**{self.LOGGED_IN: {"invalid_submit": bad}})
        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(g.startswith("/join [logged_in]: invalid submit: a 422 must say") for g in gaps), gaps)
        self.assertEqual(self.gaps_with(), [])

    def test_a_submit_that_could_not_run_is_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"invalid_submit": {"error": "no element #email"}}})
        self.assertEqual(gaps, ["/join [logged_in]: the invalid submit failed: no element #email"])

    def test_a_page_that_did_not_load_is_a_gap(self):
        gaps = self.gaps_with(**{self.LOGGED_IN: {"error": "net::ERR_CONNECTION_REFUSED"}})
        self.assertIn("could not load the page: net::ERR_CONNECTION_REFUSED", gaps[0])

    def test_logged_in_needs_a_test_session(self):
        self.configure(storage=False)
        self.fake_is(**self.healthy())
        gaps = self.check()[1].gaps
        self.assertEqual(len(gaps), 1)
        self.assertIn("logged_in needs a test session", gaps[0])
        self.configure(storage_state=str(self.root / "missing.json"))
        gaps = self.check()[1].gaps
        self.assertTrue(any("does not exist: record a test session first" in g for g in gaps), gaps)

    def test_a_driver_that_fails_blocks_with_its_message(self):
        self.fake_is(__exit__=3)
        [gap] = self.check()[1].gaps
        self.assertIn("the browser driver failed (exit 3)", gap)
        self.assertIn("playwright is not installed", gap)

    def test_unset_api_origins_is_reported_as_not_checked(self):
        self.configure(api_origins=[])
        self.fake_is(**self.healthy())
        res = self.check()[1]
        self.assertEqual(res.gaps, [])
        self.assertIn("API host (browser_smoke.api_origins is not set)", res.skipped)

    def test_servers_are_started_for_the_run(self):
        from tests.test_checks_common import free_port
        port = free_port()
        site = self.root / "site"
        site.mkdir()
        self.configure(base_url=None, servers=[{
            "name": "site", "cwd": str(site), "url": f"http://127.0.0.1:{port}/",
            "cmd": f"{sys.executable} -m http.server {port} --bind 127.0.0.1"}])
        self.fake_is(**{"/join [logged_out]": {"final_url": f"http://127.0.0.1:{port}/login"},
                        "/join [logged_in]": {"final_url": f"http://127.0.0.1:{port}/join"}})
        res = self.check()[1]
        self.assertTrue(any("started" in c for c in res.checked), res.checked)

    def test_a_failing_smoke_gates_the_tasks_after_it(self):
        self.add_task("a")
        self.verifier(deps=["a"])
        self.add_task("b", deps=["smoke"])
        self.fake_is(**self.healthy(), **{self.LOGGED_IN: {"spinner_stuck": True}})
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual((st["smoke"].status, st["b"].status, st["b"].gated_by), ("blocked", "gated", "smoke"))
        self.assertEqual(len(self.calls()), 1)
        report = (self.run_dir / "results" / "smoke.md").read_text()
        self.assertIn("endless spinner", report)
        self.assertTrue((self.run_dir / "results" / "smoke" / "00-join-logged_in.png").is_file())

    def test_a_passing_smoke_releases_the_tasks_after_it(self):
        self.add_task("a")
        self.verifier(deps=["a"])
        self.add_task("b", deps=["smoke"])
        self.fake_is(**self.healthy())
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["b"].status, "done")


class SmokeWarningTest(RunnerFixture):
    def test_plan_warns_about_a_ui_task_no_browser_smoke_follows(self):
        from baton.preflight import preflight
        (self.run_dir / "tasks" / "ui.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\n+++\nBuild src/app/join/page.tsx.\n\n## Reuse\nThe invitations page.\n')
        _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        warnings = [w for w in warnings if "run.timezone" not in w]    # the fixture's zone is not this machine's
        self.assertEqual(len(warnings), 1)
        self.assertIn("no browser-smoke verifier depends on it", warnings[0])
        (self.run_dir / "verifiers").mkdir()
        (self.run_dir / "verifiers" / "smoke.md").write_text(
            '+++\nkind = "verify"\ntemplate = "browser-smoke"\ndepends_on = ["ui"]\n+++\n')
        _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        self.assertEqual([w for w in warnings if "run.timezone" not in w], [])


# ---------------------------------------------------------------- the real driver

NODE_PATH = os.environ.get("NODE_PATH") or "/opt/node-tools/node_modules"


def playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    p = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True,
                       env={**os.environ, "NODE_PATH": NODE_PATH})
    return p.returncode == 0


PAGES = {
    "/ok": "<html><body><h1>Plans</h1><script>fetch('/api/plans')</script></body></html>",
    "/stuck": "<html><body><div class='spinner'>loading</div></body></html>",
    "/jump": "<html><body><script>location.replace('/login')</script></body></html>",
    "/login": "<html><body>login</body></html>",
    "/broken": "<html><body><script>console.error('boom from the page')</script></body></html>",
    "/invitations": "<html><body><h1>Invitations</h1></body></html>",
    "/form": ("<html><body><input id='email'><button onclick=\"fetch('/api/submit',{method:'POST'})"
              ".then(()=>{document.getElementById('msg').textContent='Something went wrong, try again'})\">Go</button>"
              "<div id='msg' role='alert'></div></body></html>"),
}


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/plans":
            return self.send(200, "application/json", '{"plans": []}')
        if path in PAGES:
            return self.send(200, "text/html", PAGES[path])
        self.send(404, "text/plain", "not found")

    def do_POST(self):
        self.send(422, "application/json", '{"detail": "bad"}')

    def send(self, code, kind, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipUnless(playwright_available(), "Node with Playwright is not available")
class RealDriverTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=self.site.serve_forever, daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.base = f"http://127.0.0.1:{self.site.server_address[1]}"
        patcher = mock.patch.dict(os.environ, {"NODE_PATH": NODE_PATH})
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, routes):
        self.write_config(extra=f'[browser_smoke]\nbase_url = "{self.base}"\napi_origins = ["{self.base}"]\n'
                                'spinner_selector = ".spinner"\nspinner_timeout_s = 2\n')
        (self.run_dir / "verifiers").mkdir(exist_ok=True)
        (self.run_dir / "verifiers" / "smoke.md").write_text(
            '+++\nkind = "verify"\ntemplate = "browser-smoke"\n' + routes + '+++\n')
        cfg = load_config(self.run_dir / "baton.toml")
        task = next(t for t in load_tasks(cfg) if t.id == "smoke")
        return run_template(CheckContext(cfg, task, cfg.results_dir / "smoke"))

    def test_a_healthy_page_passes_with_screenshots(self):
        res = self.check('[[params.routes]]\npath = "/ok"\nmodes = ["logged_out"]\nreuse = "/invitations"\n'
                         'expect_endpoints = ["/api/plans"]\nexpect_endpoints_in = "logged_out"\n')
        self.assertEqual(res.gaps, [])
        self.assertEqual(sorted(p.name for p in res.files), ["00-ok-logged_out.png", "00-reuse-invitations.png"])
        self.assertTrue(all(p.stat().st_size > 100 for p in res.files))

    def test_stuck_spinner_console_error_and_redirect_are_caught(self):
        res = self.check('[[params.routes]]\npath = "/stuck"\nmodes = ["logged_out"]\n'
                         '[[params.routes]]\npath = "/broken"\nmodes = ["logged_out"]\n'
                         '[[params.routes]]\npath = "/jump"\nmodes = ["logged_out"]\n')
        text = "\n".join(res.gaps)
        self.assertIn("/stuck [logged_out]: endless spinner", text)
        self.assertIn("/broken [logged_out]: 1 console error(s): boom from the page", text)
        self.assertIn(f"/jump [logged_out]: unexpected redirect to {self.base}/login", text)

    def test_a_generic_error_after_an_invalid_submit_is_caught(self):
        res = self.check('[[params.routes]]\npath = "/form"\nmodes = ["logged_out"]\n'
                         '[params.routes.invalid_submit]\nmode = "logged_out"\n'
                         'steps = [{ fill = "#email", value = "x" }, { click = "button" }]\n'
                         'message_selector = "[role=alert]"\nexpect_field = "email"\n')
        text = "\n".join(res.gaps)
        self.assertIn("invalid submit: a 422 must say which field is wrong ('email')", text)


if __name__ == "__main__":
    unittest.main()
