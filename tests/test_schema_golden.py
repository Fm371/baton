"""Characterization of baton.toml and task front-matter loading.

Every case below is loaded and its outcome (the exact error message, or every loaded value) is compared
with `tests/data/schema_golden.json`. The golden file was recorded from the loaders before they were
rewritten as tables, so this test pins the messages, the order in which problems are reported and the
values and defaults of the valid cases. To record again after an intended change:

    python3 -m tests.test_schema_golden --regen
"""

from __future__ import annotations

import dataclasses
import os
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from baton import config as cfg_mod
from baton.config import ConfigError, load_config
from baton.tasks import TaskError, parse_task_file

GOLDEN = Path(__file__).resolve().parent / "data" / "schema_golden.json"

BASE = """\
[providers.claude]
[providers.codex]
[routing.rungs.r1.claude]
model = "sonnet"
[routing.rungs.r1.codex]
model = "m1"
effort = "high"
[routing.work_types]
feature = "r1"
"""


def with_run(*lines: str) -> str:
    return "[run]\n" + "\n".join(lines) + "\n" + BASE


# ---- config cases: name -> toml text -------------------------------------------------------------

RUN_BAD = [
    "timezone = 'Nowhere/City'", "timezone = 5",
    "task_timeout_min = 'abc'", "task_timeout_min = 0", "task_timeout_min = true",
    "max_turns = -1", "max_turns = 1.5",
    "reset_margin_seconds = 'x'", "reset_margin_seconds = -1",
    "unknown_limit_cooldown_min = -1", "max_sleep_hours = 0", "quick_exit_seconds = -1",
    "max_quick_unknown = 0", "transient_wait_seconds = -5", "max_transient_retries = -1",
    "no_result_retries = -1", "commit_leftovers = 'false'", "default_base = 5", "watch_repos = 'x'",
    "verifier_test_paths = 'x'", "verifier_providers = ['gpt']", "verifier_providers = 'claude'",
    "hardcoded_key_check = 'no'", "return_to_start_provider = 1", "return_mid_task = 'yes'",
    "return_check_seconds = 0", "verifiers_last = 'x'", "existing_branch = 'merge'", "existing_branch = 5",
    "min_version = 5", "required_env = ['1bad']", "required_env = 'A'", "follow_poll_seconds = 0",
    "lanes = 0", "lanes = '2'", "lanes = true", "wip_secret_patterns = 'x'", "wip_secret_patterns = ['']",
    "wip_warn_patterns = [1]", "min_free_mb = -1", "stall_minutes = -1", "max_limit_cycles = 0",
    "env_limited_action = 'weird'", "env_limited_action = 5", "services_for_all_tasks = 'x'",
    "on_event = 5", "on_event = ''", "on_event = 'a \"b'", "on_event = []", "on_event = [1]",
    "on_event_sleep_min = -1", "on_event_timeout_seconds = 0", "start_provider = 'gemini'",
    "start_provider = 5", "common_prompt = 'nope.md'", "tasks_dirs = 'x'", "taskz_dir = 'x'",
]

