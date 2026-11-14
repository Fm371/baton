"""SUMMARY.md as text for a state that exercises every part of it (pins the layout so the writer can be
restructured). Fake CLIs only; the clock, the version and the paths are fixed."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from baton import versioninfo
from baton.runner import Runner
from tests.test_operator import Fixture

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class SummaryGoldenTest(Fixture):
    def build(self):
        self.add_task("a")
        self.add_task("b", deps=["a"])
        self.add_task("c", deps=["b"])
        cfg = self.cfg()
        r = Runner(cfg, echo=False)
        r.ordered = self.ordered(cfg)
        r.clock = mock.Mock(now=lambda: NOW)
        r.state.baton_version = "9.9.9 (test)"
        a, b, c = (r.state.task(x) for x in "abc")
        a.status, a.commits, a.attempts, a.providers = "done", 2, 1, ["claude"]
        a.overridden_by, a.note = "operator", "marked done by operator: fine"
        b.status, b.commits, b.attempts, b.providers = "partial", 1, 2, ["claude", "codex"]
        b.note = "half | done"
        c.status, c.gated_by = "gated", "b"
        s = r.sched.status
        s["claude"].limited_until = NOW + timedelta(hours=2)
        s["claude"].model_limits = {"opus": NOW + timedelta(hours=1)}
        s["codex"].disabled = "auth error"
        r.stop_reason = "stopped by signal 15"
        return r

    def test_the_whole_text(self):
        r = self.build()
        with mock.patch.object(versioninfo, "describe", return_value="9.9.9 (test)"):
            text = r.summary_text(r.ordered, ended=True)
        self.assertEqual(text, GOLDEN.replace("{REPO}", r.ordered[0].repo.name))

    def test_lanes_section_and_available_except(self):
        r = self.build()
        r.sched.status["claude"].limited_until = None
        r.sched.status["codex"].disabled = None
        r.cfg.lanes = 2
        r.state.running = {"a": {"lane": "api", "provider": "claude"}, "b": {"lane": "web"}}
        text = r.summary_text(r.ordered, ended=False)
        self.assertIn("## Lanes\n\n- api: a on claude\n- web: b\n\n", text)
        self.assertIn("- claude: available except opus (until ", text)
        r.state.running = {}
        self.assertIn("## Lanes\n\n- none busy\n\n", r.summary_text(r.ordered, ended=False))


GOLDEN = (
    '# baton run summary · baton 9.9.9 (test)\n'
    '\n'
    'Updated 2026-11-01 15:30 +03:30 — stopped: stopped by signal 15\n'
    '\n'
    '| task | repo | status | commits | attempts | providers | note |\n'
    '|---|---|---|---|---|---|---|\n'
    '| a | repo | done | 2 | 1 | claude | marked done by operator: fine [set by operator] |\n'
    '| b | repo | partial | 1 | 2 | claude -> codex | half / done |\n'
    '| c | repo | gated | 0 | 0 | - | - |\n'
    '\n'
    '## Branches\n'
    '\n'
    '| task | repo | branch | commits | started from |\n'
    '|---|---|---|---|---|\n'
    '| a | repo | `feat/x` | 2 | develop |\n'
    '| b | repo | `feat/x` | 1 | develop |\n'
    '\n'
    'Branches are never merged or pushed by baton.\n'
    '\n'
    '## Providers\n'
    '\n'
    '- claude: limited until 2026-11-01 17:30 +03:30 (in 2h) (current)\n'
    '- codex: disabled (auth error)\n'
    '\n'
    '## Not verified\n'
    '\n'
    'QA starts here: these were checked only with mocks or fixtures, or not at all.\n'
    '\n'
    'Nothing declared.\n'
)

if __name__ == "__main__":
    unittest.main()
