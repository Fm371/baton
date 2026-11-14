import tempfile
import textwrap
import unittest
from pathlib import Path

from baton.config import load_config
from baton.tasks import TaskError, load_tasks, order_tasks

CONFIG = """
[run]
default_base = "develop"
[providers.claude]
bin = "claude"
[providers.codex]
bin = "codex"
[routing.rungs.r1]
claude = { model = "haiku" }
codex = { model = "luna" }
[routing.rungs.r3]
claude = { model = "sonnet", effort = "medium" }
codex = { model = "sol", effort = "medium" }
[routing.work_types]
docs = "r1"
feature = "r3"
"""


def task(meta: str, body: str = "Do the thing.") -> str:
    return f"+++\n{textwrap.dedent(meta).strip()}\n+++\n{body}\n"


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "baton.toml").write_text(CONFIG)
        (self.d / "tasks").mkdir()
        self.cfg = load_config(self.d / "baton.toml")

    def put(self, name, text):
        (self.d / "tasks" / name).write_text(text)

    def test_parse_and_defaults(self):
        self.put("10-api.md", task('repo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"'))
        [t] = load_tasks(self.cfg)
        self.assertEqual(t.id, "10-api")
        self.assertEqual(t.repo, (self.d.parent / "repo").resolve())
        self.assertEqual(t.resolved_rung, "r3")
        self.assertEqual(t.base, "develop")
        self.assertEqual(t.body, "Do the thing.")

    def test_task_base_overrides_default_base(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/a"\nbase = "dev"'))
        self.put("b.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/b"'))
        by_id = {t.id: t for t in load_tasks(self.cfg)}
        self.assertEqual(by_id["a"].base, "dev")
        self.assertEqual(by_id["b"].base, "develop")

    def verifier(self, name, meta):
        (self.d / "verifiers").mkdir(exist_ok=True)
        (self.d / "verifiers" / name).write_text(task('kind = "verify"\n' + meta, "Check it."))

    def test_verifier_branches_from_its_dependency(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/a"'))
        self.verifier("va.md", 'repo = "r"\nrung = "r1"\ndepends_on = ["a"]')
        by_id = {t.id: t for t in load_tasks(self.cfg)}
        v = by_id["va"]
        self.assertTrue(v.is_verifier)
        self.assertFalse(by_id["a"].is_verifier)
        self.assertEqual((v.branch, v.base), ("verify/va", "feat/a"))

    def test_verifier_dependency_in_other_repo_keeps_default_base(self):
        self.put("a.md", task('repo = "other"\nrung = "r1"\nbranch = "feat/a"'))
        self.verifier("va.md", 'repo = "r"\nrung = "r1"\ndepends_on = ["a"]')
        v = {t.id: t for t in load_tasks(self.cfg)}["va"]
        self.assertEqual(v.base, "develop")

    def test_verifier_with_deps_on_two_branches_needs_explicit_base(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/a"'))
        self.put("b.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/b"'))
        self.verifier("v.md", 'repo = "r"\nrung = "r1"\ndepends_on = ["a", "b"]')
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertIn("different branches", str(e.exception))
        self.verifier("v.md", 'repo = "r"\nrung = "r1"\ndepends_on = ["a", "b"]\nbase = "feat/b"')
        v = {t.id: t for t in load_tasks(self.cfg)}["v"]
        self.assertEqual(v.base, "feat/b")

    def test_verifier_with_deps_on_one_branch_is_fine(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/x"'))
        self.put("b.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/x"\ndepends_on = ["a"]'))
        self.verifier("v.md", 'repo = "r"\nrung = "r1"\ndepends_on = ["a", "b"]')
        self.assertEqual({t.id: t for t in load_tasks(self.cfg)}["v"].base, "feat/x")

    def test_kind_must_match_folder(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nkind = "verify"'))
        (self.d / "verifiers").mkdir()
        (self.d / "verifiers" / "v.md").write_text(task('repo = "r"\nrung = "r1"'))
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertIn("a.md: kind = \"verify\" files belong in the verifiers folder", str(e.exception))
        self.assertIn("v.md: files in the verifiers folder need kind = \"verify\"", str(e.exception))

    def test_tasks_may_depend_on_verifiers(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/a"'))
        self.put("b.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/a"\ndepends_on = ["va"]'))
        self.verifier("va.md", 'repo = "r"\nrung = "r1"\ndepends_on = ["a"]')
        self.assertEqual([t.id for t in order_tasks(load_tasks(self.cfg))], ["a", "va", "b"])

    def test_providers_header(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nproviders = ["claude"]'))
        self.put("b.md", task('repo = "r"\nrung = "r1"'))
        self.verifier("v.md", 'repo = "r"\nrung = "r1"')
        by_id = {t.id: t for t in load_tasks(self.cfg)}
        self.assertEqual(by_id["a"].providers, ["claude"])
        self.assertIsNone(by_id["b"].providers)
        self.assertEqual(by_id["v"].providers, ["claude"])      # run.verifier_providers default
        self.put("c.md", task('repo = "r"\nrung = "r1"\nproviders = ["gemini"]'))
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertIn("providers must be a non-empty list", str(e.exception))

    def test_errors_are_collected(self):
        self.put("a.md", task('repo = "r"\nwork_type = "nope"'))
        self.put("b.md", "no front matter")
        self.put("c.md", task('repo = "r"\nwork_type = "docs"\ncolour = "red"'))
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        msg = str(e.exception)
        self.assertIn("unknown work_type 'nope'", msg)
        self.assertIn("b.md", msg)
        self.assertIn("colour", msg)

    def test_skips_underscore_and_readme(self):
        self.put("_draft.md", "junk")
        self.put("README.md", "junk")
        self.put("a.md", task('repo = "r"\nrung = "r1"'))
        self.assertEqual([t.id for t in load_tasks(self.cfg)], ["a"])

    def test_order_deps_then_priority(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\ndepends_on = ["c"]\npriority = 50'))
        self.put("b.md", task('repo = "r"\nrung = "r1"\npriority = 5'))
        self.put("c.md", task('repo = "r"\nrung = "r1"'))
        self.put("d.md", task('repo = "r"\nrung = "r1"\npriority = 9'))
        self.assertEqual([t.id for t in order_tasks(load_tasks(self.cfg))], ["d", "b", "c", "a"])

    def test_cycle_and_unknown_dep(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\ndepends_on = ["b"]'))
        self.put("b.md", task('repo = "r"\nrung = "r1"\ndepends_on = "a"'))
        with self.assertRaises(TaskError) as e:
            order_tasks(load_tasks(self.cfg))
        self.assertIn("cycle", str(e.exception))
        self.put("b.md", task('repo = "r"\nrung = "r1"\ndepends_on = "zzz"'))
        with self.assertRaises(TaskError):
            order_tasks(load_tasks(self.cfg))

    # ---- sections, checkpoints and templates

    def test_sections_are_found_by_heading_at_any_level(self):
        body = ("Intro.\n\n## User journey\n1. Open the link.\n2. Log in.\n\n### Detail\nmore\n\n"
                "## Reuse\nThe invitations page.\n\n## Acceptance\n- a\n")
        self.put("a.md", task('repo = "r"\nrung = "r1"', body))
        [t] = load_tasks(self.cfg)
        self.assertEqual(t.section("user journey"), "1. Open the link.\n2. Log in.\n\n### Detail\nmore")
        self.assertEqual(t.section("Reuse"), "The invitations page.")
        self.assertIsNone(t.section("Missing"))

    def test_an_empty_section_is_no_section(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"', "## Reuse\n\n## Acceptance\n- a\n"))
        self.assertIsNone(load_tasks(self.cfg)[0].section("Reuse"))

    def test_approve_task_needs_neither_repo_nor_rung_but_a_body(self):
        self.put("gate.md", task('kind = "approve"\ndepends_on = ["a"]', "Is it right?"))
        [t] = load_tasks(self.cfg)
        self.assertTrue(t.is_approval)
        self.assertFalse(t.runs_agent)
        self.assertEqual((t.branch, t.resolved_rung), (None, ""))
        self.put("gate.md", task('kind = "approve"', ""))
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertIn("body (prompt) is empty", str(e.exception))

    def test_approve_belongs_in_the_tasks_folder(self):
        (self.d / "verifiers").mkdir()
        (self.d / "verifiers" / "g.md").write_text(task('kind = "approve"', "Is it right?"))
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertIn("kind = \"approve\" files belong in the tasks folder", str(e.exception))

    def test_template_verifier_runs_in_baton(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nbranch = "feat/a"'))
        (self.d / "verifiers").mkdir()
        (self.d / "verifiers" / "smoke.md").write_text(
            '+++\nkind = "verify"\ntemplate = "browser-smoke"\ndepends_on = ["a"]\n'
            '[[params.routes]]\npath = "/join"\n+++\n')
        v = {t.id: t for t in load_tasks(self.cfg)}["smoke"]
        self.assertTrue(v.is_verifier and not v.runs_agent)
        self.assertEqual(v.params, {"routes": [{"path": "/join"}]})
        self.assertEqual((v.branch, v.providers), (None, None))     # no branch, no provider

    def test_template_must_be_known_and_belong_to_a_verifier(self):
        (self.d / "verifiers").mkdir()
        (self.d / "verifiers" / "v.md").write_text('+++\nkind = "verify"\ntemplate = "nope"\n+++\n')
        self.put("a.md", task('repo = "r"\nrung = "r1"\ntemplate = "env-preflight"'))
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertEqual(str(e.exception).count("template must be one of env-preflight"), 2)

    def test_a_verifier_without_a_template_still_needs_repo_and_body(self):
        (self.d / "verifiers").mkdir()
        (self.d / "verifiers" / "v.md").write_text('+++\nkind = "verify"\n+++\nCheck it.\n')
        with self.assertRaises(TaskError) as e:
            load_tasks(self.cfg)
        self.assertIn("missing repo", str(e.exception))

    def test_feature_and_paths_headers(self):
        self.put("a.md", task('repo = "r"\nrung = "r1"\nfeature = "onboarding"\npaths = ["src/app/**"]'))
        [t] = load_tasks(self.cfg)
        self.assertEqual((t.feature, t.paths), ("onboarding", ["src/app/**"]))
        self.put("a.md", task('repo = "r"\nrung = "r1"\npaths = "src"'))
        with self.assertRaises(TaskError):
            load_tasks(self.cfg)


if __name__ == "__main__":
    unittest.main()