CONFIG_CASES: dict[str, str] = {
    "minimal": BASE,
    "full": """\
[run]
tasks_dir = "t"
verifiers_dir = "v"
results_dir = "r"
state_dir = "s"
logs_dir = "l"
start_provider = "claude"
timezone = "Europe/Berlin"
task_timeout_min = 90.5
max_turns = 40
reset_margin_seconds = 60
unknown_limit_cooldown_min = 12
max_sleep_hours = 48
quick_exit_seconds = 30
max_quick_unknown = 5
transient_wait_seconds = 15
max_transient_retries = 2
no_result_retries = 0
commit_leftovers = false
default_base = "develop"
watch_repos = ["../a", "/abs/b"]
verifier_test_paths = ["spec/**"]
verifier_providers = ["claude", "codex"]
hardcoded_key_check = false
return_to_start_provider = false
return_mid_task = true
return_check_seconds = 30
verifiers_last = true
existing_branch = "rebase"
min_version = "0.3.0"
required_env = ["A_B", "C", "A_B"]
follow_poll_seconds = 7
lanes = 3
wip_secret_patterns = [" *.pem ", "id_*"]
wip_warn_patterns = []
min_free_mb = 100
stall_minutes = 20
max_limit_cycles = 9
env_limited_action = "retry_other_provider"
services_for_all_tasks = true
on_event = "notify-send 'baton done'"
on_event_sleep_min = 5
on_event_timeout_seconds = 3

[providers.claude]
bin = "bin/claude"
enabled = true
extra_args = ["--x"]
env = { A = 1, B = "two" }
required_env = ["K1"]
settings = "claude-settings.json"
permission_mode = "plan"
effort_flag = false
add_dirs = ["../shared"]
max_concurrent = 2

[providers.codex]
bin = "~/bin/codex"
sandbox = ""
profile = "p"
home = "~/.codex"
known_models = ["m1"]
sandbox_probe = ["--probe"]
network = true

[routing.rungs.r1.claude]
model = "sonnet"
effort = "medium"
[routing.rungs.r1.codex]
model = "m1"
[routing.rungs.r2.claude]
model = "opus"
[routing.work_types]
feature = "r1"
review = "r2"

[services.mongo]
check = "mongosh --eval 1"
restart = ["docker", "start", "mongo"]
timeout_s = 12
endpoint = "127.0.0.1:27017"
wait_s = 2

[plan]
ui_paths = ["web/**"]

[repos."../api"]
forbid_literals = ["prod-db"]
suite_cmd = ["pytest", "-q"]
suite_baseline = "baseline.txt"
suite_timeout_min = 15
suite_failure_regex = "FAILED (\\\\S+)"

[preflight]
repos = ["../api", "../web"]
backend = "../api"
panel_origins = ["http://127.0.0.1:3001"]
auth_callback = "http://127.0.0.1:8000/cb"
cors_keys = ["CORS"]
redirect_keys = []
env_files = [".env"]
auto = false
services_tcp = ["127.0.0.1:27017"]

[browser_smoke]
node_dir = "../web"

[real_data_smoke]
db_env = { URL = "X" }
""",
    "codex_disabled_default_start": """\
[providers.codex]
enabled = false
[providers.claude]
[routing.rungs.r1.claude]
model = "s"
""",
    "only_unknown_provider_order": "[providers.gemini]\n" + BASE,
    "two_bad_run_keys_first_wins": with_run("task_timeout_min = 0", "lanes = 0", "commit_leftovers = 'x'"),
    "bad_late_run_key_beats_nothing": with_run("on_event_timeout_seconds = 0", "min_free_mb = -1"),
    "bad_run_and_bad_provider": with_run("lanes = 0") .replace("[providers.claude]", "[providers.claude]\nbin = 5"),
    "run_error_before_routing_error": "[run]\nlanes = 0\n" + BASE.replace('model = "sonnet"', "x = 1"),
    "provider_error_before_run_error": "[run]\nlanes = 0\n[providers.claude]\nenabled = 'x'\n[routing.rungs.r1.claude]\nmodel = 'a'\n",
    "run_error_before_services_error": with_run("on_event_timeout_seconds = 0") + "[services.x]\ncheck = 5\n",
    "services_error_before_repos_error": BASE + "[services.x]\ncheck = 5\n[repos.'a']\nsuite_cmd = 5\n",
    "repos_error_before_preflight_error": BASE + "[repos.'a']\nsuite_cmd = 5\n[preflight]\nrepos = 'x'\n",
    "preflight_error_before_smoke_error": BASE + "[preflight]\nrepos = 'x'\nauto = 'x'\n[browser_smoke]\n",
    "start_provider_missing_section": "[run]\nstart_provider = 'codex'\n[providers.claude]\n[routing.rungs.r1.claude]\nmodel='a'\n",
    "no_providers": "[routing.rungs.r1.claude]\nmodel='a'\n",
    "no_rungs": "[providers.claude]\n",
    "top_level_unknown": BASE + "[extra]\nx = 1\n",
    "top_level_unknown_close": BASE + "[runn]\nx = 1\n",
    "run_not_a_table": "run = 5\n" + BASE,
    "providers_not_a_table": "providers = 5\n[routing.rungs.r1.claude]\nmodel='a'\n",
    "provider_not_a_table": "[providers]\nclaude = 5\n[routing.rungs.r1.claude]\nmodel='a'\n",
    "provider_unknown_key": BASE.replace("[providers.claude]", "[providers.claude]\nbinn = 'x'"),
    "provider_unknown_keys_two": BASE.replace("[providers.claude]", "[providers.claude]\nzz = 1\nyy = 2"),
    "provider_env_not_table": BASE.replace("[providers.claude]", "[providers.claude]\nenv = 5"),
    "provider_bin_5": BASE.replace("[providers.claude]", "[providers.claude]\nbin = 5"),
    "provider_bin_empty": BASE.replace("[providers.claude]", "[providers.claude]\nbin = ''"),
    "provider_enabled_str": BASE.replace("[providers.claude]", "[providers.claude]\nenabled = 'x'"),
    "provider_extra_args_str": BASE.replace("[providers.claude]", "[providers.claude]\nextra_args = 'x'"),
    "provider_required_env_bad": BASE.replace("[providers.claude]", "[providers.claude]\nrequired_env = ['a b']"),
    "provider_permission_mode_5": BASE.replace("[providers.claude]", "[providers.claude]\npermission_mode = 5"),
    "provider_effort_flag_str": BASE.replace("[providers.claude]", "[providers.claude]\neffort_flag = 'x'"),
    "provider_add_dirs_str": BASE.replace("[providers.claude]", "[providers.claude]\nadd_dirs = 'x'"),
    "provider_known_models_int": BASE.replace("[providers.codex]", "[providers.codex]\nknown_models = [1]"),
    "provider_sandbox_probe_str": BASE.replace("[providers.codex]", "[providers.codex]\nsandbox_probe = 'x'"),
    "provider_sandbox_probe_empty": BASE.replace("[providers.codex]", "[providers.codex]\nsandbox_probe = []"),
    "provider_max_concurrent_0": BASE.replace("[providers.claude]", "[providers.claude]\nmax_concurrent = 0"),
    "provider_max_concurrent_bool": BASE.replace("[providers.claude]", "[providers.claude]\nmax_concurrent = true"),
    "provider_network_str": BASE.replace("[providers.codex]", "[providers.codex]\nnetwork = 'x'"),
    "provider_network_on_claude": BASE.replace("[providers.claude]", "[providers.claude]\nnetwork = true"),
    "provider_bin_relative_slash": BASE.replace("[providers.claude]", "[providers.claude]\nbin = 'tools/claude'"),
    "provider_bin_absolute": BASE.replace("[providers.claude]", "[providers.claude]\nbin = '/usr/bin/claude'"),
    "provider_sandbox_none": BASE.replace("[providers.codex]", "[providers.codex]\nsandbox = ''"),
    "routing_not_table": "[providers.claude]\nrouting = 5\n",
    "routing_top_not_table": "routing = 5\n[providers.claude]\n",
    "rungs_not_table": "[providers.claude]\n[routing]\nrungs = 5\n",
    "rung_not_table": "[providers.claude]\n[routing.rungs]\nr1 = 'x'\n",
    "rung_unknown_provider": "[providers.claude]\n[routing.rungs.r1.gemini]\nmodel = 'x'\n",
    "rung_spec_without_model": "[providers.claude]\n[routing.rungs.r1.claude]\neffort = 'x'\n",
    "rung_spec_not_table": "[providers.claude]\n[routing.rungs.r1]\nclaude = 5\n",
    "work_types_not_table": "[providers.claude]\n[routing.rungs.r1.claude]\nmodel='a'\n[routing]\nwork_types = 5\n",
    "work_type_unknown_rung": BASE.replace('feature = "r1"', 'feature = "r9"'),
    "work_types_values_cast": BASE.replace('feature = "r1"', 'feature = "r1"\nx = "r1"'),
    "services_not_table": "services = 5\n" + BASE,
    "service_bad_name": BASE + "[services.'-x']\ncheck = 'true'\n",
    "service_not_table": BASE.replace("[providers.claude]", "[services]\nmongo = 5\n[providers.claude]"),
    "service_unknown_key": BASE + "[services.m]\ncheck = 'true'\nchek = 1\n",
    "service_no_check": BASE + "[services.m]\nrestart = 'x'\n",
    "service_check_bad": BASE + "[services.m]\ncheck = 5\n",
    "service_check_empty_str": BASE + "[services.m]\ncheck = ' '\n",
    "service_check_empty_list": BASE + "[services.m]\ncheck = []\n",
    "service_restart_bad": BASE + "[services.m]\ncheck = 'true'\nrestart = 5\n",
    "service_timeout_0": BASE + "[services.m]\ncheck = 'true'\ntimeout_s = 0\n",
    "service_endpoint_5": BASE + "[services.m]\ncheck = 'true'\nendpoint = 5\n",
    "service_wait_neg": BASE + "[services.m]\ncheck = 'true'\nwait_s = -1\n",
    "plan_ui_paths_str": BASE + "[plan]\nui_paths = 'x'\n",
    "plan_unknown_key_ignored": BASE + "[plan]\nzz = 1\n",
    "repos_not_table": "repos = 5\n" + BASE,
    "repo_entry_not_table": BASE.replace("[providers.claude]", "[repos]\na = 5\n[providers.claude]"),
    "repo_unknown_keys": BASE + "[repos.a]\nzz = 1\nyy = 1\n",
    "repo_bad_regex": BASE + "[repos.a]\nforbid_literals = ['(']\n",
    "repo_forbid_not_list": BASE + "[repos.a]\nforbid_literals = 'x'\n",
    "repo_suite_cmd_5": BASE + "[repos.a]\nsuite_cmd = 5\n",
    "repo_suite_cmd_empty": BASE + "[repos.a]\nsuite_cmd = ''\n",
    "repo_suite_cmd_list_bad": BASE + "[repos.a]\nsuite_cmd = ['a', 1]\n",
    "repo_failure_regex_bad": BASE + "[repos.a]\nsuite_failure_regex = '('\n",
    "repo_failure_regex_no_group": BASE + "[repos.a]\nsuite_failure_regex = 'FAILED'\n",
    "repo_failure_regex_empty": BASE + "[repos.a]\nsuite_failure_regex = ''\n",
    "repo_timeout_0": BASE + "[repos.a]\nsuite_timeout_min = 0\n",
    "repo_defaults": BASE + "[repos.a]\n",
    "preflight_not_table": "preflight = 5\n" + BASE,
    "preflight_unknown_keys": BASE + "[preflight]\nzz = 1\nyy = 1\n",
    "preflight_repos_str": BASE + "[preflight]\nrepos = 'x'\n",
    "preflight_panel_origins_str": BASE + "[preflight]\npanel_origins = 'x'\n",
    "preflight_auto_str": BASE + "[preflight]\nauto = 'x'\n",
    "preflight_services_tcp_bad": BASE + "[preflight]\nservices_tcp = ['nohost']\n",
    "preflight_services_tcp_port": BASE + "[preflight]\nservices_tcp = ['h:99999']\n",
    "preflight_cors_keys_bad": BASE + "[preflight]\ncors_keys = 'x'\n",
    "preflight_redirect_keys_bad": BASE + "[preflight]\nredirect_keys = [1]\n",
    "preflight_env_files_bad": BASE + "[preflight]\nenv_files = 5\n",
    "preflight_defaults": BASE + "[preflight]\n",
    "browser_smoke_not_table": "browser_smoke = 5\n" + BASE,
    "real_data_smoke_not_table": "real_data_smoke = 5\n" + BASE,
    "on_event_string": with_run("on_event = \"ntfy publish 'my topic'\""),
    "on_event_list": with_run("on_event = ['a', 'b']"),
    "existing_branch_empty_string": with_run("existing_branch = ''"),
    "env_limited_action_empty": with_run("env_limited_action = ''"),
    "start_provider_empty": with_run("start_provider = ''"),
    "timezone_empty": with_run("timezone = ''"),
    "bad_toml": "[run\n",
}
for _i, _line in enumerate(RUN_BAD):
    CONFIG_CASES[f"run_bad_{_i:02d}_{re.sub(r'[^a-z0-9]+', '_', _line.lower())[:40]}"] = with_run(_line)


