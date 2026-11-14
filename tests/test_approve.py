"""Approval checkpoints (kind = "approve") and the user-journey warnings."""

import json
import unittest

from baton import cli
from baton.config import load_config
from baton.preflight import plan_text, preflight
from tests.test_runner import RunnerFixture


class ApproveTest(RunnerFixture):
    def add_gate(self, gid="gate", deps=("a",), body="Is the flow right? Compare with the invitation page.",
                 extra=""):
        (self.run_dir / "tasks" / f"{gid}.md").write_text(
            f'+++\nkind = "approve"\ndepends_on = {json.dumps(list(deps))}\n{extra}+++\n{body}\n')

    def baton(self, *args):
        return cli.main([args[0], "-c", str(self.run_dir / "baton.toml"), *args[1:]])

    def two_phase_workspace(self):
        self.add_task("a")
        self.add_task("c")
        self.add_gate()
        self.add_task("b", deps=["gate"])
        self.add_task("d", deps=["b"])

    def test_parse_needs_neither_repo_nor_work_type(self):
        self.two_phase_workspace()
        cfg = load_config(self.run_dir / "baton.toml")
        errors, _, tasks = preflight(cfg)
        self.assertEqual(errors, [])
        gate = next(t for t in tasks if t.id == "gate")
        self.assertTrue(gate.is_approval)
        self.assertFalse(gate.runs_agent)
        text = plan_text(cfg, tasks)
        self.assertIn("gate  [approve]", text)
        self.assertIn("human checkpoint", text)

    def test_dependents_wait_and_others_keep_running(self):
        self.two_phase_workspace()
        shot = self.run_dir / "results" / "a"
        shot.mkdir(parents=True)
        (shot / "join.png").write_bytes(b"png")
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual((st["a"].status, st["c"].status), ("done", "done"))
        self.assertEqual(st["gate"].status, "awaiting")
        self.assertEqual((st["b"].status, st["d"].status), ("pending", "pending"))
        self.assertIn("waiting for approval of gate", st["b"].note)
        self.assertEqual(len(self.calls()), 2)          # b and d never ran
        self.assertIn("waiting for approval: gate (holds b)", self.log)
        request = (self.run_dir / "results" / "gate.md").read_text()
        self.assertTrue(request.startswith("STATUS: awaiting-approval"))
        self.assertIn("Is the flow right?", request)
        self.assertIn("### a (done)", request)
        self.assertIn("![join.png](a/join.png)", request)
        self.assertIn("baton approve gate", request)
        self.assertIn("| gate (approve) |", (self.run_dir / "results" / "SUMMARY.md").read_text())

    def test_approve_releases_dependents(self):
        self.two_phase_workspace()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}, {"commit": "d.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.baton("approve", "gate"), 0)
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual(st["gate"].status, "done")
        self.assertEqual((st["b"].status, st["d"].status), ("done", "done"))
        decisions = list((self.run_dir / "state" / "decisions").iterdir())
        self.assertEqual([p.name.endswith(".applied.json") for p in decisions], [True])

    def test_an_applied_decision_is_not_reused(self):
        self.two_phase_workspace()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.run_baton()
        self.baton("approve", "gate")
        self.run_baton(only="gate")                     # applies the decision, then asks again
        self.assertEqual(self.state.tasks["gate"].status, "done")
        self.run_baton(only="gate")
        self.assertEqual(self.state.tasks["gate"].status, "awaiting")

    def test_reject_blocks_dependents_with_the_note(self):
        self.two_phase_workspace()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.baton("reject", "gate", "--note", "theme does not match the panel"), 0)
        self.assertEqual(self.run_baton(), 0)
        st = self.state.tasks
        self.assertEqual(st["gate"].status, "rejected")
        for tid in ("b", "d"):
            self.assertEqual((st[tid].status, st[tid].rejected_by), ("blocked", "gate"))
            self.assertIn("theme does not match the panel", st[tid].note)
        self.assertEqual(st["c"].status, "done")
        self.assertEqual(len(self.calls()), 2)
        # a rejected checkpoint is not asked again; approving it later releases the work
        self.run_baton()
        self.assertEqual(self.state.tasks["gate"].status, "rejected")

    def test_approving_after_a_rejection_releases_the_blocked_tasks(self):
        self.two_phase_workspace()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}, {"commit": "d.txt", "status": "done"}])
        self.run_baton()
        self.baton("reject", "gate", "--note", "no")
        self.run_baton()
        self.baton("approve", "gate")
        self.run_baton()
        self.assertEqual({t: self.state.tasks[t].status for t in "bd"}, {"b": "done", "d": "done"})

    def test_decisions_need_a_waiting_checkpoint(self):
        self.two_phase_workspace()
        self.assertEqual(self.baton("approve", "gate"), 2)      # nothing ran yet
        self.assertEqual(self.baton("approve", "a"), 2)         # not a checkpoint
        self.assertEqual(self.baton("reject", "gate"), 2)       # a rejection needs its note
        self.assertFalse((self.run_dir / "state" / "decisions").exists())

    def test_only_refuses_a_task_behind_an_open_checkpoint(self):
        self.two_phase_workspace()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}, {"commit": "c.txt", "status": "done"},
                                 {"commit": "b.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.run_baton(only="b"), 5)
        self.assertEqual(self.run_baton(only="b", force=True), 0)
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_a_checkpoint_waits_for_the_tasks_before_it(self):
        self.add_task("a")
        self.add_gate()
        self.scenario_is(claude=[{"commit": "a.txt", "status": "partial"}])
        # a ends done here, then the checkpoint asks
        self.scenario_is(claude=[{"commit": "a.txt", "status": "done"}])
        self.run_baton()
        self.assertEqual(self.state.tasks["gate"].status, "awaiting")


class UserJourneyWarningTest(RunnerFixture):
    def put(self, tid, body, meta=""):
        (self.run_dir / "tasks" / f"{tid}.md").write_text(
            f'+++\nrepo = "../repo"\nwork_type = "feature"\n{meta}+++\n{body}\n')

    def warnings(self):
        _, warnings, _ = preflight(load_config(self.run_dir / "baton.toml"))
        return [w for w in warnings if "browser-smoke" not in w and "run.timezone" not in w]

    def test_ui_task_without_journey_or_reuse_warns(self):
        self.put("join", "Add `src/app/join/page.tsx` with a form.")
        [w] = self.warnings()
        self.assertIn("task join touches UI paths (src/app/join/page.tsx)", w)
        self.assertIn("neither", w)

    def test_a_journey_or_a_reuse_section_silences_it(self):
        self.put("j1", "Add src/app/j1/page.tsx.\n\n## User journey\nLogin, then the invitation page.")
        self.put("j2", "Add src/app/j2/page.tsx.\n\n## Reuse\nThe invitation page; reuse it.")
        self.assertEqual(self.warnings(), [])

    def test_empty_section_does_not_count(self):
        self.put("j1", "Add src/app/j1/page.tsx.\n\n## User journey\n\n## Reuse\n")
        self.assertEqual(len(self.warnings()), 1)

    def test_non_ui_task_is_left_alone(self):
        self.put("api", "Add `app/routers/plans.py` and a test in tests/test_plans.py.")
        self.assertEqual(self.warnings(), [])

    def test_front_matter_paths_count_and_globs_are_configurable(self):
        self.put("p", "Make the page.", meta='paths = ["web/pages/join.vue"]\n')
        self.assertEqual(self.warnings(), [])
        self.write_config(extra='[plan]\nui_paths = ["web/pages/**", "*.vue"]')
        self.assertEqual(len(self.warnings()), 1)

    def test_suffix_glob_matches_at_any_depth(self):
        self.put("t", "Edit components/deep/er/Card.tsx")
        self.assertEqual(len(self.warnings()), 1)


if __name__ == "__main__":
    unittest.main()
