"""Run every test module in its own process, several at once.

    python3 -m tests.run_parallel [-j N]

The suite is dominated by subprocess start-up (git, the fake agents), so
processes scale well. Plain `python3 -m unittest discover -s tests -t .` still works.
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(module: str) -> tuple[str, int, str]:
    p = subprocess.run([sys.executable, "-m", "unittest", module], cwd=ROOT, capture_output=True, text=True)
    return module, p.returncode, p.stdout + p.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()
    modules = sorted(f"tests.{p.stem}" for p in (ROOT / "tests").glob("test_*.py"))
    failed = []
    with ThreadPoolExecutor(args.j) as pool:
        for module, rc, out in pool.map(run, modules):
            print(f"{'ok  ' if rc == 0 else 'FAIL'} {module}")
            if rc:
                failed.append(module)
                print(out)
    print(f"{len(modules) - len(failed)}/{len(modules)} modules passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
