"""A verifier with when = "feature-end" runs once at the end of its feature; more than one money gate warns."""

from baton.config import load_config
from baton.preflight import plan_text, preflight
from baton.tasks import TaskError, load_tasks
from tests.test_runner import RunnerFixture

F = 'feature = "pay"\n'


class FeatureEndTest(RunnerFixture):
    def plan(self):
        cfg = load_config(self.run_dir / "baton.toml")
        errors, warnings, tasks = preflight(cfg)
        return cfg, errors, warnings, tasks

    def gate_workspace(self, vextra="", n_extra=""):
        self.add_task("a", extra=F)
        self.add_task("b", deps=["gate"], extra=F)          # gated by the verifier unless it runs at the end
        self.add_task("c", deps=["b"], extra=F)
        self.add_verifier("gate", deps=("a",), extra=F + 'when = "feature-end"\n' + vextra)
        self.add_task("n", deps=["gate"], extra=n_extra)    # outside the feature: still gated

    def test_dependencies_are_rewritten(self):
        self.gate_workspace()
        cfg, errors, _, tasks = self.plan()
        self.assertEqual(errors, [])
        by_id = {t.id: t for t in tasks}
        self.assertEqual(by_id["gate"].depends_on, ["a", "b", "c"])
        self.assertEqual(by_id["b"].depends_on, ["a"])             # no longer held by the gate
        self.assertEqual(by_id["n"].depends_on, ["gate"])          # another feature's task keeps it
        ids = [t.id for t in tasks]
        self.assertLess(ids.index("c"), ids.index("gate"))
        self.assertLess(ids.index("gate"), ids.index("n"))
        self.assertIn("gate  [verify, at the end of pay]", plan_text(cfg, tasks))

    def test_it_runs_once_after_all_feature_tasks_and_then_gates_the_next_feature(self):
        self.gate_workspace()
        self.scenario_is(claude=[{"commit": f"{x}.txt", "status": "done"} for x in "abc"]
                         + [{"commit": "test_pay.py", "status": "done"}, {"commit": "n.txt", "status": "done"}])
        self.assertEqual(self.run_baton(), 0)
        order = [next(w for w in ("Build a.", "Build b.", "Build c.", "check rounding", "Build n.") if w in c["prompt"])
                 for c in self.calls()]
        self.assertEqual(order, ["Build a.", "Build b.", "Build c.", "check rounding", "Build n."])
        self.assertEqual({t: s.status for t, s in self.state.tasks.items()},
                         {t: "done" for t in ("a", "b", "c", "gate", "n")})

    def test_a_blocked_gate_still_holds_the_next_feature_but_not_the_feature_itself(self):
        self.gate_workspace()
        self.scenario_is(claude=[{"commit": f"{x}.txt", "status": "done"} for x in "abc"]
                         + [{"commit": "test_pay.py", "status": "blocked"}])
        self.run_baton()
        st = self.state.tasks
        self.assertEqual((st["a"].status, st["b"].status, st["c"].status), ("done", "done", "done"))
        self.assertEqual(st["gate"].status, "blocked")
        self.assertEqual(st["n"].status, "gated")

    def test_work_after_a_checkpoint_is_not_waited_for(self):
        self.add_task("a", extra=F)
        (self.run_dir / "tasks" / "ok.md").write_text(
            '+++\nkind = "approve"\nfeature = "pay"\ndepends_on = ["a"]\n+++\nLooks right?\n')
        self.add_task("after", deps=["ok"], extra=F)
        self.add_verifier("gate", deps=("a",), extra=F + 'when = "feature-end"\n')
        _, errors, _, tasks = self.plan()
        self.assertEqual(errors, [])
        self.assertEqual({t.id: t.depends_on for t in tasks}["gate"], ["a"])

    def test_other_verifiers_and_checks_in_the_feature_are_not_waited_for(self):
        self.add_task("a", extra=F)
        self.add_verifier("other", deps=("a",), extra=F)
        self.add_verifier("gate", deps=("a",), extra=F + 'when = "feature-end"\n')
        _, _, _, tasks = self.plan()
        self.assertEqual({t.id: t.depends_on for t in tasks}["gate"], ["a"])

    def test_front_matter_is_checked(self):
        self.add_task("a", extra=F)
        self.add_verifier("gate", extra='when = "feature-end"\n')
        with self.assertRaises(TaskError) as e:
            load_tasks(load_config(self.run_dir / "baton.toml"))
        self.assertIn("needs a feature", str(e.exception))
        self.add_verifier("gate", extra=F + 'when = "sometimes"\n')
        with self.assertRaises(TaskError) as e:
            load_tasks(load_config(self.run_dir / "baton.toml"))
        self.assertIn('when must be "feature-end"', str(e.exception))
        self.add_verifier("gate", extra=F)
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nwhen = "feature-end"\n+++\nx\n')
        with self.assertRaises(TaskError):
            load_tasks(load_config(self.run_dir / "baton.toml"))


class MoneyGateTest(RunnerFixture):
    def warnings(self):
        return preflight(load_config(self.run_dir / "baton.toml"))[1]

    def money(self, vid, feature="pay"):
        self.add_verifier(vid, deps=("a",), extra=f'feature = "{feature}"\nmoney_gate = true\n')

    def test_more_than_one_money_gate_in_a_feature_warns(self):
        self.add_task("a")
        self.money("m1")
        self.money("m2")
        [w] = [w for w in self.warnings() if "money gate" in w]
        self.assertIn("feature pay has 2 money gates (m1, m2)", w)
        self.assertIn('when = "feature-end"', w)

    def test_one_per_feature_is_fine(self):
        self.add_task("a")
        self.money("m1")
        self.money("m2", feature="refund")
        self.assertFalse([w for w in self.warnings() if "money gate" in w])

    def test_money_gate_belongs_on_a_verifier(self):
        (self.run_dir / "tasks").mkdir(exist_ok=True)
        self.add_task("a", extra="money_gate = true\n")
        with self.assertRaises(TaskError):
            load_tasks(load_config(self.run_dir / "baton.toml"))
