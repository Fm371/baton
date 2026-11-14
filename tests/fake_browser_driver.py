"""A stand-in for baton/checks/browser_driver.js: it reads the job list and
writes observations, so the verdict logic is tested without a browser.

BATON_FAKE_BROWSER may point to a JSON file {job id: fields to merge into that
job's observation}; "__exit__": N makes the driver die with that exit code.
"""

import json
import os
import sys

spec = json.load(open(sys.argv[1]))
over = json.load(open(os.environ["BATON_FAKE_BROWSER"])) if os.environ.get("BATON_FAKE_BROWSER") else {}
if over.get("__exit__"):
    print("playwright is not installed here: Cannot find module", file=sys.stderr)
    sys.exit(over["__exit__"])


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"png")


observations = []
for job in spec["jobs"]:
    obs = {"id": job["id"], "error": None, "spinner_stuck": False, "console_errors": [],
           "requests": [{"url": "http://127.0.0.1:8000/api/plans", "method": "GET", "status": 200,
                         "resource_type": "fetch"}],
           "final_url": job["url"], "screenshot": job["screenshot"]}
    touch(job["screenshot"])
    if job.get("reference"):
        touch(job["reference"]["screenshot"])
    if job.get("invalid_submit"):
        obs["invalid_submit"] = {"message": "The email must look like name@example.com", "status": 422,
                                 "error": None}
    obs.update(over.get(job["id"], {}))
    observations.append(obs)
json.dump({"observations": observations}, open(sys.argv[2], "w"))
