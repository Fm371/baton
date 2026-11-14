"""Characterization of `preflight()` and `plan_text()`: the exact messages, in order, for a set of rich
workspaces. It guards refactoring of baton/preflight.py: the output may only change on purpose, and then
the golden file is regenerated with `BATON_UPDATE_GOLDEN=1 python3 -m unittest tests.test_preflight_golden`."""

import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from baton.config import load_config
from baton.preflight import plan_text, preflight
from tests.test_runner import RunnerFixture, git

GOLDEN = Path(__file__).parent / "fixtures" / "preflight_golden.json"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class PreflightGolden(RunnerFixture):
    # --- helpers ---------------------------------------------------------------------------------
    def toml(self) -> str:
        return (self.run_dir / "baton.toml").read_text()

    def edit_toml(self, old: str, new: str) -> None:
        text = self.toml()
        assert old in text, old
        (self.run_dir / "baton.toml").write_text(text.replace(old, new, 1))

    def task_file(self, tid: str, front: str, body: str = "do it") -> None:
        (self.run_dir / "tasks" / f"{tid}.md").write_text(f"+++\n{front}+++\n{body}\n")

    def verifier_file(self, vid: str, front: str, body: str = "check it") -> None:
        d = self.run_dir / "verifiers"
        d.mkdir(exist_ok=True)
        (d / f"{vid}.md").write_text(f"+++\nkind = \"verify\"\n{front}+++\n{body}\n")

    def other_repo(self, name: str, branch: str = "develop") -> Path:
        r = self.root / name
        r.mkdir()
        git(r, "init", "-q", "-b", branch)
        git(r, "config", "user.email", "t@example.com")
        git(r, "config", "user.name", "t")
        (r / "f").write_text("x\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "init")
        return r

    def snapshot(self, provider=None, create_dirs=False) -> dict:
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, tasks = preflight(cfg, provider, create_dirs=create_dirs)
        text = plan_text(cfg, tasks, provider, now=NOW)

        def norm(s: str) -> str:
            return s.replace(str(self.root.resolve()), "<ROOT>").replace(str(self.root), "<ROOT>")

        return {"errors": [norm(e) for e in errors], "warnings": [norm(w) for w in warnings],
                "tasks": [t.id for t in tasks], "plan": norm(text)}

    def write_run(self, **extra):
        old = self.toml()
        opts = {"start_provider": "claude", "quick_exit_seconds": 0, "transient_wait_seconds": 5,
                "timezone": "Asia/Tehran", "default_base": "develop", "common_prompt": "common.md",
                "min_free_mb": 0}
        opts.update(extra)
        run_toml = "\n".join(f"{k} = {json.dumps(v)}" for k, v in opts.items())
        _, rest = old.split("[providers.claude]", 1)
        (self.run_dir / "baton.toml").write_text(f"\n[run]\n{run_toml}\n\n[providers.claude]{rest}")

    # --- scenarios --------------------------------------------------------------------------------
    def s_clean_basic(self):
        self.add_task("a")

    def s_no_tasks(self):
        pass

    def s_codex_disabled_start_codex(self):
        self.edit_toml("[providers.codex]\n", "[providers.codex]\nenabled = false\n")
        self.write_run(start_provider="codex")
        self.add_task("a")

    def s_all_disabled(self):
        self.edit_toml("[providers.codex]\n", "[providers.codex]\nenabled = false\n")
        self.edit_toml("[providers.claude]\n", "[providers.claude]\nenabled = false\n")
        self.add_task("a")

    def s_missing_binary(self):
        self.edit_toml(f'bin = "{self.root}/bin/codex"', 'bin = "/nonexistent/codex"')
        self.add_task("a")

    def s_settings_and_add_dirs(self):
        verifiers = self.run_dir / "verifiers"
        verifiers.mkdir()
        self.edit_toml("[providers.claude]\n", f'[providers.claude]\nsettings = "/nonexistent/s.json"\n'
                                              f'add_dirs = ["/nonexistent/dir", "{verifiers}", "{self.root}"]\n')
        self.add_task("a")

    def s_dirty_repo(self):
        (self.repo / "scratch.txt").write_text("dirty\n")
        self.add_task("a")

    def s_baton_folder_inside_repo(self):
        inside = self.repo / "task-auto-results"
        inside.mkdir()
        (inside / "x").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "results")
        self.write_run(results_dir=str(inside))
        self.add_task("a")

    def s_not_a_repo_and_bare(self):
        plain = self.root / "plain"
        plain.mkdir()
        bare = self.root / "bare.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.task_file("a", 'repo = "../plain"\nwork_type = "feature"\n')
        self.task_file("b", 'repo = "../bare.git"\nwork_type = "feature"\n')
        self.task_file("c", 'repo = "../missing-dir"\nwork_type = "feature"\n')

    def s_branch_variants(self):
        self.task_file("a", 'repo = "../repo"\nwork_type = "feature"\nbranch = "feat/new"\nbase = "nope"\n')
        self.task_file("b", 'repo = "../repo"\nwork_type = "feature"\nbranch = "bad..name"\n')
        self.task_file("c", 'repo = "../repo"\nwork_type = "feature"\nbranch = "main"\n')
        self.task_file("d", 'repo = "../repo"\nwork_type = "feature"\nbranch = "develop"\n')
        self.task_file("e", 'repo = "../repo"\nwork_type = "feature"\n')

    def s_existing_branch_with_history(self):
        git(self.repo, "checkout", "-q", "-b", "feat/old", "develop")
        (self.repo / "old.txt").write_text("old\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "old")
        git(self.repo, "checkout", "-q", "develop")
        git(self.repo, "checkout", "-q", "-b", "feat/b", "develop")
        (self.repo / "b.txt").write_text("b\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "b")
        git(self.repo, "checkout", "-q", "develop")
        self.task_file("a", 'repo = "../repo"\nwork_type = "feature"\nbranch = "feat/old"\n')
        self.task_file("b", 'repo = "../repo"\nwork_type = "feature"\nbranch = "feat/b"\nbase = "feat/old"\n'
                            'depends_on = ["a"]\n')

    def s_rung_without_model(self):
        self.edit_toml("[routing.work_types]", '[routing.rungs.r4]\nclaude = { model = "sonnet", effort = "high" }\n\n'
                                              '[routing.work_types]')
        self.task_file("a", 'repo = "../repo"\nrung = "r4"\nbranch = "feat/x"\n')
        self.task_file("b", 'repo = "../repo"\nrung = "r4"\nbranch = "feat/x"\nproviders = ["codex"]\n')

    def s_verifier_providers(self):
        self.edit_toml("[providers.claude]\n", "[providers.claude]\nenabled = false\n")
        self.add_task("a")
        self.add_verifier("v", extra='providers = ["codex"]\n')
        self.task_file("c", 'repo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"\nproviders = ["claude"]\n')

    def s_money_gates_and_suite(self):
        self.task_file("a", 'repo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"\nfeature = "f1"\n')
        self.verifier_file("g1", 'repo = "../repo"\nwork_type = "feature"\nmoney_gate = true\n'
                                 'feature = "f1"\ndepends_on = ["a"]\n')
        self.verifier_file("g2", 'repo = "../repo"\nwork_type = "feature"\nmoney_gate = true\n'
                                 'feature = "f1"\ndepends_on = ["a"]\n')
        self.verifier_file("s", 'template = "repo-suite"\nrepo = "../repo"\ndepends_on = ["a"]\n'
                                '[params]\nbaseline = "missing-baseline.txt"\n')

    def s_lanes(self):
        self.write_run(lanes=2)
        self.add_task("a")
        self.add_verifier("v", extra='providers = ["claude"]\n')

    def s_lanes_two_repos(self):
        other = self.other_repo("repo2")
        self.write_run(lanes=2)
        self.add_task("a")
        self.task_file("b", f'repo = "../{other.name}"\nwork_type = "feature"\nbranch = "feat/y"\n'
                            'providers = ["codex"]\n')

    def s_unsafe_and_heavy(self):
        self.edit_toml("[providers.claude]\n", '[providers.claude]\npermission_mode = "bypassPermissions"\n'
                                              'extra_args = ["--dangerously-skip-permissions"]\n')
        self.edit_toml('claude = { model = "sonnet", effort = "medium" }',
                       'claude = { model = "opus", effort = "xhigh" }')
        self.edit_toml('codex = { model = "sol", effort = "medium" }',
                       'codex = { model = "gpt-astra", effort = "medium" }')
        self.add_task("a")

    def s_watch_repos(self):
        plain = self.root / "plainwatch"
        plain.mkdir()
        self.write_run(watch_repos=[str(plain)])
        self.add_task("a")

    def s_subfolder_repo(self):
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "f").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "sub")
        self.task_file("a", 'repo = "../repo/sub"\nwork_type = "feature"\nbranch = "feat/x"\n')

    def s_state_driven(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.add_task("c", deps=["b"], extra="priority = 5\n")
        state = {"version": 2, "current": "codex",
                 "providers": {"claude": {"limited_until": (NOW + timedelta(hours=2)).isoformat(),
                                          "window": "five_hour",
                                          "model_limits": {"opus": (NOW + timedelta(hours=1)).isoformat(),
                                                           "haiku": (NOW - timedelta(hours=1)).isoformat()}},
                               "codex": {"limited_until": (NOW - timedelta(hours=1)).isoformat()}},
                 "quick_fails": 0,
                 "tasks": {"a": {"status": "running", "attempts": 1}, "b": {"status": "pending"},
                           "c": {"status": "done"}}}
        (self.run_dir / "state").mkdir(exist_ok=True)
        (self.run_dir / "state" / "run.json").write_text(json.dumps(state))

    def s_plan_kinds(self):
        self.write_run(verifiers_last=True, lanes=3)
        self.add_task("a", extra="return_mid_task = false\n")
        self.task_file("ap", 'kind = "approve"\ndepends_on = ["a"]\n', "ok?")
        self.verifier_file("t", 'template = "env-preflight"\nrepo = "../repo"\ndepends_on = ["a"]\n')
        self.verifier_file("w", 'repo = "../repo"\nwork_type = "feature"\nfeature = "f2"\n'
                                'when = "feature-end"\ndepends_on = ["a"]\n')
        self.add_verifier("v")

    def s_required_env_and_paths(self):
        self.write_run(required_env=["BATON_GOLDEN_NOT_SET_VAR"])
        self.add_task("a")
        (self.run_dir / "common.md").write_text("see /definitely/not/here/file.txt please\n")

    def s_services_down(self):
        self.edit_toml("[routing.work_types]", '[services.db]\ncheck = "false"\n\n[routing.work_types]')
        self.task_file("a", 'repo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"\nneeds = ["db"]\n')

    def s_provider_override(self):
        self.add_task("a")
        return {"provider": "codex"}

    def s_provider_not_enabled(self):
        self.edit_toml("[providers.codex]\n", "[providers.codex]\nenabled = false\n")
        self.add_task("a")
        return {"provider": "codex"}

    def s_create_dirs(self):
        self.add_task("a")
        return {"create_dirs": True}

    # --- the test --------------------------------------------------------------------------------------
    def test_golden(self):
        names = sorted(n for n in dir(self) if n.startswith("s_"))
        results = {}
        for name in names:
            self.setUp()                       # a fresh workspace for every scenario
            with mock.patch("baton.preflight.timezone_warning", return_value=None):
                self.write_run()
                kwargs = getattr(self, name)() or {}
                results[name[2:]] = self.snapshot(**kwargs)
        if os.environ.get("BATON_UPDATE_GOLDEN"):
            GOLDEN.write_text(json.dumps(results, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        expected = json.loads(GOLDEN.read_text())
        self.assertEqual(sorted(results), sorted(expected))
        for name in sorted(results):
            with self.subTest(scenario=name):
                self.assertEqual(results[name], expected[name])


if __name__ == "__main__":
    unittest.main()
