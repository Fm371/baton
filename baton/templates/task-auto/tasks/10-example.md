+++
# id defaults to the file name ("10-example"); file names also break priority ties
repo = "../my-repo"           # relative to baton.toml, or absolute
work_type = "crud"            # a key of [routing.work_types]; or set rung = "r3"
branch = "feat/example"       # created from base if it doesn't exist
# base = "dev"                # overrides run.default_base for this task
# depends_on = ["05-schema"]  # runs only after these finished (done, or partial with commits)
# priority = 10               # higher runs first among tasks that are ready
# timeout_min = 60
# max_turns = 80              # Claude only
+++
## Goal
Add a `GET /widgets` endpoint that lists widgets, paginated.

## Acceptance
- `GET /widgets?page=1&page_size=20` returns 200 with `items` and `total`.
- `page_size` above 100 returns 422.
- Tests in `tests/api/test_widgets.py` cover both.
