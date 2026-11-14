"""`baton watch`: a readable live view of the current attempt."""

import contextlib
import io
import json
import os
import time

from baton import cli
from baton.config import load_config
from baton.watch import describe, latest_attempt, render, watch
from tests.test_runner import RunnerFixture

CLAUDE_TEXT = {"type": "assistant", "message": {"content": [
    {"type": "text", "text": "I will add the route."},
    {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}}
CLAUDE_ERR = {"type": "user", "message": {"content": [
    {"type": "tool_result", "is_error": True, "content": "permission denied"}]}}


def j(ev):
    return json.dumps(ev)


class RenderTest(RunnerFixture):
    def test_claude_events(self):
        self.assertEqual(render(j(CLAUDE_TEXT)), ["agent: I will add the route.", "▸ Bash: git status"])
        self.assertEqual(render(j(CLAUDE_ERR)), ["  ✗ permission denied"])
        self.assertEqual(render(j({"type": "result", "is_error": False, "result": "done"})), ["✓ result: done"])
        self.assertEqual(render(j({"type": "result", "is_error": True, "result": "You've hit your limit"})),
                         ["✗ result: You've hit your limit"])

    def test_codex_events(self):
        item = lambda kind, **it: j({"type": kind, "item": it})
        self.assertEqual(render(item("item.completed", type="agent_message", text="Done.")), ["agent: Done."])
        self.assertEqual(render(item("item.started", type="command_execution", command="npm test")), ["▸ npm test"])
        self.assertEqual(render(item("item.completed", type="command_execution", command="npm test",
                                     exit_code=1, aggregated_output="1 failed")), ["  ✗ exit 1: 1 failed"])
        self.assertEqual(render(item("item.completed", type="command_execution", exit_code=0)), [])
        self.assertEqual(render(j({"type": "turn.failed", "error": {"message": "boom"}})), ['✗ {"message": "boom"}'])

    def test_plain_text_is_kept_and_blank_lines_dropped(self):
        self.assertEqual(render("bwrap: something"), ["bwrap: something"])
        self.assertEqual(render("   "), [])

    def test_long_text_is_clipped_to_one_line(self):
        [line] = render(j({"type": "item.completed", "item": {"type": "agent_message", "text": "word " * 200}}))
        self.assertLessEqual(len(line), 310)
        self.assertTrue(line.endswith("…"))


class WatchTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.cfg = load_config(self.run_dir / "baton.toml")
        self.out = io.StringIO()

    def attempt(self, task, n, provider, events, mtime=None):
        d = self.cfg.results_dir / task
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"attempt-{n:02d}.{provider}.out"
        p.write_text("".join(j(e) + "\n" for e in events))
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_no_output_yet(self):
        self.assertEqual(watch(self.cfg, self.out, follow=False), 1)
        self.assertIn("no attempt output yet", self.out.getvalue())

    def test_prints_the_tail_of_the_newest_attempt_without_following(self):
        self.attempt("a", 1, "claude", [CLAUDE_TEXT], mtime=1000)
        self.attempt("a", 2, "codex", [{"type": "item.completed", "item": {"type": "agent_message", "text": f"step {i}"}}
                                       for i in range(10)], mtime=2000)
        self.assertEqual(watch(self.cfg, self.out, follow=False, lines=3), 0)
        text = self.out.getvalue()
        self.assertIn("task a, attempt 2 on codex", text)
        self.assertEqual(text.splitlines()[1:], ["agent: step 7", "agent: step 8", "agent: step 9"])

    def test_the_running_task_wins_over_a_newer_file_of_another_task(self):
        self.attempt("a", 1, "claude", [CLAUDE_TEXT], mtime=1000)
        self.attempt("b", 1, "claude", [CLAUDE_TEXT], mtime=2000)
        (self.run_dir / "state").mkdir(exist_ok=True)
        (self.run_dir / "state" / "run.json").write_text(json.dumps(
            {"current": "claude", "providers": {}, "tasks": {"a": {"status": "running"}, "b": {"status": "done"}}}))
        self.assertEqual(describe(latest_attempt(self.cfg)), "task a, attempt 1 on claude")

    def test_follows_new_lines_then_the_next_attempt_and_stops_when_the_run_ends(self):
        p1 = self.attempt("a", 1, "claude", [CLAUDE_TEXT], mtime=1000)
        ticks = []

        def sleep(_):
            ticks.append(1)
            n = len(ticks)
            if n == 1:      # the agent writes more
                with p1.open("a") as f:
                    f.write(j({"type": "result", "is_error": False, "result": "first attempt done"}) + "\n")
            elif n == 2:    # a new attempt starts, on the other provider
                self.attempt("a", 2, "codex", [{"type": "item.completed",
                                                "item": {"type": "agent_message", "text": "continuing"}}],
                             mtime=time.time() + 100)

        active = lambda: len(ticks) < 4
        self.assertEqual(watch(self.cfg, self.out, poll=0, sleep=sleep, active=active), 0)
        lines = self.out.getvalue().splitlines()
        self.assertIn("agent: I will add the route.", lines)
        self.assertIn("✓ result: first attempt done", lines)
        self.assertIn("agent: continuing", lines)
        self.assertTrue(any("attempt 2 on codex" in ln for ln in lines))
        self.assertEqual(lines[-1], "== the run is not active ==")

    def test_raw_mode_and_the_command(self):
        self.attempt("a", 1, "claude", [CLAUDE_TEXT])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["watch", "-c", str(self.run_dir / "baton.toml"), "--no-follow", "--raw"]), 0)
        self.assertIn('"tool_use"', buf.getvalue())
