"""[repos."<path>"] forbid_literals on top of the built-in hardcoded-key check."""

import re
import unittest

from baton.checks.keycheck import FORBIDDEN, scan_diff
from baton.config import ConfigError, load_config
from tests.test_runner import RunnerFixture

DIFF = """\
diff --git a/app/db.py b/app/db.py
+++ b/app/db.py
@@ -0,0 +1,3 @@
+URL = "mongodb://prod-host/app"
+OK = "mongodb://prod-host/app"  # baton: ok
+x = 1
diff --git a/tests/test_db.py b/tests/test_db.py
+++ b/tests/test_db.py
@@ -0,0 +1 @@
+URL = "mongodb://prod-host/app"
diff --git a/config/app.yaml b/config/app.yaml
+++ b/config/app.yaml
@@ -0,0 +1 @@
+db: mongodb://prod-host/app
"""


class ScanTest(unittest.TestCase):
    def test_forbidden_literals_match_any_file_but_tests_and_marked_lines(self):
        found = scan_diff(DIFF, ["tests/**"], [re.compile(r"mongodb://\S+")], builtin=False)
        self.assertEqual([(f.path, f.line, f.name) for f in found],
                         [("app/db.py", 1, FORBIDDEN), ("config/app.yaml", 1, FORBIDDEN)])
        self.assertEqual(found[0].value, 'mongodb://prod-host/app"')

    def test_nothing_is_forbidden_by_default(self):
        self.assertEqual(scan_diff(DIFF, ["tests/**"], None, builtin=False), [])


class ForbidRunTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.add_task("a")

    def forbid(self, *patterns, **run):
        self.write_config(extra='\n[repos."../repo"]\nforbid_literals = ' + str(list(patterns)).replace("'", '"') + "\n",
                          **run)

    def test_a_forbidden_literal_sends_the_agent_back_once(self):
        self.forbid("prod-host")
        self.scenario_is(claude=[{"commit": "app.py", "content": 'URL = "mongodb://prod-host/app"\n',
                                  "status": "done"}, {"status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("literals this repository forbids", calls[1]["prompt"])
        self.assertIn('app.py:1: forbidden literal = "prod-host"', calls[1]["prompt"])
        self.assertNotIn("look like hardcoded data keys", calls[1]["prompt"])
        self.assertIn("1 possible hardcoded data key(s)", self.state.tasks["a"].note)

    def test_it_applies_even_when_the_builtin_check_is_off(self):
        self.forbid("prod-host", hardcoded_key_check=False)
        self.scenario_is(claude=[{"commit": "app.py", "content": "x = 'prod-host'\n", "status": "done"},
                                 {"status": "done"}])
        self.run_baton()
        self.assertEqual(len(self.calls()), 2)

    def test_other_repositories_and_clean_work_are_not_affected(self):
        self.forbid("prod-host")
        self.scenario_is(claude=[{"commit": "app.py", "content": "x = 1\n", "status": "done"}])
        self.run_baton()
        self.assertEqual(len(self.calls()), 1)

    def test_an_invalid_regex_fails_the_config(self):
        self.forbid("(unclosed")
        with self.assertRaises(ConfigError) as e:
            load_config(self.run_dir / "baton.toml")
        self.assertIn("bad regex", str(e.exception))


if __name__ == "__main__":
    unittest.main()
