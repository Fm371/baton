"""Loading and validating baton.toml."""

from __future__ import annotations

import difflib
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import gitutil

KNOWN_PROVIDERS = ("claude", "codex")
DEFAULT_TEST_PATHS = ["tests/**", "**/__tests__/**", "**/*.test.*", "**/test_*.py"]
DEFAULT_UI_PATHS = ["src/app/**", "*.tsx"]
DEFAULT_ENV_FILES = [".env", ".env.local", ".env.development", ".env.development.local"]
DEFAULT_CORS_KEYS = ["CORS_ORIGINS", "CORS_ALLOW_ORIGINS", "ALLOWED_ORIGINS", "BACKEND_CORS_ORIGINS"]
DEFAULT_REDIRECT_KEYS = ["ALLOWED_REDIRECT_URLS", "ALLOWED_REDIRECT_URIS", "REDIRECT_ALLOW_LIST",
                         "AUTH_REDIRECT_ALLOW_LIST"]


class ConfigError(Exception):
    pass


RUN_KEYS = {
    "tasks_dir", "verifiers_dir", "results_dir", "state_dir", "logs_dir", "common_prompt", "start_provider",
    "timezone", "task_timeout_min", "max_turns", "reset_margin_seconds", "unknown_limit_cooldown_min",
    "max_sleep_hours", "quick_exit_seconds", "max_quick_unknown", "transient_wait_seconds",
    "max_transient_retries", "no_result_retries", "commit_leftovers", "default_base", "watch_repos",
    "verifier_test_paths", "verifier_providers", "allow_codex_verifiers", "restore_branches", "hardcoded_key_check", "return_to_start_provider",
    "return_mid_task", "return_check_seconds", "verifiers_last", "existing_branch",
    "follow_poll_seconds", "lanes", "required_env", "min_version", "min_free_mb", "stall_minutes", "max_limit_cycles", "wip_secret_patterns", "wip_warn_patterns",
    "env_limited_action", "services_for_all_tasks", "on_event", "on_event_sleep_min", "on_event_timeout_seconds",
}
ENV_LIMITED_ACTIONS = ("note", "retry_other_provider")
SERVICE_KEYS = {"check", "restart", "timeout_s", "endpoint", "wait_s"}
PROVIDER_KEYS = {
    "bin", "enabled", "extra_args", "env", "settings", "permission_mode", "effort_flag", "add_dirs",
    "sandbox", "profile", "home", "known_models", "sandbox_probe", "max_concurrent", "network", "required_env",
}
TOP_LEVEL_KEYS = {"run", "providers", "routing", "plan", "repos", "preflight", "browser_smoke", "real_data_smoke",
                  "services"}


def _check_keys(table: dict, known: set[str], where: str) -> None:
    """A misspelled key must not be ignored silently: the default it leaves in
    place is usually not what the user meant."""
    bad = sorted(set(table) - known)
    if not bad:
        return
    parts = []
    for k in bad:
        near = difflib.get_close_matches(str(k), sorted(known), n=1, cutoff=0.6)
        parts.append(f"'{k}'" + (f" (did you mean '{near[0]}'?)" if near else ""))
    raise ConfigError(f"{where}: unknown key{'s' if len(bad) > 1 else ''} {', '.join(parts)}")