# ---- task cases: name -> (kind, file text) -------------------------------------------------------

def task(front: str, body: str = "Do the thing.\n") -> str:
    return f"+++\n{front}\n+++\n{body}"


TASK_CONFIG = BASE + """\
[run]
default_base = "develop"
verifier_providers = ["claude"]
[services.mongo]
check = "true"
"""

TASK_BAD = [
    ("implement", 'repo = "."\nwork_type = "feature"\nzzz = 1'),
    ("implement", 'repo = "."\nwork_type = "feature"\nzzz = 1\nyyy = 2'),
    ("implement", 'repo = "."\nwork_type = "feature"\nkind = "weird"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nkind = "verify"'),
    ("verify", 'repo = "."\nwork_type = "feature"\nkind = "approve"'),
    ("verify", 'repo = "."\nwork_type = "feature"'),
    ("verify", 'repo = "."\nwork_type = "feature"\nkind = "implement"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nid = "-bad"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nid = "café"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nid = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\ntemplate = "repo-suite"'),
    ("verify", 'kind = "verify"\ntemplate = "nope"'),
    ("verify", 'kind = "verify"\nrepo = "."\nwork_type = "feature"\nwhen = "later"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nwhen = "feature-end"'),
    ("verify", 'kind = "verify"\nrepo = "."\nwork_type = "feature"\nwhen = "feature-end"'),
    ("verify", 'kind = "verify"\nrepo = "."\nwork_type = "feature"\nwhen = "feature-end"\nfeature = ""'),
    ("implement", 'repo = "."\nwork_type = "feature"\nmoney_gate = true'),
    ("verify", 'kind = "verify"\nrepo = "."\nwork_type = "feature"\nmoney_gate = "yes"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nparams = 5'),
    ("implement", 'work_type = "feature"'),
    ("verify", 'kind = "verify"\nwork_type = "feature"'),
    ("implement", 'repo = "."\nwork_type = "feature"\ndepends_on = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\ndepends_on = [1]'),
    ("implement", 'repo = "."\nwork_type = "feature"\nbranch = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nbase = ["a"]'),
    ("implement", 'repo = "."\nwork_type = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nrung = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nfeature = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nrequired_env = ["a b"]'),
    ("implement", 'repo = "."\nwork_type = "feature"\nrequired_env = "A"'),
    ("implement", 'repo = "."\nwork_type = "feature"\npaths = "x"'),
    ("implement", 'repo = "."\nwork_type = "feature"\npaths = [1]'),
    ("implement", 'repo = "."\nwork_type = "feature"\nproviders = []'),
    ("implement", 'repo = "."\nwork_type = "feature"\nproviders = ["gemini"]'),
    ("implement", 'repo = "."\nwork_type = "feature"\nproviders = "claude"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nlane = "-x"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nlane = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nneeds = 5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nneeds = ["redis"]'),
    ("implement", 'repo = "."\nwork_type = "feature"\nneeds = [1]'),
    ("implement", 'repo = "."\nwork_type = "feature"\nreturn_mid_task = "no"'),
    ("implement", 'repo = "."\nwork_type = "feature"\npriority = "high"'),
    ("implement", 'repo = "."\nwork_type = "feature"\npriority = 1.5'),
    ("implement", 'repo = "."\nwork_type = "feature"\npriority = -2000000'),
    ("implement", 'repo = "."\nwork_type = "feature"\ntimeout_min = 0'),
    ("implement", 'repo = "."\nwork_type = "feature"\ntimeout_min = "9"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nmax_turns = -1'),
    ("implement", 'repo = "."\nwork_type = "feature"\nmax_turns = 1.5'),
    ("implement", 'repo = "."\nwork_type = "feature"\nstall_minutes = -1'),
    ("implement", 'repo = "."'),
    ("implement", 'repo = "."\nwork_type = "nope"'),
    ("implement", 'repo = "."\nrung = "r9"'),
    ("implement", 'repo = "."\nwork_type = "feature"\nbranch = "x"\nlane = "ok"\nneeds = ["mongo"]\npriority = true'),
    ("implement", 'priority = "high"\nzzz = 1'),
    ("implement", 'repo = "."\nwork_type = "feature"\ndepends_on = 5\nbranch = 5\nparams = 5'),
]

