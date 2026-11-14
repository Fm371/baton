"""Characterization of `baton watch`'s event rendering: pins today's output line by line so the
renderer can be restructured without changing what people see."""

import unittest

from baton.watch import render

GOLDEN = [('', []),
 ('   ', []),
 ('plain text line', ['plain text line']),
 ('[1, 2]', ['[1, 2]']),
 ('"a string"', ['"a string"']),
 ('{"type": "system", "subtype": "init", "session_id": "abcdef123456"}', ['· session abcdef12 started']),
 ('{"type": "system", "subtype": "other"}', []),
 ('{"type": "assistant", "message": {"content": [{"type": "text", "text": "hello   there\\nworld"}, {"type": '
  '"text", "text": "   "}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}, {"type": '
  '"tool_use", "name": "Read", "input": {"file_path": "/x/y.py"}}, {"type": "tool_use", "name": "Odd", '
  '"input": {"zzz": 1}}, {"type": "tool_use", "input": "not a dict"}, {"type": "thinking"}]}}',
  ['agent: hello there world', '▸ Bash: ls -la', '▸ Read: /x/y.py', '▸ Odd', '▸ tool']),
 ('{"type": "assistant", "message": null}', []),
 ('{"type": "user", "message": {"content": [{"type": "tool_result", "is_error": true, "content": "boom"}, '
  '{"type": "tool_result", "is_error": false, "content": "fine"}, "not a dict"]}}',
  ['  ✗ boom']),
 ('{"type": "result", "is_error": false, "result": "all done"}', ['✓ result: all done']),
 ('{"type": "result", "is_error": true, "result": "Invalid API key"}', ['✗ result: Invalid API key']),
 ('{"type": "thread.started", "thread_id": "0123456789abcdef"}', ['· thread 01234567 started']),
 ('{"type": "item.completed", "item": {"type": "agent_message", "text": "codex says hi"}}',
  ['agent: codex says hi']),
 ('{"type": "item.completed", "item": {"type": "reasoning", "text": "thinking hard"}}', ['· thinking hard']),
 ('{"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}}', ['▸ pytest -q']),
 ('{"type": "item.completed", "item": {"type": "command_execution", "exit_code": 2, "aggregated_output": "1 '
  'failed"}}',
  ['  ✗ exit 2: 1 failed']),
 ('{"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}}', []),
 ('{"type": "item.completed", "item": {"type": "command_execution"}}', []),
 ('{"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py"}, {"path": '
  '"b.py"}, {"path": "c.py"}, {"path": "d.py"}, {"path": "e.py"}, {"path": "f.py"}]}}',
  ['▸ edited a.py, b.py, c.py, d.py, e.py']),
 ('{"type": "item.started", "item": {"type": "agent_message"}}', []),
 ('{"type": "item.completed", "item": {"type": "web_search"}}', []),
 ('{"type": "error", "message": "rate limited"}', ['✗ rate limited']),
 ('{"type": "turn.failed", "error": {"message": "nope"}}', ['✗ {"message": "nope"}']),
 ('{"type": "turn.completed", "usage": {"output_tokens": 42}}', ['· turn done (42 output tokens)']),
 ('{"type": "turn.completed"}', ['· turn done (? output tokens)']),
 ('{"type": "something.else"}', []),
 ('{"type": "assistant", "message": {"content": [{"type": "text", "text": '
  '"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}]}}',
  ['agent: '
   'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…'])]


class RenderGoldenTest(unittest.TestCase):
    def test_every_event_renders_as_before(self):
        for event, expected in GOLDEN:
            with self.subTest(event=event[:60]):
                self.assertEqual(render(event + "\n"), expected)


if __name__ == "__main__":
    unittest.main()
