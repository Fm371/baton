"""The shared pieces of the built-in checks: the error-message rule, the
hardcoded-key scan and the dev-server helper."""

import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from baton.checks.errormsg import check_error_message
from baton.checks.keycheck import justified, parse_added_lines, scan_diff, scan_line
from baton.checks.servers import CheckError, dev_servers, reachable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ErrorMessageTest(unittest.TestCase):
    def test_generic_message_without_a_code_is_rejected(self):
        [p] = check_error_message("This opportunity is not available, try again", 404)
        self.assertIn("generic message without an error code", p)
        self.assertIn("HTTP 404", p)

    def test_a_status_or_an_error_code_makes_a_generic_message_acceptable(self):
        self.assertEqual(check_error_message("Something went wrong (HTTP 500)", 500), [])
        self.assertEqual(check_error_message("Plan not available, try again. Code: PLAN_EXPIRED", 410), [])
        self.assertEqual(check_error_message("Plan expired [ERR_PLAN_EXPIRED]", None), [])

    def test_a_422_names_the_field_and_the_format(self):
        self.assertEqual(check_error_message("The email must look like name@example.com", 422, "email"), [])
        both = check_error_message("Invalid input", 422, "email")
        self.assertEqual(len(both), 2)
        self.assertIn("which field", both[0])
        self.assertIn("what format", both[1])
        [fmt] = check_error_message("The email is wrong", 422, "email")
        self.assertIn("what format", fmt)

    def test_field_names_match_with_underscores_or_spaces(self):
        self.assertEqual(check_error_message("Enter the national id as 10 digits", 422, "national_id"), [])

    def test_an_unexpected_error_shows_its_status(self):
        [p] = check_error_message("The server fell over", 503)
        self.assertIn("HTTP status (503)", p)
        self.assertEqual(check_error_message("The server fell over (503)", 503), [])

    def test_no_message_at_all(self):
        self.assertEqual(check_error_message(None, 422), ["no error message is shown"])
        self.assertEqual(check_error_message("  ", 500), ["no error message is shown"])


class KeyCheckTest(unittest.TestCase):
    def test_flags_literals_that_look_like_data_keys(self):
        for line in ['if type_key == "person":', 'q = {"type_key": "person"}',
                     'db.types.find_one({"key": "person"})', 'resource_type="person"',
                     'if ("person" === typeKey) {', "const q = { plan_slug: 'pro' }"]:
            self.assertIsNotNone(scan_line(line), line)

    def test_leaves_ordinary_literals_alone(self):
        for line in ['content_type = "application/json"', 'if status == "active":', '<li key="item">',
                     'api_key = "abc123"', 'x = row["type_key"]', 'plan_slug = "pro"  # baton: ok',
                     'KEY = "x"']:
            self.assertIsNone(scan_line(line), line)

    def test_scan_diff_skips_tests_and_non_source_files(self):
        diff = (
            "diff --git a/app/svc.py b/app/svc.py\n--- a/app/svc.py\n+++ b/app/svc.py\n"
            "@@ -1,0 +10,2 @@\n+x = 1\n+if type_key == \"person\":\n"
            "diff --git a/tests/test_svc.py b/tests/test_svc.py\n--- /dev/null\n+++ b/tests/test_svc.py\n"
            "@@ -0,0 +1 @@\n+type_key = \"person\"\n"
            "diff --git a/notes.md b/notes.md\n--- a/notes.md\n+++ b/notes.md\n"
            "@@ -0,0 +1 @@\n+type_key == \"person\"\n")
        self.assertEqual(parse_added_lines(diff)[1], ("app/svc.py", 11, 'if type_key == "person":'))
        [f] = scan_diff(diff, ["tests/**", "**/test_*.py"])
        self.assertEqual((f.path, f.line, f.value), ("app/svc.py", 11, "person"))

    def test_justified_needs_the_heading(self):
        self.assertTrue(justified("STATUS: done\n\n## Hardcoded keys\n`person` is a protocol constant.\n"))
        self.assertFalse(justified("STATUS: done\n\nI looked at hardcoded keys.\n"))


class DevServersTest(unittest.TestCase):
    def spec(self, port):
        return {"name": "web", "url": f"http://127.0.0.1:{port}/",
                "cmd": f"{sys.executable} -m http.server {port} --bind 127.0.0.1", "cwd": "."}

    def test_starts_a_server_and_stops_it(self):
        port, d = free_port(), Path(tempfile.mkdtemp())
        with dev_servers([self.spec(port)], d, d / "out", 20) as notes:
            self.assertTrue(reachable(f"http://127.0.0.1:{port}/"))
            self.assertIn("started", notes[0])
        self.assertFalse(reachable(f"http://127.0.0.1:{port}/"))

    def test_reuses_a_running_server_and_leaves_it_running(self):
        port, d = free_port(), Path(tempfile.mkdtemp())
        proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                                cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if reachable(f"http://127.0.0.1:{port}/"):
                    break
                time.sleep(0.1)
            with dev_servers([self.spec(port)], d, d / "out", 20) as notes:
                self.assertIn("reusing", notes[0])
            self.assertTrue(reachable(f"http://127.0.0.1:{port}/"))
        finally:
            proc.terminate()
            proc.wait()

    def test_a_server_that_dies_is_an_error_with_its_log(self):
        d = Path(tempfile.mkdtemp())
        spec = {"name": "bad", "url": f"http://127.0.0.1:{free_port()}/",
                "cmd": f"{sys.executable} -c 'import sys; print(\"boom\"); sys.exit(3)'"}
        with self.assertRaises(CheckError) as cm:
            with dev_servers([spec], d, d / "out", 20):
                pass
        self.assertIn("exited with 3", str(cm.exception))
        self.assertIn("boom", str(cm.exception))

    def test_no_command_and_nothing_listening_is_an_error(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(CheckError):
            with dev_servers([{"url": f"http://127.0.0.1:{free_port()}/"}], d, d / "out", 5):
                pass


if __name__ == "__main__":
    unittest.main()