TASK_CASES: dict[str, tuple[str, str]] = {
    "implement_minimal": ("implement", task('repo = "."\nwork_type = "feature"')),
    "implement_full": ("implement", task(
        'id = "T-1.a"\nrepo = "sub/{base_dir}"\nwork_type = "feature"\nrung = "r1"\n'
        'depends_on = ["a", "b"]\npriority = 7\nbranch = "feat/x"\nbase = "main"\ntimeout_min = 12.5\n'
        'max_turns = 30\nstall_minutes = 0\nreturn_mid_task = false\nproviders = ["codex", "claude"]\n'
        'feature = "f"\npaths = ["src/**"]\nlane = "db-1"\nneeds = ["mongo"]\nrequired_env = ["A", "A", "B"]',
        "Body with {base_dir} and text.\n")),
    "implement_depends_on_str": ("implement", task('repo = "."\nwork_type = "feature"\ndepends_on = "a"')),
    "implement_needs_str": ("implement", task('repo = "."\nwork_type = "feature"\nneeds = "mongo"')),
    "implement_rung_only": ("implement", task('repo = "."\nrung = "r1"')),
    "implement_work_type_and_rung": ("implement", task('repo = "."\nwork_type = "feature"\nrung = "r1"')),
    "implement_absolute_repo": ("implement", task('repo = "/tmp/abs-repo"\nwork_type = "feature"')),
    "implement_home_repo": ("implement", task('repo = "~/proj"\nwork_type = "feature"')),
    "implement_no_branch_auto": ("implement", task('repo = "."\nwork_type = "feature"')),
    "implement_empty_branch_string": ("implement", task('repo = "."\nwork_type = "feature"\nbranch = ""')),
    "implement_empty_base_string": ("implement", task('repo = "."\nwork_type = "feature"\nbase = ""')),
    "implement_float_priority_int_ok": ("implement", task('repo = "."\nwork_type = "feature"\npriority = -1000000')),
    "implement_timeout_float": ("implement", task('repo = "."\nwork_type = "feature"\ntimeout_min = 1')),
    "implement_empty_body": ("implement", task('repo = "."\nwork_type = "feature"', "   \n")),
    "implement_return_mid_task_true": ("implement", task('repo = "."\nwork_type = "feature"\nreturn_mid_task = true')),
    "implement_money_gate_false": ("implement", task('repo = "."\nwork_type = "feature"\nmoney_gate = false')),
    "verify_minimal": ("verify", task('kind = "verify"\nrepo = "."\nwork_type = "feature"')),
    "verify_providers_default": ("verify", task('kind = "verify"\nrepo = "."\nrung = "r1"')),
    "verify_explicit_providers": ("verify", task('kind = "verify"\nrepo = "."\nrung = "r1"\nproviders = ["codex"]')),
    "verify_feature_end": ("verify", task(
        'kind = "verify"\nrepo = "."\nrung = "r1"\nfeature = "f"\nwhen = "feature-end"\nmoney_gate = true')),
    "verify_template": ("verify", task('kind = "verify"\ntemplate = "repo-suite"\nrepo = "."', "")),
    "verify_template_params": ("verify", task(
        'kind = "verify"\ntemplate = "env-preflight"\n[params]\nx = 1\n[params.sub]\ny = "z"', "")),
    "verify_template_no_repo": ("verify", task('kind = "verify"\ntemplate = "browser-smoke"', "")),
    "verify_template_money_gate": ("verify", task(
        'kind = "verify"\ntemplate = "real-data-smoke"\nmoney_gate = true', "")),
    "approve_minimal": ("implement", task('kind = "approve"', "Is it fine?\n")),
    "approve_with_repo": ("implement", task('kind = "approve"\nrepo = "."\ndepends_on = ["a"]', "ok?\n")),
    "approve_empty_body": ("implement", task('kind = "approve"', "")),
    "no_front_matter": ("implement", "just text\n"),
    "unclosed_front_matter": ("implement", "+++\nrepo = '.'\n"),
    "bad_toml_front_matter": ("implement", "+++\nrepo = \n+++\nbody\n"),
    "bom_front_matter": ("implement", "﻿" + task('repo = "."\nwork_type = "feature"')),
    "crlf_front_matter": ("implement", task('repo = "."\nwork_type = "feature"').replace("\n", "\r\n")),
}
for _i, (_kind, _front) in enumerate(TASK_BAD):
    TASK_CASES[f"bad_{_i:02d}_{_kind}_{re.sub(r'[^a-z0-9]+', '_', _front.lower())[:42]}"] = (_kind, task(_front))


