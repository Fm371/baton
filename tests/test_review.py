"""`baton review` and `baton review-fix`: cross-provider read-only review and one round of fixes.
Fake CLIs and a real git repository, as in the runner tests."""

import contextlib
import fcntl
import io
import json
import re
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from baton import cli, reviewrun, reviewstate
from baton.config import load_config
from baton.runner import Runner
from baton.state import load_state, save_state
from baton.tasks import TaskError, load_tasks
from tests.test_runner import FakeClock, RunnerFixture, claude_limit, git

CHANGES = ("VERDICT: changes\n"
           "[blocker] a.txt:1 — the total is wrong\n"
           "[blocker] a.txt:2 — a check is missing\n"
           "[should] a.txt:3 — the helper is called twice\n"
           "[nit] a.txt:4 — typo\n")


def capture(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def codex_says(text, **extra):
    return {"stdout": [{"type": "item.completed", "item": {"type": "agent_message", "text": text}}], **extra}


def claude_says(text, **extra):
    return {"stdout": [{"type": "system", "subtype": "init", "session_id": "c-1"},
                       {"type": "result", "is_error": False, "result": text}], **extra}


class ReviewFixture(RunnerFixture):
    """Steps are appended per provider and the scenario file is rewritten whole, so the fake CLIs' call
    counters (one per provider) keep pointing at the right step."""

    def setUp(self):
        super().setUp()
        self.steps = {"claude": [], "codex": []}

    @property
    def cfg_path(self):
        return str(self.run_dir / "baton.toml")

    def step(self, prov, **step):
        self.steps[prov].append(step)
        self.scenario.write_text(json.dumps(self.steps))

    def calls_of(self, prov):
        return [c for c in self.calls() if c["provider"] == prov]

    def saved(self):
        return load_state(self.run_dir / "state" / "run.json")

    def implement(self, by="claude", tasks=("a",), extra="review = true\n"):
        """Run the task(s) to `done` with `by`; one implementer step per task."""
        if by == "codex":
            self.write_config(start_provider="codex")
        for t in tasks:
            self.add_task(t, extra=extra)
            if by == "claude":
                self.step("claude", commit=f"{t}.txt", status="done")
            else:
                self.step("codex", dirty=f"{t}.txt", status="done", commit_message=f"feat({t}): add {t}")
        self.assertEqual(self.run_baton(), 0)
        for t in tasks:
            self.assertEqual(self.state.tasks[t].status, "done")

    def review(self, *args):
        return capture(["review", *args, "-c", self.cfg_path])

    def fix(self, *args):
        return capture(["review-fix", *args, "-c", self.cfg_path])

    def limit(self, prov, hours=1, now=None):
        st = self.saved()
        until = (now or datetime.now(timezone.utc)) + timedelta(hours=hours)
        st.providers[prov] = {"limited_until": until.isoformat(), "window": "five_hour", "disabled": None,
                              "reset_known": True}
        save_state(self.run_dir / "state" / "run.json", st)

    def review_file(self, tid="a"):
        return self.run_dir / "reviews" / f"{tid}.md"

    def fake_clock(self):
        """review and review-fix on a fake clock, so waiting for a limit costs no time."""
        self.clock = FakeClock()

        def make(cfg, ordered, echo=True):
            r = Runner(cfg, clock=self.clock, echo=False)
            r.ordered = ordered
            r.verifiers = [t for t in ordered if t.is_verifier]
            return r
        for name in ("baton.reviewrun.new_runner", "baton.reviewfix.new_runner"):
            p = mock.patch(name, make)
            p.start()
            self.addCleanup(p.stop)

    def report_path(self, tid="a"):
        return str(self.run_dir / "results" / tid / "review-fix.md")


# ---------------------------------------------------------------- the review format


class ParseTest(unittest.TestCase):
    def test_a_valid_review_with_all_three_tags(self):
        p = reviewstate.parse_review(CHANGES)
        self.assertEqual(p.verdict, "changes")
        self.assertEqual([f.severity for f in p.findings], ["blocker", "blocker", "should", "nit"])
        self.assertEqual(p.findings[0].location, "a.txt:1")
        self.assertEqual(p.findings[0].reason, "the total is wrong")

    def test_case_whitespace_bullets_and_numbering_are_tolerated(self):
        p = reviewstate.parse_review("\n  verdict :  Approve  \n\n- [Should] x/y.py:10-12: reason one\n"
                                     "2) [NIT]  z.md:3 - typo\n")
        self.assertEqual(p.verdict, "approve")
        self.assertEqual([(f.severity, f.location, f.reason) for f in p.findings],
                         [("should", "x/y.py:10-12", "reason one"), ("nit", "z.md:3", "typo")])

    def test_a_finding_without_a_location_keeps_its_reason(self):
        f = reviewstate.parse_finding("[blocker] the whole approach is wrong")
        self.assertEqual((f.location, f.reason), ("", "the whole approach is wrong"))

    def test_reviews_that_do_not_parse_have_no_verdict(self):
        for text in ("", "looks fine to me", "Summary first\nVERDICT: approve", "VERDICT: maybe\n",
                     "VERDICT: approve please\n"):
            p = reviewstate.parse_review(text)
            self.assertIsNone(p.verdict, text)
            self.assertFalse(p.valid)
            self.assertTrue(p.problem)

    def test_a_saved_review_has_no_blank_lines_in_front_and_one_newline_at_the_end(self):
        self.assertEqual(reviewstate.normalised("\n\nVERDICT: reject\n[blocker] a:1 — x\n\n\n"),
                         "VERDICT: reject\n[blocker] a:1 — x\n")
        self.assertFalse(reviewstate.parse_review("Sure!\nVERDICT: reject\n").valid)    # chatter first: invalid


class FrontMatterTest(ReviewFixture):
    def test_review_defaults_to_false_and_is_a_strict_boolean(self):
        self.add_task("plain")
        self.add_task("on", extra="review = true\n")
        cfg = load_config(self.run_dir / "baton.toml")
        by_id = {t.id: t for t in load_tasks(cfg)}
        self.assertFalse(by_id["plain"].review)
        self.assertTrue(by_id["on"].review)
        self.add_task("bad", extra='review = "yes"\n')
        with self.assertRaisesRegex(TaskError, r"bad\.md: review must be true or false"):
            load_tasks(cfg)

    def test_old_state_files_load_and_a_done_task_remembers_its_implementer(self):
        self.implement("claude")
        self.assertEqual(self.saved().tasks["a"].implemented_by, "claude")
        raw = json.loads((self.run_dir / "state" / "run.json").read_text())
        for key in ("implemented_by", "review_verdict", "review_by", "review_at", "fix_round", "fix_state"):
            raw["tasks"]["a"].pop(key, None)
        (self.run_dir / "state" / "run.json").write_text(json.dumps(raw))
        ts = self.saved().tasks["a"]
        self.assertEqual((ts.implemented_by, ts.review_verdict, ts.fix_round), ("", "", 0))


# ---------------------------------------------------------------- who reviews


class ProviderSelectionTest(ReviewFixture):
    def test_the_other_provider_reviews_and_is_sandboxed_read_only(self):
        self.implement("claude")
        self.step("codex", **codex_says(CHANGES))
        rc, out, err = self.review("--only", "a")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("a: changes by codex (2 blocker, 1 should, 1 nit)", out)
        calls = self.calls_of("codex")
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(len(self.calls_of("claude")), 1)             # only the implementation

    def test_a_codex_implementer_is_reviewed_by_claude(self):
        self.implement("codex")
        self.step("claude", **claude_says("VERDICT: approve\n"))
        rc, out, _ = self.review("--only", "a")
        self.assertEqual(rc, 0)
        self.assertIn("a: approve by claude", out)
        self.assertEqual(len(self.calls_of("claude")), 1)
        self.assertEqual(len(self.calls_of("codex")), 1)

    def test_a_limited_reviewer_is_an_error_not_a_fallback(self):
        self.implement("claude")
        self.limit("codex")
        rc, out, err = self.review("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("codex is limited until", err)
        self.assertIn("does not fall back to claude", err)
        self.assertEqual(len(self.calls()), 1)                         # nothing but the implementation ran
        self.assertFalse(self.review_file().exists())

    def test_allow_same_lets_the_implementer_review_when_the_other_cannot(self):
        self.implement("claude")
        self.limit("codex")
        self.step("claude", **claude_says("VERDICT: approve\n"))
        rc, out, _ = self.review("--only", "a", "--allow-same")
        self.assertEqual(rc, 0)
        self.assertIn("a: approve by claude", out)
        self.assertEqual(self.saved().tasks["a"].review_by, "claude")

    def test_allow_same_still_prefers_the_other_provider(self):
        self.implement("claude")
        self.step("codex", **codex_says("VERDICT: approve\n"))
        rc, out, _ = self.review("--only", "a", "--allow-same")
        self.assertEqual(rc, 0)
        self.assertIn("by codex", out)

    def test_an_unknown_implementer_needs_allow_same(self):
        self.implement("claude")
        st = self.saved()
        st.tasks["a"].implemented_by, st.tasks["a"].providers = "", []
        save_state(self.run_dir / "state" / "run.json", st)
        rc, _, err = self.review("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("does not know which provider implemented", err)
        self.step("claude", **claude_says("VERDICT: approve\n"))
        self.assertEqual(self.review("--only", "a", "--allow-same")[0], 0)

    def test_no_other_provider_enabled(self):
        self.implement("claude")
        cfgp = self.run_dir / "baton.toml"
        cfgp.write_text(cfgp.read_text().replace('[providers.codex]\n', '[providers.codex]\nenabled = false\n'))
        rc, _, err = self.review("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("no provider other than claude is enabled", err)

    def test_only_done_implementation_tasks_are_reviewed(self):
        self.add_task("a")
        rc, _, err = self.review("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("a is pending; only a done task is reviewed", err)
        rc, _, err = self.review("--only", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("no task with id 'nope'", err)


class PendingTest(ReviewFixture):
    def test_pending_reviews_only_tasks_that_asked_for_it(self):
        self.add_task("a", extra="review = true\n")
        self.add_task("b")
        self.step("claude", commit="a.txt", status="done")
        self.step("claude", commit="b.txt", status="done")
        self.assertEqual(self.run_baton(), 0)
        self.step("codex", **codex_says("VERDICT: approve\n"))
        rc, out, _ = self.review("--pending")
        self.assertEqual(rc, 0)
        self.assertIn("a: approve by codex", out)
        self.assertNotIn("b:", out)
        self.assertEqual(self.saved().tasks["b"].review_verdict, "")
        rc, out, _ = self.review("--pending")
        self.assertEqual((rc, "nothing to review" in out), (0, True))
        self.assertEqual(len(self.calls_of("codex")), 1)

    def test_only_works_without_the_flag(self):
        self.implement("claude", extra="")
        self.step("codex", **codex_says("VERDICT: approve\n"))
        rc, out, _ = self.review("--only", "a")
        self.assertEqual(rc, 0)
        self.assertIn("a: approve by codex", out)

    def test_one_of_the_two_options_is_required(self):
        rc, _, err = self.review()
        self.assertEqual(rc, 2)
        self.assertIn("one of the arguments --only --pending is required", err)
        self.assertEqual(self.review("--only", "a", "--pending")[0], 2)


# ---------------------------------------------------------------- what the reviewer gets and writes


class ReviewFileTest(ReviewFixture):
    def test_the_verdict_and_findings_are_recorded(self):
        self.implement("claude")
        self.step("codex", **codex_says(CHANGES + "\n\n"))
        self.assertEqual(self.review("--only", "a")[0], 0)
        text = self.review_file().read_text()
        self.assertTrue(text.startswith("VERDICT: changes\n"))
        ts = self.saved().tasks["a"]
        self.assertEqual((ts.review_verdict, ts.review_by), ("changes", "codex"))
        self.assertTrue(ts.review_at)
        self.assertEqual(reviewstate.read_review(load_config(self.run_dir / "baton.toml"), "a").verdict, "changes")

    def test_a_review_that_does_not_parse_is_kept_as_written_and_marked_invalid(self):
        self.implement("claude")
        self.step("codex", **codex_says("Looks good to me, ship it."))
        rc, out, _ = self.review("--only", "a")
        self.assertEqual(rc, 0)
        self.assertIn("INVALID review", out)
        self.assertEqual(self.review_file().read_text(), "Looks good to me, ship it.")
        self.assertEqual(self.saved().tasks["a"].review_verdict, "invalid")

    def test_a_second_review_keeps_the_first_in_history(self):
        self.implement("claude")
        self.step("codex", **codex_says(CHANGES))
        self.step("codex", **codex_says("VERDICT: approve\n"))
        self.review("--only", "a")
        self.review("--only", "a")
        self.assertEqual(self.saved().tasks["a"].review_verdict, "approve")
        self.assertIn("VERDICT: changes", (self.run_dir / "reviews" / "history" / "a-1.md").read_text())

    def test_the_prompt_has_goal_acceptance_result_and_diff_and_never_the_verifiers(self):
        (self.run_dir / "tasks").mkdir(exist_ok=True)
        (self.run_dir / "tasks" / "a.md").write_text(
            '+++\nrepo = "../repo"\nwork_type = "feature"\nbranch = "feat/x"\nreview = true\n+++\n'
            "## Goal\nCount the widgets.\n\n## Acceptance\n- the count is 3\n\n## Notes\nprivate scratch\n")
        self.step("claude", commit="a.txt", status="done", content="the widget count is 3\n")
        self.assertEqual(self.run_baton(), 0)
        self.add_verifier("v", deps=("a",))
        self.step("codex", **codex_says("VERDICT: approve\n"))
        self.assertEqual(self.review("--only", "a")[0], 0)
        prompt = self.calls_of("codex")[0]["prompt"]
        self.assertIn("Count the widgets.", prompt)
        self.assertIn("the count is 3", prompt)
        self.assertNotIn("private scratch", prompt)
        self.assertIn("## Changed", prompt)                              # results/a.md
        self.assertIn("+the widget count is 3", prompt)                   # the diff
        self.assertIn("git diff develop...feat/x", prompt)
        self.assertIn("VERDICT: approve", prompt)
        self.assertNotIn("SECRET CANARY", prompt)
        self.assertNotIn("verifiers", prompt)

    def test_a_task_without_goal_sections_sends_its_whole_text(self):
        self.implement("claude")
        self.step("codex", **codex_says("VERDICT: approve\n"))
        self.review("--only", "a")
        self.assertIn("Build a.", self.calls_of("codex")[0]["prompt"])


class ClaudeReviewerTest(ReviewFixture):
    def test_claude_gets_deny_rules_hidden_paths_and_a_reviews_only_allow(self):
        self.implement("codex")
        self.add_verifier("v", deps=("a",))
        self.step("claude", **claude_says("VERDICT: approve\n"))
        self.assertEqual(self.review("--only", "a")[0], 0)
        argv = self.calls_of("claude")[0]["argv"]
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        deny, allow = settings["permissions"]["deny"], settings["permissions"]["allow"]
        top = self.repo.resolve()
        self.assertIn(f"Edit(/{top}/**)", deny)                                         # the repository
        self.assertIn(f"Edit(/{(self.run_dir / 'results').resolve()}/**)", deny)       # the baton folder ...
        self.assertNotIn(f"Edit(/{(self.run_dir / 'reviews').resolve()}/**)", deny)    # ... except reviews/
        self.assertIn(f"Edit(/{(self.run_dir / 'reviews').resolve()}/**)", allow)
        self.assertIn("Bash(git commit:*)", deny)
        self.assertIn("Bash(rm:*)", deny)
        self.assertIn(f"Read(/{(self.run_dir / 'verifiers').resolve()})", deny)         # verifiers/ stays hidden
        self.assertNotIn(str(self.run_dir / "verifiers"), argv)

    def test_a_bypass_permission_mode_is_not_used_for_a_review(self):
        self.implement("codex")
        cfgp = self.run_dir / "baton.toml"
        cfgp.write_text(cfgp.read_text().replace('[providers.claude]\n',
                                                 '[providers.claude]\npermission_mode = "bypassPermissions"\n'))
        self.step("claude", **claude_says("VERDICT: approve\n"))
        self.assertEqual(self.review("--only", "a")[0], 0)
        argv = self.calls_of("claude")[0]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")

    def test_the_file_claude_wrote_is_the_review(self):
        self.implement("codex")
        self.step("claude", write={str(self.review_file()): "VERDICT: reject\n[blocker] a.txt:1 — wrong\n"},
                  **claude_says("done"))
        rc, out, _ = self.review("--only", "a")
        self.assertEqual(rc, 0)
        self.assertIn("a: reject by claude", out)
        self.assertEqual(self.saved().tasks["a"].review_verdict, "reject")


# ---------------------------------------------------------------- read-only enforcement


class ReadOnlyTest(ReviewFixture):
    def assert_discarded(self, rc, err):
        self.assertEqual(rc, 3)
        self.assertIn("changed the repository", err)
        self.assertFalse(self.review_file().exists())
        self.assertEqual(self.saved().tasks["a"].review_verdict, "blocked")
        self.assertTrue((self.run_dir / "reviews" / "a.blocked.md").is_file())

    def test_a_reviewer_that_writes_a_file_is_discarded_and_nothing_is_cleaned_up(self):
        self.implement("claude")
        self.step("codex", dirty="evil.txt", **codex_says("VERDICT: approve\n"))
        rc, _, err = self.review("--only", "a")
        self.assert_discarded(rc, err)
        self.assertIn("status", err)
        self.assertTrue((self.repo / "evil.txt").exists())                # baton does not clean up

    def test_a_reviewer_that_commits_is_discarded(self):
        self.implement("claude")
        self.step("codex", commit="sneaky.txt", **codex_says("VERDICT: approve\n"))
        rc, _, err = self.review("--only", "a")
        self.assert_discarded(rc, err)
        self.assertIn("head", err)

    def test_a_reviewer_that_edits_an_already_dirty_file_is_caught(self):
        self.implement("claude")
        (self.repo / "README").write_text("user edit\n")
        self.step("codex", **codex_says("VERDICT: approve\n"))
        self.assertEqual(self.review("--only", "a")[0], 0)               # untouched dirt is fine ...
        self.step("codex", write={str(self.repo / "README"): "changed by the reviewer\n"},
                  **codex_says("VERDICT: approve\n"))
        rc, _, err = self.review("--only", "a")                           # ... editing it is not
        self.assertEqual(rc, 3)
        self.assertIn("tracked_changes", err)

    def test_an_unchanged_repository_passes(self):
        self.implement("claude")
        self.step("codex", **codex_says("VERDICT: approve\n"))
        before = reviewrun.tree_snapshot(self.repo)
        self.assertEqual(self.review("--only", "a")[0], 0)
        self.assertEqual(reviewrun.snapshot_difference(before, reviewrun.tree_snapshot(self.repo)), [])

    def test_a_codex_config_that_changes_the_sandbox_is_refused(self):
        self.implement("claude")
        cfgp = self.run_dir / "baton.toml"
        cfgp.write_text(cfgp.read_text().replace('[providers.codex]\n',
                                                 '[providers.codex]\nextra_args = ["--yolo"]\n'))
        rc, _, err = self.review("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("--yolo", err)
        self.assertEqual(len(self.calls_of("codex")), 0)

    def test_the_reviewer_hitting_its_limit_records_no_verdict(self):
        self.implement("codex")
        self.step("claude", **claude_limit())
        rc, _, err = self.review("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("claude hit its usage limit while reviewing", err)
        self.assertEqual(self.saved().tasks["a"].review_verdict, "")


# ---------------------------------------------------------------- reviews gate nothing, and only on request


class NeverAutomaticTest(ReviewFixture):
    def test_baton_run_never_starts_a_review(self):
        self.add_task("a", extra="review = true\n")
        self.add_task("b", deps=["a"], extra="review = true\n")
        self.step("claude", commit="a.txt", status="done")
        self.step("claude", commit="b.txt", status="done")
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.calls_of("codex"), [])
        self.assertFalse((self.run_dir / "reviews").exists())
        self.assertEqual(reviewstate.review_label(load_tasks(load_config(self.run_dir / "baton.toml"))[0],
                                                  self.saved().tasks["a"]), "pending")

    def test_no_scheduler_module_knows_about_reviews(self):
        src = Path(reviewrun.__file__).parent
        for name in ("runner.py", "lanes.py", "taskqueue.py", "scheduler.py", "process.py"):
            text = (src / name).read_text()
            self.assertNotRegex(text, r"reviewrun|reviewfix|review_verdict|fix_round", name)

    def test_a_reject_does_not_gate_a_dependent(self):
        self.add_task("a", extra="review = true\n")
        self.add_task("b", deps=["a"])
        self.step("claude", commit="a.txt", status="done")
        self.step("claude", commit="b.txt", status="done")
        self.assertEqual(self.run_baton(only="a"), 0)
        self.step("codex", **codex_says("VERDICT: reject\n[blocker] a.txt:1 — the approach is wrong\n"))
        self.assertEqual(self.review("--only", "a")[0], 0)
        self.assertEqual(self.saved().tasks["a"].review_verdict, "reject")
        self.assertEqual(self.run_baton(), 0)
        self.assertEqual(self.state.tasks["b"].status, "done")

    def test_a_running_baton_blocks_the_commands(self):
        self.implement("claude")
        d = self.run_dir / "state"
        f = (d / "lock").open("a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)
        self.assertEqual(self.review("--only", "a")[0], 4)
        self.assertEqual(self.fix("--only", "a")[0], 4)
        self.assertEqual(len(self.calls_of("codex")), 0)


# ---------------------------------------------------------------- plan and status


class VisibilityTest(ReviewFixture):
    def test_status_shows_review_and_fix_state(self):
        self.implement("claude")
        rc, out, _ = capture(["status", "-c", self.cfg_path])
        self.assertIn("a: review pending, fix not run", out)
        self.step("codex", **codex_says(CHANGES))
        self.review("--only", "a")
        rc, out, _ = capture(["status", "-c", self.cfg_path])
        self.assertIn("a: review changes by codex, fix not run", out)

    def test_plan_lists_the_opt_in_tasks(self):
        self.add_task("a", extra="review = true\n")
        self.add_task("b")
        rc, out, _ = capture(["plan", "-c", self.cfg_path])
        self.assertIn("a: review none, fix not run", out)
        self.assertNotIn("  b: review", out)
        self.assertIn("never run by `baton run`", out)

    def test_labels(self):
        class T:
            review = True
        ts = type("S", (), {"review_verdict": "", "status": "done", "fix_state": "", "fix_round": 0})()
        self.assertEqual(reviewstate.review_label(T, ts), "pending")
        ts.status = "pending"
        self.assertEqual(reviewstate.review_label(T, ts), "none")
        ts.review_verdict = "reject"
        self.assertEqual(reviewstate.review_label(T, ts), "reject")
        for state, label in (("", "not run"), ("done", "done"), ("blocked", "blocked"), ("running", "interrupted")):
            ts.fix_state = state
            self.assertEqual(reviewstate.fix_label(ts), label)


# ---------------------------------------------------------------- review-fix


class FixFixture(ReviewFixture):
    def reviewed(self, by="claude", review=CHANGES):
        """A done task by `by`, reviewed by the other provider."""
        self.implement(by)
        other = "codex" if by == "claude" else "claude"
        if other == "codex":
            self.step("codex", **codex_says(review))
        else:
            self.step("claude", **claude_says(review))
        self.assertEqual(self.review("--only", "a")[0], 0)

    def answers(self, extra=""):
        return {self.report_path(): "B1: fixed\nB2: declined: out of scope for this task\nS1: fixed\n" + extra}


class FixTest(FixFixture):
    def test_the_implementer_fixes_once_and_the_result_file_records_each_finding(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", write=self.answers())
        rc, out, err = self.fix("--only", "a")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("review fix round 1 done by claude", out)
        ts = self.saved().tasks["a"]
        self.assertEqual((ts.fix_round, ts.fix_state), (1, "done"))
        result = (self.run_dir / "results" / "a.md").read_text()
        self.assertIn("## Review fix (round 1, claude", result)
        self.assertIn("- [blocker] a.txt:1 — the total is wrong — fixed", result)
        self.assertIn("- [blocker] a.txt:2 — a check is missing — declined: out of scope for this task", result)
        self.assertIn("- [should] a.txt:3 — the helper is called twice — fixed", result)
        self.assertTrue(result.startswith("STATUS: done"))                 # the original result is intact
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "feat/x"), "claude: fix.txt")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")
        self.assertEqual(len(self.calls_of("codex")), 1)                   # the review only: no re-review

    def test_the_fixer_prompt_scopes_the_work(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", write=self.answers())
        self.fix("--only", "a")
        prompt = self.calls_of("claude")[1]["prompt"]
        self.assertIn("B1 [blocker] a.txt:1", prompt)
        self.assertIn("B2 [blocker] a.txt:2", prompt)
        self.assertIn("S1 [should] a.txt:3", prompt)
        self.assertNotIn("S2", prompt)
        self.assertNotIn("B3", prompt)
        self.assertIn("ignore", prompt)
        self.assertIn("DECLINE", prompt)
        self.assertIn("Never push", prompt)
        self.assertIn("COMMON RULES", prompt)
        self.assertIn("Stay inside the task's Goal", prompt)

    def test_a_codex_implementer_is_fixed_by_codex_and_baton_commits(self):
        self.reviewed("codex", review=CHANGES)
        self.step("codex", dirty="fix.txt", write={
            self.report_path(): "COMMIT: fix(a): handle both blockers\nB1: fixed\nB2: fixed\n"})
        rc, out, err = self.fix("--only", "a")
        self.assertEqual(rc, 0, err)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "feat/x"), "fix(a): handle both blockers")
        self.assertEqual(len(self.calls_of("claude")), 1)                  # the reviewer, once
        self.assertEqual(len(self.calls_of("codex")), 2)

    def test_the_fixer_never_pushes_and_the_branch_is_put_back(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", write=self.answers())
        self.fix("--only", "a")
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "remote"], capture_output=True,
                                        text=True).stdout.strip(), "")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "develop")


class FixRefusalsTest(FixFixture):
    def test_no_review(self):
        self.implement("claude")
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("a has no review", err)
        self.assertIn("baton review --only a", err)

    def test_a_review_without_blockers(self):
        self.reviewed("claude", review="VERDICT: approve\n[should] a.txt:1 — maybe\n[nit] a.txt:2 — typo\n")
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("no [blocker] findings", err)
        self.assertEqual(self.saved().tasks["a"].fix_round, 0)
        self.assertEqual(len(self.calls_of("claude")), 1)

    def test_an_invalid_or_blocked_review_is_not_acted_on(self):
        self.reviewed("claude", review="all good")
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("not usable", err)

    def test_a_second_round_needs_force(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", write=self.answers())
        self.assertEqual(self.fix("--only", "a")[0], 0)
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("Only one round is allowed; --force runs another", err)
        self.assertEqual(self.saved().tasks["a"].fix_round, 1)
        self.step("claude", commit="fix2.txt", write=self.answers())
        rc, out, _ = self.fix("--only", "a", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(self.saved().tasks["a"].fix_round, 2)
        self.assertIn("round 2", (self.run_dir / "results" / "a.md").read_text())

    def test_a_new_review_never_unlocks_a_second_round(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", write=self.answers())
        self.assertEqual(self.fix("--only", "a")[0], 0)
        self.step("codex", **codex_says(CHANGES))
        self.assertEqual(self.review("--only", "a")[0], 0)                 # a manual re-review is fine
        self.assertEqual(self.saved().tasks["a"].fix_round, 1)
        self.assertEqual(self.fix("--only", "a")[0], 2)

    def test_a_dirty_repository_is_refused(self):
        self.reviewed("claude")
        (self.repo / "scratch.txt").write_text("x\n")
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted changes", err)
        self.assertEqual(self.saved().tasks["a"].fix_round, 0)

    def test_an_unknown_implementer_is_refused_not_guessed(self):
        self.reviewed("claude")
        st = self.saved()
        st.tasks["a"].implemented_by, st.tasks["a"].providers = "", []
        save_state(self.run_dir / "state" / "run.json", st)
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("never given to the other provider", err)


class FixLimitTest(FixFixture):
    def test_a_limited_implementer_is_waited_for_never_replaced(self):
        self.reviewed("claude")
        self.fake_clock()
        self.limit("claude", hours=2, now=self.clock.t)
        self.step("claude", commit="fix.txt", write=self.answers())
        rc, out, err = self.fix("--only", "a")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(any(isinstance(s, datetime) for s in self.clock.slept))      # it slept until the reset
        self.assertEqual(len(self.calls_of("codex")), 1)                              # only the review
        self.assertEqual(len(self.calls_of("claude")), 2)

    def test_a_limit_during_the_fix_waits_and_continues_on_the_same_provider(self):
        self.reviewed("claude")
        self.fake_clock()
        self.step("claude", **claude_limit(reset="11:30pm (Asia/Tehran)"))
        self.step("claude", commit="fix.txt", write=self.answers())
        rc, _, err = self.fix("--only", "a")
        self.assertEqual((rc, err), (0, ""))
        prompts = [c["prompt"] for c in self.calls_of("claude")[1:]]
        self.assertEqual(len(prompts), 2)
        self.assertIn("hit its usage limit during an earlier attempt", prompts[1])
        self.assertEqual(len(self.calls_of("codex")), 1)

    def test_a_reset_too_far_away_is_blocked_and_the_round_is_not_used_up(self):
        self.reviewed("claude")
        self.write_config(max_sleep_hours=1)
        self.fake_clock()
        self.limit("claude", hours=5, now=self.clock.t)
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("more than 1h away", err)
        self.assertIn("does not use the other provider", err)
        ts = self.saved().tasks["a"]
        self.assertEqual((ts.fix_round, ts.fix_state), (0, "blocked"))
        self.assertEqual(len(self.calls_of("codex")), 1)
        self.assertEqual(len(self.calls_of("claude")), 1)


class FixOutcomeTest(FixFixture):
    def test_a_blocker_the_fixer_did_not_answer_is_a_problem(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", write={self.report_path(): "B1: fixed\n"})
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("did not report on B2", err)
        ts = self.saved().tasks["a"]
        self.assertEqual((ts.fix_round, ts.fix_state), (1, "blocked"))          # the work ran: the round is used
        result = (self.run_dir / "results" / "a.md").read_text()
        self.assertIn("NOT REPORTED by the fixer", result)
        self.assertIn("PROBLEM:", result)

    def test_claimed_fixes_without_a_commit_are_a_problem(self):
        self.reviewed("claude")
        self.step("claude", write=self.answers())
        rc, _, err = self.fix("--only", "a")
        self.assertEqual(rc, 3)
        self.assertIn("reports fixes but the branch has no new commit", err)

    def test_declining_everything_needs_no_commit(self):
        self.reviewed("claude")
        self.step("claude", write={self.report_path(): "B1: declined: not a bug\nB2: declined: out of scope\n"})
        rc, _, err = self.fix("--only", "a")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.saved().tasks["a"].fix_state, "done")

    def test_the_answers_are_also_read_from_the_final_message(self):
        self.reviewed("claude")
        self.step("claude", commit="fix.txt", **claude_says("B1: fixed\nB2: fixed\n"))
        rc, _, err = self.fix("--only", "a")
        self.assertEqual((rc, err), (0, ""))

    def test_parse_answers(self):
        from baton.reviewfix import parse_answers
        got = parse_answers("- B1: fixed\n`B2` - Declined — because of X\nS1: skipped\nnoise\nB3 fixed")
        self.assertEqual(got, {"B1": ("fixed", ""), "B2": ("declined", "because of X"), "S1": ("skipped", "")})


class CommandHelpTest(unittest.TestCase):
    def test_help_lists_both_commands(self):
        rc, out, _ = capture(["--help"])
        self.assertIn("review-fix", out)
        for cmd in ("review", "review-fix"):
            rc, out, _ = capture([cmd, "--help"])
            self.assertEqual(rc, 0)
            self.assertIn("-c CONFIG", out)
        rc, out, _ = capture(["review", "--help"])
        self.assertIn("--allow-same", out)
        rc, out, _ = capture(["review-fix", "--help"])
        self.assertIn("--force", out)
        self.assertIsNotNone(re.search(r"one round", out, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
