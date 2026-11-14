"""Fixes from the 0.4.0 field report: `after`, releasing held-back tasks, relinking edited task files."""

import json

from baton import ops
from baton.config import load_config
from baton.preflight import preflight
from baton.runner import Runner
from baton.tasks import load_tasks, order_tasks
from tests.test_runner import RunnerFixture


class QueueFixesTest(RunnerFixture):
    def task_on(self, tid, branch, extra=""):
        (self.run_dir / "tasks" / f"{tid}.md").write_text(
            f'+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "{branch}"\n{extra}+++\nBuild {tid}.\n')

    def cfg(self):
        return load_config(self.run_dir / "baton.toml")

    def test_after_orders_tasks_on_different_branches_without_containment(self):
        self.task_on("a", "feat/a")
        self.task_on("b", "feat/b", 'after = ["a"]\n')
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()}, {"a": "done", "b": "done"})
        calls = [c for c in self.calls()]
        self.assertEqual(len(calls), 2)

    def test_after_runs_even_when_the_other_task_failed(self):
        self.task_on("a", "feat/a")
        self.task_on("b", "feat/b", 'after = ["a"]\n')
        self.scenario_is(claude=[{"status": "partial"}, {"commit": "b.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["a"].status, "failed")
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_after_unknown_task_is_an_error(self):
        self.task_on("b", "feat/b", 'after = ["nope"]\n')
        errors, _, _ = preflight(self.cfg())
        self.assertTrue(any("nope" in e for e in errors), errors)

    def test_retried_verifier_frees_the_tasks_it_gated(self):
        self.add_task("a")
        self.add_verifier("va")
        self.add_task("b", deps=["va"])
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "partial"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["b"].status, "gated")
        ops.prepare_retry(self.cfg(), "va")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"},
                                 {"commit": "test_a.py", "status": "partial"},
                                 {"commit": "test_a2.py", "status": "done"}])
        self.assertEqual(self.run_baton(only="va"), 0)
        self.assertEqual(self.state.tasks["va"].status, "done")
        b = self.state.tasks["b"]
        self.assertEqual((b.status, b.note, b.gated_by), ("pending", "", None))

    def test_a_retried_dependency_releases_the_skipped_dependent(self):
        self.add_task("a")
        self.task_on("b", "feat/b", 'depends_on = ["a"]\n')
        self.scenario_is(claude=[{"status": "partial"}])
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["b"].status, "skipped")
        ops.prepare_retry(self.cfg(), "a")
        self.scenario_is(claude=[{"status": "partial"}, {"commit": "a.txt", "status": "done"}])
        self.assertEqual(self.run_baton(only="a"), 0)
        self.assertEqual(self.state.tasks["b"].status, "pending")

    def test_mark_partial_retires_a_task(self):
        self.add_task("a")
        self.scenario_is(claude=[{"status": "blocked"}])
        self.run_baton()
        out = ops.mark(self.cfg(), "a", "partial", "superseded", None)
        self.assertIn("partial", out)
        self.assertEqual(json.loads((self.run_dir / "state" / "run.json").read_text())["tasks"]["a"]["status"],
                         "partial")

    def test_an_edited_depends_on_is_taken_before_the_next_pick(self):
        self.add_task("a")
        self.task_on("b", "feat/b")
        cfg = self.cfg()
        _, _, tasks = preflight(cfg)
        r = Runner(cfg, clock=self.clock if hasattr(self, "clock") else None, echo=False, follow=True)
        r.ordered = tasks
        by_id = {t.id: t for t in tasks}
        self.task_on("b", "feat/b", 'depends_on = ["a"]\n')
        _, _, new = r.reload(tasks, by_id, {t.id for t in tasks})
        self.assertEqual(by_id["b"].depends_on, ["a"])

    def test_resumed_verifier_forgets_commits_other_tasks_made_meanwhile(self):
        from baton import gitutil
        from tests.test_runner import git
        self.add_task("a")
        self.add_verifier("va")
        cfg = self.cfg()
        _, _, tasks = preflight(cfg)
        r = Runner(cfg, echo=False)
        r.ordered = tasks
        va = next(t for t in tasks if t.id == "va")
        old = gitutil.head(self.repo)
        ts = r.state.task("va")
        ts.status, ts.start_head, ts.started = "running", old, "2026-11-07T10:00:00"
        r.state.task("a").finished = "2026-11-07T10:30:00"
        (self.repo / "src.py").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "other task")
        r._rebase_resumed_verifier(va, ts)
        self.assertEqual(ts.start_head, gitutil.head(self.repo))

    def test_codex_verifiers_need_an_opt_in_and_then_only_warn(self):
        self.write_config(verifier_providers=["codex"])
        self.add_task("a")
        self.add_verifier("va")
        errors, _, _ = preflight(self.cfg())
        self.assertTrue(any("allow_codex_verifiers" in e for e in errors), errors)
        self.write_config(verifier_providers=["codex"], allow_codex_verifiers=True)
        errors, warnings, _ = preflight(self.cfg())
        self.assertEqual(errors, [])
        self.assertTrue(any("allowed by run.allow_codex_verifiers" in w for w in warnings), warnings)

    def test_a_finished_codex_verifier_is_not_refused_again(self):
        self.write_config(verifier_providers=["codex"])
        self.add_task("a")
        self.add_verifier("va")
        self.scenario_is(codex=[{"commit": "a.txt", "status": "done"}])
        from baton.state import load_state, save_state
        cfg = self.cfg()
        st = load_state(cfg.state_dir / "run.json")
        st.task("va").status = "done"
        save_state(cfg.state_dir / "run.json", st)
        errors, _, _ = preflight(cfg)
        self.assertFalse(any("codex" in e and "va" in e for e in errors), errors)

    def test_approve_is_refused_while_a_dependency_is_open(self):
        import contextlib, io
        from baton import cli
        from baton.state import load_state, save_state
        self.add_task("a")
        (self.run_dir / "tasks" / "gate.md").write_text('+++\nkind = "approve"\ndepends_on = ["a"]\n+++\nOK?\n')
        cfg = self.cfg()
        st = load_state(cfg.state_dir / "run.json")
        st.task("gate").status = "awaiting"
        st.task("a").status = "pending"
        save_state(cfg.state_dir / "run.json", st)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["approve", "-c", str(self.run_dir / "baton.toml"), "gate"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot be approved yet", err.getvalue())

    def test_generated_claude_settings_turn_the_commit_trailer_off(self):
        from baton.isolation import write_claude_settings
        out = write_claude_settings(None, [], self.root / "s" / "x.json")
        data = json.loads(out.read_text())
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"]["commit"], "")
        mine = self.root / "mine.json"
        mine.write_text(json.dumps({"attribution": {"commit": "keep"}}))
        data = json.loads(write_claude_settings(str(mine), [], self.root / "s" / "y.json").read_text())
        self.assertEqual(data["attribution"]["commit"], "keep")

    def test_retry_during_a_run_is_queued_and_the_live_run_requeues_the_task(self):
        from baton.state import load_state, save_state
        self.add_task("a")
        cfg = self.cfg()
        _, _, tasks = preflight(cfg)
        r = Runner(cfg, echo=False)
        r.ordered = tasks
        r.state.task("a").status = "failed"
        queued = []
        with __import__("unittest.mock").mock.patch("baton.ops.run_active", return_value=True), \
                __import__("unittest.mock").mock.patch("baton.ops.queue_op", side_effect=lambda c, o: queued.append(o)):
            self.assertIsNone(ops.prepare_retry(cfg, "a"))
        self.assertEqual(queued, [{"op": "retry", "task": "a"}])
        ops.apply_retry(r, tasks[0])
        pending = []
        r.take_freed(pending)
        self.assertEqual([t.id for t in pending], ["a"])
        self.assertEqual(r.state.task("a").status, "pending")

    def test_a_dependency_through_an_approval_checkpoint_keeps_the_branch_base(self):
        self.task_on("a", "feat/a")
        (self.run_dir / "tasks" / "gate.md").write_text('+++\nkind = "approve"\ndepends_on = ["a"]\n+++\nOK?\n')
        self.task_on("b", "feat/b", 'depends_on = ["gate"]\n')
        tasks = {t.id: t for t in order_tasks(load_tasks(self.cfg()))}
        self.assertEqual(tasks["b"].base, "feat/a")

    def test_the_key_scan_skips_history_the_task_only_imported(self):
        import os
        import subprocess
        from baton import gitutil
        from baton.checks import keycheck
        start = gitutil.head(self.repo)
        (self.repo / "old.py").write_text('type_key = "person"\n')
        env = {**os.environ, "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"}
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "imported"], check=True, env=env)
        self.assertEqual(len(keycheck.scan_repo(self.repo, start, [])), 1)                       # whole range
        self.assertEqual(keycheck.scan_repo(self.repo, start, [], started="2026-11-01T00:00:00Z"), [])
        (self.repo / "new.py").write_text('type_key = "company"\n')
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "mine"], check=True)
        found = keycheck.scan_repo(self.repo, start, [], started="2026-11-01T00:00:00Z")
        self.assertEqual([f.value for f in found], ["company"])

    def test_creates_base_silences_the_main_line_warning_for_that_task_only(self):
        self.task_on("imp", "develop", "creates_base = true\n")
        self.task_on("other", "develop")
        _, warnings, _ = preflight(self.cfg())
        mine = [w for w in warnings if "is a main line" in w]
        self.assertEqual(len(mine), 1, warnings)
        self.assertIn("task other", mine[0])