# ---- observing a case ----------------------------------------------------------------------------

# A fixed HOME while observing, so ~ expands the same way for every user (the golden says <HOME>).
FAKE_HOME = "/baton-test-home"


def _scrub(text: str, root: str) -> str:
    return text.replace(root, "<DIR>").replace(FAKE_HOME, "<HOME>")


def plain(value, root: str):
    """A JSON-able picture of a loaded object; the temp folder is <DIR> and the home folder <HOME>."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name), root) for f in dataclasses.fields(value)}
    if isinstance(value, Path):
        return _scrub(str(value), root)
    if isinstance(value, ZoneInfo):
        return f"zone:{value.key}"
    if isinstance(value, re.Pattern):
        return f"re:{value.pattern}"
    if isinstance(value, dict):
        return {_scrub(str(k), root): plain(v, root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v, root) for v in value]
    if isinstance(value, str):
        return _scrub(value, root)
    return value


def observe_config(text: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": FAKE_HOME}):
        root = str(Path(tmp).resolve())
        path = Path(tmp) / "baton.toml"
        path.write_text(text, encoding="utf-8")
        try:
            cfg = load_config(path)
        except ConfigError as e:
            return {"error": _scrub(str(e), root)}
        except Exception as e:                                      # noqa: BLE001 - recorded, not hidden
            return {"crash": _scrub(f"{type(e).__name__}: {e}", root)}
        return {"ok": plain(cfg, root)}


def observe_task(kind: str, text: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": FAKE_HOME}):
        root = str(Path(tmp).resolve())
        (Path(tmp) / "baton.toml").write_text(TASK_CONFIG, encoding="utf-8")
        cfg = load_config(Path(tmp) / "baton.toml")
        path = Path(tmp) / "T.md"
        path.write_bytes(text.encode("utf-8"))
        try:
            task_ = parse_task_file(path, cfg, kind)
        except TaskError as e:
            return {"error": _scrub(str(e), root)}
        except Exception as e:                                      # noqa: BLE001 - recorded, not hidden
            return {"crash": _scrub(f"{type(e).__name__}: {e}", root)}
        return {"ok": plain(task_, root)}


def observe_all() -> dict:
    out = {}
    for name, text in CONFIG_CASES.items():
        out[f"config/{name}"] = observe_config(text)
    for name, (kind, text) in TASK_CASES.items():
        out[f"task/{name}"] = observe_task(kind, text)
    return out


class FieldTablesTest(unittest.TestCase):
    """The tables that drive loading must agree with the key sets the docs are checked against."""

    def test_run_table_matches_the_allowed_run_keys(self):
        keys = [f.key for f in cfg_mod.RUN_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys) | {"timezone"}, cfg_mod.RUN_KEYS)

    def test_provider_table_matches_the_allowed_provider_keys(self):
        keys = [f.key for f in cfg_mod.PROVIDER_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys) | {"bin", "env", "type"}, cfg_mod.PROVIDER_KEYS)

    def test_every_field_fills_an_attribute_of_its_dataclass(self):
        config_fields = {f.name for f in dataclasses.fields(cfg_mod.Config)}
        provider_fields = {f.name for f in dataclasses.fields(cfg_mod.ProviderConfig)}
        self.assertEqual([f.attr for f in cfg_mod.RUN_FIELDS if f.attr not in config_fields], [])
        self.assertEqual([f.attr for f in cfg_mod.PROVIDER_FIELDS if f.attr not in provider_fields], [])


class SchemaGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_the_golden_file_covers_every_case(self):
        expected = {f"config/{n}" for n in CONFIG_CASES} | {f"task/{n}" for n in TASK_CASES}
        self.assertEqual(sorted(expected), sorted(self.golden))

    def test_config_cases(self):
        for name, text in CONFIG_CASES.items():
            with self.subTest(name):
                self.assertEqual(self.golden[f"config/{name}"], observe_config(text))

    def test_task_cases(self):
        for name, (kind, text) in TASK_CASES.items():
            with self.subTest(name):
                self.assertEqual(self.golden[f"task/{name}"], observe_task(kind, text))

    def test_the_cases_exercise_both_outcomes(self):
        oks = [k for k, v in self.golden.items() if "ok" in v]
        errors = [k for k, v in self.golden.items() if "error" in v]
        self.assertGreater(len(oks), 25)
        self.assertGreater(len(errors), 120)


if __name__ == "__main__":
    if "--regen" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(observe_all(), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        print(f"wrote {GOLDEN}")
    else:
        unittest.main()
