"""A stand-in for `claude` and `codex` that replays a scripted scenario.

Usage (from a tiny wrapper script):  python3 fake_agent.py <claude|codex> [args...]

BATON_FAKE_SCENARIO points to a JSON file:
  {"claude": [step, ...], "codex": [step, ...]}
Each call takes the next step of its provider. A step may contain:
  stdout   list of lines (strings, or objects written as JSON)
  stderr   string
  exit     exit code (default 0)
  status   "done" | "partial" | "blocked" -> write the result file
  commit_message  add a COMMIT line to the result file
  result_extra  text appended to the result file
  content  text to write into the commit / dirty file (default: a call counter line)
  commit   file name to create and commit in the working directory
  dirty    file name to create without committing
  write    {absolute path: text}: write those files (a reviewer's review, a fixer's report)
  sleep    seconds to sleep
  touch    path of a file to create (a test uses it to act while the run is busy)
  touch_ready path created after progress/output are flushed, just before hang
  progress text written to the progress file the prompt names
  bwrap    true: print the bwrap sandbox error as a command's output (a broken sandbox)
  snapshot file name: copy results/SUMMARY.md there while the call runs
  span     path: append "<start|end> <unix time> <cwd name> <provider>" around the call (overlap checks)
  wait_for path: wait (up to 30 s) until that file exists before doing anything else (a barrier)
  hang     seconds to sleep at the very end, after the files are written (a stop arrives while it is busy)
  ignore_term true: ignore SIGTERM (an agent that has to be SIGKILLed)
  spawn    path: start a `sleep 300` in the agent's process group and write its pid there (a leftover process)
Every call is recorded in <scenario>.calls.jsonl (argv, cwd, prompt).
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time

provider = sys.argv[1]
argv = sys.argv[2:]
if "--help" in argv:
    print("--output-format --effort --model --settings --permission-mode --max-turns --add-dir")
    sys.exit(0)

if argv[:1] == ["sandbox"]:
    # the sandbox probe: BATON_FAKE_SANDBOX=broken | unknown (default: works)
    mode = os.environ.get("BATON_FAKE_SANDBOX", "ok")
    if mode == "broken":
        print("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", file=sys.stderr)
        sys.exit(1)
    if mode == "unknown":
        print("error: unrecognized subcommand 'sandbox'", file=sys.stderr)
        sys.exit(2)
    if mode in ("net-ok", "net-blocked") and "--" in argv:
        # `codex sandbox [-c ...] -- program args`: run the program, or pretend the sandbox has no network
        rest = argv[argv.index("--") + 1:]
        if mode == "net-blocked" and rest[:1] != ["true"]:
            print("BLOCKED:[Errno 1] Operation not permitted")
            sys.exit(3)
        sys.exit(subprocess.run(rest).returncode)
    sys.exit(0)

scenario_path = os.environ["BATON_FAKE_SCENARIO"]
prompt = sys.stdin.read()
counter_path = scenario_path + f".{provider}.n"
with open(counter_path + ".lock", "w") as lock:        # parallel lanes may call at the same moment
    fcntl.flock(lock, fcntl.LOCK_EX)
    n = int(open(counter_path).read()) if os.path.exists(counter_path) else 0
    open(counter_path, "w").write(str(n + 1))
with open(scenario_path + ".calls.jsonl", "a") as f:
    f.write(json.dumps({"provider": provider, "argv": argv, "cwd": os.getcwd(), "prompt": prompt}) + "\n")

steps = json.load(open(scenario_path)).get(provider, [])
if n >= len(steps):
    print(f"fake {provider}: no step {n}", file=sys.stderr)
    sys.exit(99)
step = steps[n]


def span(kind):
    if step.get("span"):
        with open(step["span"], "a") as f:
            f.write(f"{kind} {time.time():.3f} {os.path.basename(os.getcwd())} {provider}\n")


span("start")
if step.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if step.get("spawn"):
    bg = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    with open(step["spawn"], "w") as f:
        f.write(str(bg.pid))
if step.get("touch_start"):
    open(step["touch_start"], "w").write("x")
if step.get("wait_for"):
    deadline = time.time() + 30
    while not os.path.exists(step["wait_for"]) and time.time() < deadline:
        time.sleep(0.02)
time.sleep(step.get("sleep", 0))
if step.get("snapshot"):
    m = re.search(r"write your result file to (\S+?)\.\s", prompt)
    summary = os.path.join(os.path.dirname(m.group(1)), "SUMMARY.md")
    if not os.path.exists(summary):      # Codex writes inside results/<id>/
        summary = os.path.join(os.path.dirname(os.path.dirname(m.group(1))), "SUMMARY.md")
    with open(summary) as src, open(step["snapshot"], "w") as dst:
        dst.write(src.read())
for name in (step.get("commit"), step.get("dirty")):
    if name:
        if os.path.dirname(name):
            os.makedirs(os.path.dirname(name), exist_ok=True)
        with open(name, "a") as f:
            f.write(step.get("content", f"{provider} call {n}\n"))
if step.get("commit"):
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"{provider}: {step['commit']}"], check=True)
for target, text in (step.get("write") or {}).items():
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(text)
if step.get("status"):
    m = re.search(r"write your result file to (\S+?)\.\s", prompt)
    if m:
        with open(m.group(1), "w") as f:
            f.write(f"STATUS: {step['status']}\n")
            if step.get("commit_message"):
                f.write(f"COMMIT: {step['commit_message']}\n")
            f.write("\n## Changed\nfake\n")
            f.write(step.get("result_extra", ""))
if step.get("bwrap"):
    print(json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "git status", "exit_code": 1,
        "aggregated_output": "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"}}))
if step.get("touch"):
    open(step["touch"], "w").write("x")
if step.get("progress"):
    m = re.search(r"keep (\S+) up to date", prompt)
    if m:
        os.makedirs(os.path.dirname(m.group(1)), exist_ok=True)
        with open(m.group(1), "w") as f:
            f.write(step["progress"])
for line in step.get("stdout", []):
    print(line if isinstance(line, str) else json.dumps(line))
if step.get("stderr"):
    print(step["stderr"], file=sys.stderr)
sys.stdout.flush()      # an agent that hangs has already printed what it printed (stall tests read the file)
sys.stderr.flush()
if step.get("touch_ready"):
    open(step["touch_ready"], "w").write("x")
time.sleep(step.get("hang", 0))
span("end")
sys.exit(step.get("exit", 0))
