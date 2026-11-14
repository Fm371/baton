"""{base_dir} placeholder and the plan checks for absolute paths in common.md, task files and settings."""

import json
from pathlib import Path

from baton.config import load_config
from baton.pathcheck import expand, missing_paths, path_warnings
from baton.preflight import preflight
from tests.test_runner import RunnerFixture


class PathCheckTest(RunnerFixture):
    def use_settings(self, data):
        (self.run_dir / "claude-settings.json").write_text(json.dumps(data))
        toml = (self.run_dir / "baton.toml").read_text()
        toml = toml.replace("[providers.claude]", '[providers.claude]\nsettings = "claude-settings.json"', 1)
        (self.run_dir / "baton.toml").write_text(toml)

    def plan_warnings(self):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, _ = preflight(cfg)
        self.assertEqual(errors, [])
        return warnings

    def test_expand_replaces_the_placeholder_only(self):
        self.assertEqual(expand("see {base_dir}/docs and {other}", Path("/srv/x")), "see /srv/x/docs and {other}")

    def test_missing_paths_ignores_url_paths_and_existing_paths(self):
        here = str(self.run_dir)
        text = f"call /api/plans and /v1/users; read {here}/common.md; also /tmp/nobody-xyz-gone/gone/file.md."
        self.assertEqual(missing_paths(text), ["/tmp/nobody-xyz-gone/gone/file.md"])

    def test_plan_warns_about_a_missing_path_in_common_md_and_in_a_task(self):
        self.write_config()
        (self.run_dir / "common.md").write_text("Rules live in /tmp/nobody-xyz-gone/old-checkout/RULES.md\n")
        (self.run_dir / "RULES.md").write_text("x\n")     # the same file now sits in the baton folder: it moved
        self.add_task("a")
        task = self.run_dir / "tasks" / "a.md"
        task.write_text(task.read_text() + "\nDesign: /tmp/nobody-xyz-gone/old-checkout/DESIGN.md\n")
        warnings = self.plan_warnings()
        self.assertTrue(any("common.md" in w and "RULES.md" in w and "{base_dir}" in w for w in warnings), warnings)
        self.assertTrue(any("task a" in w and "DESIGN.md" in w for w in warnings), warnings)

    def test_base_dir_placeholder_is_expanded_everywhere_and_is_not_reported(self):
        self.write_config()
        (self.run_dir / "common.md").write_text("Rules: {base_dir}/common.md\n")
        self.add_task("a")
        task = self.run_dir / "tasks" / "a.md"
        task.write_text(task.read_text() + "\nNotes: {base_dir}/tasks/a.md\n")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        warnings = self.plan_warnings()
        self.assertFalse([w for w in warnings if "does not exist" in w], warnings)
        self.assertEqual(self.run_baton(), 0)
        prompt = self.calls()[0]["prompt"]
        self.assertIn(f"Rules: {self.run_dir.resolve()}/common.md", prompt)
        self.assertIn(f"Notes: {self.run_dir.resolve()}/tasks/a.md", prompt)
        self.assertNotIn("{base_dir}", prompt)

    def test_settings_with_the_placeholder_are_rewritten_for_the_attempt(self):
        self.write_config()
        self.use_settings({"permissions": {"deny": ["Read({base_dir}/secret.env)"], "allow": []}})
        self.add_task("a")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        argv = self.calls()[0]["argv"]
        used = Path(argv[argv.index("--settings") + 1])
        self.assertNotEqual(used.name, "claude-settings.json")        # a per-attempt copy, not the template
        data = json.loads(used.read_text())
        self.assertIn(f"Read({self.run_dir.resolve()}/secret.env)", data["permissions"]["deny"])

    def test_plan_warns_when_a_settings_rule_points_at_a_path_that_is_gone(self):
        self.write_config()
        self.use_settings({"permissions": {"deny": ["Read(//tmp/nobody-xyz-gone/old/.env)",
                                                    "Read({base_dir}/.env)"]}})
        self.add_task("a")
        warnings = self.plan_warnings()
        gone = [w for w in warnings if "settings file mentions" in w]
        self.assertEqual(len(gone), 1, warnings)
        self.assertIn("/tmp/nobody-xyz-gone/old/.env", gone[0])

    def test_plan_warns_when_settings_allow_writing_outside_the_baton_folder_and_repos(self):
        self.write_config()
        outside = self.root / "elsewhere"
        outside.mkdir()
        self.use_settings({"permissions": {"allow": [f"Edit(/{outside}/**)"], "deny": []}})
        self.add_task("a")
        warnings = self.plan_warnings()
        self.assertTrue(any("write to" in w and str(outside) in w for w in warnings), warnings)

    def test_path_warnings_function_is_quiet_without_problems(self):
        self.write_config()
        self.add_task("a")
        cfg = load_config(self.run_dir / "baton.toml")
        _, _, tasks = preflight(cfg)
        self.assertEqual(path_warnings(cfg, tasks), [])

    def test_bash_prefix_rules_lose_their_wildcard_and_relative_rules_are_skipped(self):
        self.write_config()
        self.use_settings({"permissions": {"allow": ["Bash(/usr/bin/true:*)", "Bash(.venv/bin/python:*)",
                                                     "Bash(git diff:*)"], "deny": []}})
        self.add_task("a")
        gone = [w for w in self.plan_warnings() if "settings file mentions" in w]
        self.assertEqual(gone, [])

    def test_a_missing_path_in_an_allow_rule_gets_its_own_text(self):
        self.write_config()
        self.use_settings({"permissions": {"allow": ["Bash(/tmp/nobody-xyz-gone/bin/ruff:*)"],
                                           "deny": ["Read(//tmp/nobody-xyz-gone/.env)"]}})
        self.add_task("a")
        gone = [w for w in self.plan_warnings() if "settings file mentions" in w]
        self.assertEqual(len(gone), 2, gone)
        allow = next(w for w in gone if "ruff" in w)
        self.assertIn("allow rule", allow)
        self.assertNotIn("protects nothing", allow)
        self.assertNotIn("ruff:", allow)

    def test_no_base_dir_hint_for_a_path_that_is_deliberately_elsewhere(self):
        self.write_config()
        (self.run_dir / "common.md").write_text("Notes: /tmp/nobody-xyz-gone/elsewhere/NOTES.md\n")
        self.add_task("a")
        warnings = [w for w in self.plan_warnings() if "NOTES.md" in w]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertNotIn("{base_dir}", warnings[0])

    def test_a_missing_results_folder_is_not_an_add_dirs_error(self):
        import shutil
        self.write_config()
        self.add_task("a")
        toml = self.run_dir / "baton.toml"
        toml.write_text(toml.read_text().replace("[providers.claude]", f'[providers.claude]\nadd_dirs = ["{self.run_dir}/results"]', 1))
        shutil.rmtree(self.run_dir / "results", ignore_errors=True)
        cfg = load_config(toml)
        errors, _, _ = preflight(cfg)
        self.assertEqual(errors, [])
        self.assertTrue((self.run_dir / "results").is_dir())