def _number(value, where: str, *, integer: bool = False, minimum: float | None = None,
            exclusive: bool = False) -> int | float:
    """A TOML number checked for type and range; a string like "abc" or a bool
    is a config error naming the key, not a traceback."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise ConfigError(f"{where} must be {'a whole number' if integer else 'a number'}, got {value!r}")
    if minimum is not None and (value <= minimum if exclusive else value < minimum):
        raise ConfigError(f"{where} must be {'greater than' if exclusive else 'at least'} {minimum:g}, got {value!r}")
    return value


@dataclass
class ProviderConfig:
    name: str
    bin: str
    enabled: bool = True
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    required_env: list[str] = field(default_factory=list)   # must be set (shell or `env`) before this provider runs a task
    # claude only
    settings: str | None = None
    permission_mode: str = "acceptEdits"
    effort_flag: bool = True
    # extra writable/readable directories supported by both CLIs
    add_dirs: list[str] = field(default_factory=list)
    # codex only
    sandbox: str | None = "workspace-write"
    profile: str | None = None
    # where the CLI keeps its session files (for the limit fallback)
    home: str | None = None
    # codex only: model ids to accept in preflight. Empty: read the installed
    # codex's own list (models_cache.json in its home folder), if it has one.
    known_models: list[str] = field(default_factory=list)
    # codex only: command arguments (after the binary) that probe whether the
    # sandbox works here; [] turns the probe off.
    sandbox_probe: list[str] | None = None
    # how many attempts may run on this provider at once (only matters with [run] lanes > 1)
    max_concurrent: int = 1
    # codex only: let commands inside the workspace-write sandbox use the network (tests that need
    # a real database on localhost hang or fail without it)
    network: bool = False


@dataclass
class RungSpec:
    model: str
    effort: str = ""


@dataclass
class Routing:
    rungs: dict[str, dict[str, RungSpec]]
    work_types: dict[str, str]

    def rung_for(self, work_type: str | None, rung: str | None) -> str:
        if rung:
            if rung not in self.rungs:
                raise ConfigError(f"unknown rung '{rung}'")
            return rung
        if work_type is None:
            raise ConfigError("task has neither work_type nor rung")
        if work_type not in self.work_types:
            raise ConfigError(f"unknown work_type '{work_type}'")
        return self.work_types[work_type]

    def spec(self, rung: str, provider: str) -> RungSpec | None:
        return self.rungs.get(rung, {}).get(provider)


@dataclass
class PreflightConfig:
    """[preflight]: what the env-preflight verifier reads. Nothing here is
    a secret; env files are read for key names only."""
    repos: list[Path] = field(default_factory=list)
    backend: Path | None = None            # the repo whose env files hold CORS and redirect lists
    panel_origins: list[str] = field(default_factory=list)
    auth_callback: str | None = None
    cors_keys: list[str] = field(default_factory=lambda: list(DEFAULT_CORS_KEYS))
    redirect_keys: list[str] = field(default_factory=lambda: list(DEFAULT_REDIRECT_KEYS))
    env_files: list[str] = field(default_factory=lambda: list(DEFAULT_ENV_FILES))
    auto: bool = True                      # baton plan schedules it after each feature
    services_tcp: list[str] = field(default_factory=list)   # "host:port" the agents' sandbox must reach

    @property
    def configured(self) -> bool:
        return bool(self.repos)


@dataclass
class ServiceConfig:
    """[services.<name>]: something the tasks need running (a database, a container)."""
    name: str
    check: str | list[str]               # exit 0 = healthy; a string runs through /bin/sh, a list as an argv
    restart: str | list[str] | None = None
    timeout_s: float = 30
    endpoint: str | None = None          # told to the agents ("mongo at 127.0.0.1:27017")
    wait_s: float = 5                    # pause after a restart, before the second check


@dataclass
class RepoConfig:
    """[repos."<path>"]: per-repository rules."""
    path: Path
    forbid_literals: list[re.Pattern] = field(default_factory=list)   # regexes no added line may match
    suite_cmd: str | list[str] | None = None        # the repository's full test suite (repo-suite)
    suite_baseline: Path | None = None              # failures that are known and not this feature's fault
    suite_timeout_min: float = 60
    suite_failure_regex: re.Pattern | None = None   # one group: the name of a failing test


REPO_KEYS = {"forbid_literals", "suite_cmd", "suite_baseline", "suite_timeout_min", "suite_failure_regex"}


@dataclass
class Config:
    base_dir: Path
    tasks_dir: Path
    verifiers_dir: Path
    results_dir: Path
    state_dir: Path
    logs_dir: Path
    common_prompt: Path | None
    start_provider: str
    timezone: ZoneInfo | None
    task_timeout_min: float
    max_turns: int
    reset_margin_s: int
    unknown_limit_cooldown_min: float
    max_sleep_hours: float
    quick_exit_s: float
    max_quick_unknown: int
    transient_wait_s: float
    max_transient: int
    no_result_retries: int
    commit_leftovers: bool
    default_base: str | None
    watch_repos: list[Path]
    verifier_test_paths: list[str]
    verifier_providers: list[str]
    providers: dict[str, ProviderConfig]
    routing: Routing
    ui_paths: list[str] = field(default_factory=lambda: list(DEFAULT_UI_PATHS))
    hardcoded_key_check: bool = True
    restore_branches: bool = True         # at the run's end put each repository back on the branch it was on
    allow_codex_verifiers: bool = False   # accept codex in verifier providers (with a warning)
    repos: dict[Path, RepoConfig] = field(default_factory=dict)   # by resolved path
    return_to_start_provider: bool = True
    return_mid_task: bool = False         # experimental: stop an attempt on another provider once start_provider resets
    return_check_s: float = 60
    verifiers_last: bool = False          # verifiers gate nothing; they run after every other task
    existing_branch: str = "fail"         # a task branch that exists but lacks its dependency's work: warn | rebase | fail
    min_version: str | None = None        # `baton plan` warns when the installed baton is older
    source: Path | None = None            # the baton.toml this was loaded from (read again between attempts)
    follow_poll_s: float = 15
    required_env: list[str] = field(default_factory=list)   # variables every agent task needs (tests skip without them)
    lanes: int = 1                  # tasks that may run at once, in different repositories (experimental above 1)
    # file-name globs for WIP commits (see gitutil): refused (never committed) and warned (committed, reported)
    wip_secret_patterns: tuple = gitutil.DEFAULT_SECRET_PATTERNS
    wip_warn_patterns: tuple = gitutil.DEFAULT_WARN_PATTERNS
    min_free_mb: float = 500        # preflight warns when the baton folder has less free space than this
    stall_minutes: float = 0        # 0 = off; an attempt whose output has not grown for this long is ended
    max_limit_cycles: int = 6       # usage-limit outcomes in a row without any progress before the task fails
    preflight: PreflightConfig = field(default_factory=PreflightConfig)
    env_limited_action: str = "note"      # "retry_other_provider": retry once elsewhere
    services: dict[str, ServiceConfig] = field(default_factory=dict)
    services_for_all_tasks: bool = False  # check the services before every task, not only those with `needs`
    on_event: list[str] | None = None     # command run on task end, long sleeps, run end (see baton/notify.py)
    on_event_sleep_min: float = 30
    on_event_timeout_s: float = 15
    browser_smoke: dict = field(default_factory=dict)     # [browser_smoke], read by that template
    real_data_smoke: dict = field(default_factory=dict)   # [real_data_smoke]

    def enabled_providers(self) -> list[str]:
        return [n for n in KNOWN_PROVIDERS if n in self.providers and self.providers[n].enabled]


def _path(base: Path, value: str | None, default: str | None = None) -> Path | None:
    value = value if value is not None else default
    if value is None:
        return None
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else (base / p)


def _positive_int(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} must be a whole number of at least 1")
    return value


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false (without quotes), not {value!r}")
    return value


def _opt_str(value, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string, not {value!r}")
    return value


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_names(value, where: str) -> list[str]:
    """A list of environment variable names (required_env)."""
    names = _str_list(value, where)
    bad = [n for n in names if not ENV_NAME_RE.match(n)]
    if bad:
        raise ConfigError(f"{where}: {', '.join(repr(n) for n in bad)} is not an environment variable name")
    return list(dict.fromkeys(names))


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def _glob_list(value, where: str, default: tuple) -> tuple:
    """A list of file-name globs. Absent: the default. An empty list switches the tier off."""
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where} must be a list of non-empty glob strings, not {value!r}")
    return tuple(v.strip() for v in value)


def _tcp_list(value, where: str) -> list[str]:
    out = _str_list(value, where)
    for item in out:
        host, _, port = item.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ConfigError(f"{where}: '{item}' must look like host:port, e.g. 127.0.0.1:27017")
    return out


def _command(value, where: str) -> str | list[str]:
    """A command from the config: a string (run through /bin/sh) or a list of arguments."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    raise ConfigError(f"{where} must be a command string or a list of arguments")


