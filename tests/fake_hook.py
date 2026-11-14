"""A stand-in for an `on_event` command: appends the BATON_* variables it was given, as one JSON line, to the
file named by its first argument. `fail` as the second argument exits 1; `hang` sleeps."""

import json
import os
import sys
import time

with open(sys.argv[1], "a") as f:
    f.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith("BATON_")}) + "\n")
if sys.argv[2:3] == ["hang"]:
    time.sleep(30)
sys.exit(1 if sys.argv[2:3] == ["fail"] else 0)
