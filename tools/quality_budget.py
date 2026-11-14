#!/usr/bin/env python3
"""A ratchet on function size and complexity (stdlib only).

For every function in baton/**/*.py it computes an approximate cyclomatic complexity (1 + branches) and its
length in lines. A function over the limits (complexity > 15 or more than 80 lines) is tolerated only while
it is listed in tools/quality_baseline.json at its recorded values. So:

  * a NEW function over a limit fails,
  * a listed function that got WORSE (more complex or longer) fails,
  * a listed function that got better or went away is fine (run --update to lock in the gain).

    python3 tools/quality_budget.py             # check (exit 1 on a violation)
    python3 tools/quality_budget.py --update    # rewrite the baseline; refuses to let any value rise
    python3 tools/quality_budget.py --update --force   # accept a rise (say why in the commit message)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

MAX_CC = 15
MAX_LINES = 80
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = Path(__file__).resolve().parent / "quality_baseline.json"

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _own_nodes(func: ast.AST):
    """The nodes of a function without those of the functions and classes defined inside it."""
    todo = list(ast.iter_child_nodes(func))
    while todo:
        node = todo.pop()
        yield node
        if isinstance(node, (*_FUNCS, ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(node))


def complexity(func: ast.AST) -> int:
    """1 + decision points: if/elif, loops, except, conditional expressions, boolean operators,
    comprehension loops and conditions, match cases, assert."""
    cc = 1
    for n in _own_nodes(func):
        if isinstance(n, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.Assert)):
            cc += 1
        elif isinstance(n, ast.BoolOp):
            cc += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            cc += 1 + len(n.ifs)
        elif isinstance(n, ast.match_case):
            cc += 1
    return cc


def measure_source(source: str, rel: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCS):
                name = ".".join([*scope, child.name])
                out[f"{rel}::{name}"] = {"cc": complexity(child),
                                         "lines": (child.end_lineno or child.lineno) - child.lineno + 1}
                visit(child, [*scope, child.name])
            elif isinstance(child, ast.ClassDef):
                visit(child, [*scope, child.name])
            else:
                visit(child, scope)

    visit(ast.parse(source), [])
    return out


def measure_tree(root: Path, package: str = "baton") -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for path in sorted((root / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.update(measure_source(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()))
    return out


def offenders(measured: dict[str, dict[str, int]], max_cc: int, max_lines: int) -> dict[str, dict[str, int]]:
    return {k: v for k, v in measured.items() if v["cc"] > max_cc or v["lines"] > max_lines}


def check(measured: dict, baseline: dict, max_cc: int = MAX_CC, max_lines: int = MAX_LINES) -> list[str]:
    """Violations, as human-readable lines (empty = within budget)."""
    problems = []
    for key, v in sorted(offenders(measured, max_cc, max_lines).items()):
        base = baseline.get(key)
        if base is None:
            problems.append(f"{key}: new function over the budget (complexity {v['cc']}, {v['lines']} lines; "
                            f"limits {max_cc} / {max_lines}). Split it.")
            continue
        worse = [f"{what} {base[what]} -> {v[what]}" for what in ("cc", "lines") if v[what] > base[what]]
        if worse:
            problems.append(f"{key}: got worse ({', '.join(worse)}). It is already over the budget; "
                            "do not make it bigger.")
    return problems


def improvements(measured: dict, baseline: dict, max_cc: int = MAX_CC, max_lines: int = MAX_LINES) -> list[str]:
    """Baselined functions that are now smaller, within the budget, or gone (a hint to run --update)."""
    still = offenders(measured, max_cc, max_lines)
    notes = []
    for key, base in sorted(baseline.items()):
        now = still.get(key)
        if now is None:
            notes.append(f"{key}: now within the budget or gone")
        elif now["cc"] < base["cc"] or now["lines"] < base["lines"]:
            notes.append(f"{key}: improved (cc {base['cc']} -> {now['cc']}, lines {base['lines']} -> {now['lines']})")
    return notes


def updated_baseline(measured: dict, baseline: dict, max_cc: int, max_lines: int, force: bool) -> dict:
    """The new baseline. Unless `force`, no value may rise and no new offender may appear."""
    new = offenders(measured, max_cc, max_lines)
    if not force:
        problems = check(measured, baseline, max_cc, max_lines)
        if problems:
            raise ValueError("refusing to raise the baseline (use --force to accept it):\n  " + "\n  ".join(problems))
    return dict(sorted(new.items()))


def load_baseline(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("functions", {})


def save_baseline(path: Path, functions: dict, max_cc: int, max_lines: int) -> None:
    doc = {"_comment": "Functions allowed to stay over the budget at these values; see tools/quality_budget.py. "
                       "Only ever lower them.",
           "max_cc": max_cc, "max_lines": max_lines, "functions": functions}
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--max-cc", type=int, default=MAX_CC)
    ap.add_argument("--max-lines", type=int, default=MAX_LINES)
    ap.add_argument("--update", action="store_true", help="rewrite the baseline from the current code")
    ap.add_argument("--force", action="store_true", help="with --update: accept values that rose")
    args = ap.parse_args(argv)

    measured = measure_tree(args.root)
    baseline = load_baseline(args.baseline)
    if args.update:
        try:
            first_time = not args.baseline.is_file()      # nothing to ratchet against yet: record the state as is
            new = updated_baseline(measured, baseline, args.max_cc, args.max_lines, args.force or first_time)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        save_baseline(args.baseline, new, args.max_cc, args.max_lines)
        print(f"baseline written: {len(new)} function(s) over the budget -> {args.baseline}")
        return 0
    problems = check(measured, baseline, args.max_cc, args.max_lines)
    for line in problems:
        print(f"FAIL {line}")
    if problems:
        print(f"\n{len(problems)} problem(s). Split the function, or (only if unavoidable) "
              "python3 tools/quality_budget.py --update --force.", file=sys.stderr)
        return 1
    notes = improvements(measured, baseline, args.max_cc, args.max_lines)
    for line in notes:
        print(f"note {line}")
    if notes:
        print("run `python3 tools/quality_budget.py --update` to lock these gains in")
    print(f"quality budget ok: {len(measured)} functions, {len(offenders(measured, args.max_cc, args.max_lines))} "
          f"over the limits, all at or below their baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