def _load_services(d) -> dict[str, ServiceConfig]:
    if not isinstance(d, dict):
        raise ConfigError("[services] must be a table of [services.<name>] tables")
    out: dict[str, ServiceConfig] = {}
    for name, table in d.items():
        where = f"[services.{name}]"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(name)):
            raise ConfigError(f"{where}: the name may only contain letters, digits, '.', '_' and '-'")
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        _check_keys(table, SERVICE_KEYS, where)
        if "check" not in table:
            raise ConfigError(f"{where} needs a `check` command (exit code 0 = healthy)")
        out[name] = ServiceConfig(
            name=name,
            check=_command(table["check"], f"services.{name}.check"),
            restart=_command(table["restart"], f"services.{name}.restart") if "restart" in table else None,
            timeout_s=float(_number(table.get("timeout_s", 30), f"services.{name}.timeout_s",
                                    minimum=0, exclusive=True)),
            endpoint=_opt_str(table.get("endpoint"), f"services.{name}.endpoint"),
            wait_s=float(_number(table.get("wait_s", 5), f"services.{name}.wait_s", minimum=0)),
        )
    return out


def _load_on_event(value) -> list[str] | None:
    """[run] on_event: a command line (split like a shell would, but never run through one) or an argv list."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            argv = shlex.split(value)
        except ValueError as e:
            raise ConfigError(f"run.on_event: {e}") from e
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        argv = list(value)
    else:
        raise ConfigError("run.on_event must be a command line or a list of arguments")
    if not argv:
        raise ConfigError("run.on_event is empty")
    return argv


def _load_preflight(base: Path, d: dict) -> PreflightConfig:
    if not isinstance(d, dict):
        raise ConfigError("[preflight] must be a table")
    known = {"repos", "backend", "panel_origins", "auth_callback", "cors_keys", "redirect_keys",
             "env_files", "auto", "services_tcp"}
    if set(d) - known:
        raise ConfigError(f"[preflight]: unknown keys: {', '.join(sorted(set(d) - known))}")
    pc = PreflightConfig(
        repos=[_path(base, r) for r in _str_list(d.get("repos"), "preflight.repos")],
        backend=_path(base, d.get("backend")),
        panel_origins=_str_list(d.get("panel_origins"), "preflight.panel_origins"),
        auth_callback=d.get("auth_callback"),
        auto=_bool(d.get("auto", True), "preflight.auto"),
        services_tcp=_tcp_list(d.get("services_tcp"), "preflight.services_tcp"),
    )
    for key in ("cors_keys", "redirect_keys", "env_files"):
        if key in d:
            setattr(pc, key, _str_list(d[key], f"preflight.{key}"))
    return pc


def _forbid_patterns(key: str, value) -> list[re.Pattern]:
    patterns = []
    for rx in _str_list(value, f"repos.\"{key}\".forbid_literals"):
        try:
            patterns.append(re.compile(rx))
        except re.error as e:
            raise ConfigError(f"[repos.\"{key}\"] forbid_literals: bad regex {rx!r}: {e}") from e
    return patterns


def _suite_cmd(key: str, cmd):
    if cmd is not None and not (isinstance(cmd, str) and cmd.strip()) and not (
            isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd)):
        raise ConfigError(f"[repos.\"{key}\"] suite_cmd must be a command string or a list of arguments")
    return cmd


def _failure_regex(key: str, value) -> re.Pattern | None:
    if not value:
        return None
    try:
        rx = re.compile(str(value))
    except re.error as e:
        raise ConfigError(f"[repos.\"{key}\"] suite_failure_regex: {e}") from e
    if rx.groups < 1:
        raise ConfigError(f"[repos.\"{key}\"] suite_failure_regex needs one group: the failing test's name")
    return rx


def _load_repo(base: Path, key: str, table) -> RepoConfig:
    if not isinstance(table, dict):
        raise ConfigError(f"[repos.\"{key}\"] must be a table")
    if set(table) - REPO_KEYS:
        raise ConfigError(f"[repos.\"{key}\"]: unknown keys: {', '.join(sorted(set(table) - REPO_KEYS))}")
    patterns = _forbid_patterns(key, table.get("forbid_literals"))
    cmd = _suite_cmd(key, table.get("suite_cmd"))
    failure_rx = _failure_regex(key, table.get("suite_failure_regex"))
    path = _path(base, key).resolve()
    return RepoConfig(
        path=path, forbid_literals=patterns, suite_cmd=cmd,
        suite_baseline=_path(base, table["suite_baseline"]) if table.get("suite_baseline") else None,
        suite_timeout_min=float(_number(table.get("suite_timeout_min", 60),
                                        f"repos.\"{key}\".suite_timeout_min", minimum=0, exclusive=True)),
        suite_failure_regex=failure_rx)


def _load_repos(base: Path, d) -> dict[Path, RepoConfig]:
    if not isinstance(d, dict):
        raise ConfigError("[repos] must be a table of [repos.\"<path>\"] tables")
    out: dict[Path, RepoConfig] = {}
    for key, table in d.items():
        repo = _load_repo(base, key, table)
        out[repo.path] = repo
    return out


def _default_start_provider(providers) -> str:
    """Codex starts a run unless the config says otherwise; a config without a
    enabled [providers.codex] section starts on the first enabled provider."""
    enabled = [n for n, p in providers.items() if p.enabled]
    if "codex" in enabled:
        return "codex"
    return (enabled or list(providers) or ["codex"])[0]


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: {e}") from e
    try:
        cfg = _build(path, data)
        cfg.source = path
        return cfg
    except ConfigError as e:
        if str(e).startswith(path.name):
            raise
        raise ConfigError(f"{path.name}: {e}") from e


@dataclass(frozen=True)
class _Ctx:
    """What a field parser may need besides the value: the config folder and the providers read so far."""
    base: Path
    providers: dict | None = None


@dataclass(frozen=True)
class _Field:
    """One key of a table: where it is read, which attribute it fills, how it is checked, its default."""
    key: str
    attr: str
    parse: object            # (value, where, ctx) -> the attribute's value; raises ConfigError
    default: object = None   # used when the key is absent (None: the parser sees None)


def _read_fields(fields, table: dict, prefix: str, ctx: _Ctx) -> dict:
    """Every field in table order, so the first problem reported is the first one in the list."""
    return {f.attr: f.parse(table.get(f.key, f.default), f"{prefix}.{f.key}", ctx) for f in fields}


def _p_float(minimum=None, exclusive=False):
    return lambda v, w, c: float(_number(v, w, minimum=minimum, exclusive=exclusive))


def _p_int(minimum=None):
    return lambda v, w, c: _number(v, w, integer=True, minimum=minimum)


def _p_bool(v, w, c):
    return _bool(v, w)


def _p_opt_str(v, w, c):
    return _opt_str(v, w)


def _p_str_list(v, w, c):
    return _str_list(v, w)


def _p_env_names(v, w, c):
    return env_names(v, w)


def _p_positive_int(v, w, c):
    return _positive_int(v, w)


def _p_paths(v, w, c):
    return [_path(c.base, r) for r in _str_list(v, w)]


def _p_dir(default: str | None = None):
    return lambda v, w, c: _path(c.base, v, default)


def _p_start_provider(v, w, c):
    return _opt_str(v, w) or _default_start_provider(c.providers)


def _p_test_paths(v, w, c):
    return list(DEFAULT_TEST_PATHS) if v is None else _str_list(v, w)


def _p_verifier_providers(v, w, c):
    names = ["claude"] if v is None else _str_list(v, w)
    for name in names:
        if name not in KNOWN_PROVIDERS:
            raise ConfigError(f"run.verifier_providers: unknown provider '{name}'")
    return names


def _p_existing_branch(v, w, c):
    value = _opt_str(v, w) or "fail"
    if value not in ("warn", "rebase", "fail"):
        raise ConfigError(f"run.existing_branch must be \"warn\", \"rebase\" or \"fail\", not {value!r}")
    return value


def _p_env_limited_action(v, w, c):
    value = _opt_str(v, w) or "note"
    if value not in ENV_LIMITED_ACTIONS:
        raise ConfigError(f"run.env_limited_action must be one of {', '.join(ENV_LIMITED_ACTIONS)}, "
                          f"not '{value}'")
    return value


def _p_secret_patterns(v, w, c):
    return _glob_list(v, w, gitutil.DEFAULT_SECRET_PATTERNS)


def _p_warn_patterns(v, w, c):
    return _glob_list(v, w, gitutil.DEFAULT_WARN_PATTERNS)


def _p_on_event(v, w, c):
    return _load_on_event(v)


# [run]: the order is the order in which problems are reported
RUN_FIELDS = (
    _Field("tasks_dir", "tasks_dir", _p_dir("tasks")),
    _Field("verifiers_dir", "verifiers_dir", _p_dir("verifiers")),
    _Field("results_dir", "results_dir", _p_dir("results")),
    _Field("state_dir", "state_dir", _p_dir("state")),
    _Field("logs_dir", "logs_dir", _p_dir("logs")),
    _Field("common_prompt", "common_prompt", _p_dir()),
    _Field("start_provider", "start_provider", _p_start_provider),
    _Field("task_timeout_min", "task_timeout_min", _p_float(0, True), 240),
    _Field("max_turns", "max_turns", _p_int(0), 0),
    _Field("reset_margin_seconds", "reset_margin_s", _p_int(0), 180),
    _Field("unknown_limit_cooldown_min", "unknown_limit_cooldown_min", _p_float(0), 30),
    _Field("max_sleep_hours", "max_sleep_hours", _p_float(0, True), 170),
    _Field("quick_exit_seconds", "quick_exit_s", _p_float(0), 120),
    _Field("max_quick_unknown", "max_quick_unknown", _p_int(1), 3),
    _Field("transient_wait_seconds", "transient_wait_s", _p_float(0), 600),
    _Field("max_transient_retries", "max_transient", _p_int(0), 3),
    _Field("no_result_retries", "no_result_retries", _p_int(0), 1),
    _Field("commit_leftovers", "commit_leftovers", _p_bool, True),
    _Field("default_base", "default_base", _p_opt_str),
    _Field("watch_repos", "watch_repos", _p_paths),
    _Field("verifier_test_paths", "verifier_test_paths", _p_test_paths),
    _Field("verifier_providers", "verifier_providers", _p_verifier_providers),
    _Field("restore_branches", "restore_branches", _p_bool, True),
    _Field("allow_codex_verifiers", "allow_codex_verifiers", _p_bool, False),
    _Field("hardcoded_key_check", "hardcoded_key_check", _p_bool, True),
    _Field("return_to_start_provider", "return_to_start_provider", _p_bool, True),
    _Field("return_mid_task", "return_mid_task", _p_bool, False),
    _Field("return_check_seconds", "return_check_s", _p_float(0, True), 60),
    _Field("verifiers_last", "verifiers_last", _p_bool, False),
    _Field("existing_branch", "existing_branch", _p_existing_branch),
    _Field("min_version", "min_version", _p_opt_str),
    _Field("required_env", "required_env", _p_env_names),
    _Field("follow_poll_seconds", "follow_poll_s", _p_float(0, True), 15),
    _Field("lanes", "lanes", _p_positive_int, 1),
    _Field("wip_secret_patterns", "wip_secret_patterns", _p_secret_patterns),
    _Field("wip_warn_patterns", "wip_warn_patterns", _p_warn_patterns),
    _Field("min_free_mb", "min_free_mb", _p_float(0), 500),
    _Field("stall_minutes", "stall_minutes", _p_float(0), 0),
    _Field("max_limit_cycles", "max_limit_cycles", _p_int(1), 6),
    _Field("env_limited_action", "env_limited_action", _p_env_limited_action),
    _Field("services_for_all_tasks", "services_for_all_tasks", _p_bool, False),
    _Field("on_event", "on_event", _p_on_event),
    _Field("on_event_sleep_min", "on_event_sleep_min", _p_float(0), 30),
    _Field("on_event_timeout_seconds", "on_event_timeout_s", _p_float(0, True), 15),
)


def _p_settings(v, w, c):
    return str(_path(c.base, v)) if v else None


def _p_add_dirs(v, w, c):
    return [str(_path(c.base, d)) for d in _str_list(v, w)]


def _p_home(v, w, c):
    return os.path.expanduser(v) if v else None


def _p_sandbox(v, w, c):
    return v or None


def _p_raw(v, w, c):
    return v


def _p_sandbox_probe(v, w, c):
    return None if v is None else _str_list(v, w)


# [providers.<name>]: `bin` and `env` are read first, see _load_provider
PROVIDER_FIELDS = (
    _Field("enabled", "enabled", _p_bool, True),
    _Field("extra_args", "extra_args", _p_str_list),
    _Field("required_env", "required_env", _p_env_names),
    _Field("settings", "settings", _p_settings),
    _Field("permission_mode", "permission_mode", _p_opt_str, "acceptEdits"),
    _Field("effort_flag", "effort_flag", _p_bool, True),
    _Field("add_dirs", "add_dirs", _p_add_dirs),
    _Field("sandbox", "sandbox", _p_sandbox, "workspace-write"),
    _Field("profile", "profile", _p_raw),
    _Field("home", "home", _p_home),
    _Field("known_models", "known_models", _p_str_list),
    _Field("sandbox_probe", "sandbox_probe", _p_sandbox_probe),
    _Field("max_concurrent", "max_concurrent", _p_positive_int, 1),
    _Field("network", "network", _p_bool, False),
)


def _load_timezone(run: dict) -> ZoneInfo | None:
    if not run.get("timezone"):
        return None
    try:
        return ZoneInfo(str(run["timezone"]))
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ConfigError(f"run.timezone: unknown timezone '{run['timezone']}' (use an IANA name such as "
                          "Europe/Berlin, or leave it out to use this machine's zone)") from e


def _provider_bin(base: Path, name: str, p: dict) -> str:
    bin_ = _opt_str(p.get("bin", name), f"providers.{name}.bin") or name
    # a relative path with a slash is relative to the config folder
    if "/" in bin_ and not os.path.isabs(os.path.expanduser(bin_)):
        bin_ = str(base / bin_)
    return os.path.expanduser(bin_)


def _load_provider(base: Path, name: str, p) -> ProviderConfig:
    if name not in KNOWN_PROVIDERS:
        raise ConfigError(f"unknown provider '{name}' (known: {', '.join(KNOWN_PROVIDERS)})")
    if not isinstance(p, dict):
        raise ConfigError(f"[providers.{name}] must be a table")
    _check_keys(p, PROVIDER_KEYS, f"[providers.{name}]")
    env = p.get("env", {})
    if not isinstance(env, dict):
        raise ConfigError(f"providers.{name}.env must be a table")
    bin_ = _provider_bin(base, name, p)
    fields = _read_fields(PROVIDER_FIELDS, p, f"providers.{name}", _Ctx(base))
    provider = ProviderConfig(name=name, bin=bin_, env={str(k): str(v) for k, v in env.items()}, **fields)
    if provider.network and name != "codex":
        raise ConfigError(f"providers.{name}.network only applies to codex (its sandbox blocks the network "
                          "by default); Claude's access is set in its settings file")
    return provider


def _load_providers(base: Path, providers_data) -> dict[str, ProviderConfig]:
    if not isinstance(providers_data, dict):
        raise ConfigError("[providers] must be a table of [providers.<name>] tables")
    providers = {name: _load_provider(base, name, p) for name, p in providers_data.items()}
    if not providers:
        raise ConfigError("no [providers.*] section")
    return providers


def _load_rungs(rungs_data) -> dict[str, dict[str, RungSpec]]:
    if not isinstance(rungs_data, dict):
        raise ConfigError("[routing.rungs] must be a table of [routing.rungs.<name>.<provider>] tables")
    rungs: dict[str, dict[str, RungSpec]] = {}
    for rung, per_provider in rungs_data.items():
        if not isinstance(per_provider, dict):
            raise ConfigError(f"routing.rungs.{rung} must be a table with one sub-table per provider "
                              f"(e.g. [routing.rungs.{rung}.claude]), not {per_provider!r}")
        rungs[rung] = {}
        for prov, spec in per_provider.items():
            if prov not in KNOWN_PROVIDERS:
                raise ConfigError(f"routing.rungs.{rung}: unknown provider '{prov}'")
            if not isinstance(spec, dict) or "model" not in spec:
                raise ConfigError(f"routing.rungs.{rung}.{prov} needs a model")
            rungs[rung][prov] = RungSpec(model=str(spec["model"]), effort=str(spec.get("effort", "")))
    return rungs


def _load_work_types(work_types_data, rungs: dict) -> dict[str, str]:
    if not isinstance(work_types_data, dict):
        raise ConfigError("[routing.work_types] must be a table")
    work_types = {str(k): str(v) for k, v in work_types_data.items()}
    for wt, rung in work_types.items():
        if rung not in rungs:
            raise ConfigError(f"work type '{wt}' points to unknown rung '{rung}'")
    return work_types


def _load_routing(routing_data) -> Routing:
    if not isinstance(routing_data, dict):
        raise ConfigError("[routing] must be a table")
    rungs = _load_rungs(routing_data.get("rungs", {}))
    work_types = _load_work_types(routing_data.get("work_types", {}), rungs)
    if not rungs:
        raise ConfigError("no [routing.rungs] defined")
    return Routing(rungs=rungs, work_types=work_types)


def _load_sections(cfg: Config, base: Path, data: dict) -> None:
    """The tables outside [run] and the checks that need the whole config."""
    cfg.services = _load_services(data.get("services", {}))
    plan = data.get("plan", {})
    if "ui_paths" in plan:
        cfg.ui_paths = _str_list(plan["ui_paths"], "plan.ui_paths")
    cfg.repos = _load_repos(base, data.get("repos", {}))
    cfg.preflight = _load_preflight(base, data.get("preflight", {}))
    for name in ("browser_smoke", "real_data_smoke"):
        section = data.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        setattr(cfg, name, section)
    if cfg.start_provider not in cfg.providers:
        raise ConfigError(f"start_provider '{cfg.start_provider}' has no [providers.{cfg.start_provider}] section")
    if cfg.common_prompt and not cfg.common_prompt.is_file():
        raise ConfigError(f"common_prompt not found: {cfg.common_prompt}")


def _build(path: Path, data: dict) -> Config:
    base = path.parent
    _check_keys(data, TOP_LEVEL_KEYS, "top level")
    run = data.get("run", {})
    if not isinstance(run, dict):
        raise ConfigError("[run] must be a table")
    _check_keys(run, RUN_KEYS, "[run]")
    tz = _load_timezone(run)
    providers = _load_providers(base, data.get("providers", {}))
    routing = _load_routing(data.get("routing", {}))
    values = _read_fields(RUN_FIELDS, run, "run", _Ctx(base, providers))
    cfg = Config(base_dir=base, timezone=tz, providers=providers, routing=routing, **values)
    _load_sections(cfg, base, data)
    return cfg
