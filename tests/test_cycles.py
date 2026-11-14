"""A dependency cycle, including one through a check baton added, fails the plan with the cycle spelled out."""

from baton.config import load_config
from baton.preflight import preflight
from baton.tasks import TaskError, load_tasks, order_tasks
from tests.test_runner import RunnerFixture


class CycleTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        api = self.root / "api"
        api.mkdir()
        (api / ".env.example").write_text("MONGO_URL=\n")
        (api / ".env").write_text("MONGO_URL=mongodb://localhost/db\n")
        self.write_config(extra='[preflight]\nrepos = ["../api"]\n')

    def gate(self, deps=("a",)):
        (self.run_dir / "tasks" / "gate.md").write_text(
            f'+++\nkind = "approve"\nfeature = "F06A"\ndepends_on = {list(deps)}\n+++\nLooks right?\n'.replace("'", '"'))

    def plan(self):
        return preflight(load_config(self.run_dir / "baton.toml"))

    def test_a_checkpoint_with_a_feature_does_not_deadlock_on_the_auto_check(self):
        self.add_task("a", extra='feature = "F06A"\n')
        self.gate()
        self.add_task("b", deps=["gate"], extra='feature = "F06A"\n')
        self.add_task("c", deps=["b"], extra='feature = "F06A"\n')
        errors, _, tasks = self.plan()
        self.assertEqual(errors, [])
        by_id = {t.id: t for t in tasks}
        self.assertEqual(by_id["env-preflight-F06A"].depends_on, ["a"])
        order = [t.id for t in tasks]
        self.assertLess(order.index("env-preflight-F06A"), order.index("gate"))
        self.assertLess(order.index("gate"), order.index("b"))

    def test_a_cycle_through_the_auto_check_is_spelled_out(self):
        # `late` waits for the auto check, which waits for the last task of the feature: `late` itself
        self.add_task("a", extra='feature = "F06A"\n')
        self.add_task("late", deps=["env-preflight-F06A"], extra='feature = "F06A"\n')
        errors, _, _ = self.plan()
        msg = "\n".join(errors)
        self.assertIn("dependency cycle: ", msg)
        self.assertIn("env-preflight-F06A (auto-added check) -> late -> env-preflight-F06A (auto-added check)", msg)
        self.assertIn("each task waits for the next", msg)
        self.assertIn("[preflight] auto = false", msg)

    def test_a_plain_cycle_names_its_members_in_order(self):
        self.add_task("a", deps=["c"])
        self.add_task("b", deps=["a"])
        self.add_task("c", deps=["b"])
        cfg = load_config(self.run_dir / "baton.toml")
        with self.assertRaises(TaskError) as e:
            order_tasks(load_tasks(cfg))
        self.assertIn("dependency cycle: a -> c -> b -> a", str(e.exception))
